"""Opt-in cedb477 terminal experiment; default loading/import is inert.

General and endpoint plugins reuse HUST's existing utility and HTTP surfaces.
No engine files are edited. This is not Manager activation or a production ABI.
"""

import asyncio
import contextvars
import hmac
import os
from dataclasses import dataclass
from types import MethodType

from .adapters.terminal_guard import PatchSet, verify_callable, verify_installation
from .entry_capture import EntryCapture
from .terminal_transport import CORE_METHOD, CoreEnds, FrontendEnds

PLUGIN_NAME = "prefix_routing_terminal"
_context = contextvars.ContextVar("prefix_routing_terminal_capture", default=None)


def enabled():
    value = os.environ.get("PREFIX_ROUTING_HUST_TERMINAL", "0")
    if value not in ("0", "1"):
        raise ValueError("PREFIX_ROUTING_HUST_TERMINAL must be 0 or 1")
    if value == "1" and PLUGIN_NAME not in os.environ.get("VLLM_PLUGINS", "").split(
        ","
    ):
        raise RuntimeError("Terminal experiment requires an explicit plugin allowlist")
    return value == "1"


def _core_poll(core, epoch, after, acknowledge, disable=False):
    if type(disable) is not bool:
        raise ValueError("Invalid disable flag")
    if not hasattr(core, "_prefix_terminal_source"):
        if (
            epoch is not None
            or type(after) is not int
            or type(acknowledge) is not int
            or after != 0
            or acknowledge != 0
            or disable
        ):
            raise ValueError("Explicit empty terminal handshake required")
        config = core.vllm_config
        if (
            config.parallel_config.tensor_parallel_size != 1
            or config.parallel_config.data_parallel_size != 1
            or core.scheduler.connector is not None
            or core.scheduler.ec_connector is not None
        ):
            raise RuntimeError("Terminal experiment supports one local TP1/DP1 core")
        original = core.scheduler._free_request
        verify_callable(
            original, "vllm/v1/core/sched/scheduler.py", "Scheduler._free_request"
        )
        source = CoreEnds()
        patches = PatchSet()

        def cleanup(scheduler, request, *args, **kwargs):
            result = original(request, *args, **kwargs)
            try:
                source.observe(
                    request.request_id,
                    getattr(getattr(request, "status", None), "name", "unknown"),
                )
            except Exception:
                source.failed = True
            return result

        patches.set(
            core.scheduler, "_free_request", MethodType(cleanup, core.scheduler)
        )
        core._prefix_terminal_source = source
        core._prefix_terminal_patches = patches
    source = core._prefix_terminal_source
    core._prefix_terminal_patches.check_current()
    reply = source.poll(epoch, after, acknowledge)
    if disable:
        if epoch != source.epoch:
            raise ValueError("Explicit source identity required before disabling")
        core._prefix_terminal_patches.restore()
    reply["scheduler_type"] = type(core.scheduler).__qualname__
    reply["cleanup_observer_installed"] = bool(core._prefix_terminal_patches.entries)
    return reply


def load_plugin():
    if not enabled():
        return
    verify_installation()
    from vllm.v1.engine.core import EngineCore

    if hasattr(EngineCore, CORE_METHOD):
        if getattr(EngineCore, CORE_METHOD) is not _core_poll:
            raise RuntimeError("Terminal utility name already owned")
        return
    setattr(EngineCore, CORE_METHOD, _core_poll)


@dataclass
class Capture:
    attempt: str
    entry: EntryCapture | None = None


def attach_frontend(frontend, state, serving_types, *, guard=verify_callable):
    """Install explicit, reversible bindings after all backend platform loading."""
    patches = PatchSet()
    originals = {
        "generate": frontend.generate,
        "add_request": frontend.add_request,
        "output_add": frontend.output_processor.add_request,
    }
    guard(originals["generate"], "vllm/v1/engine/async_llm.py", "AsyncLLM.generate")
    guard(
        originals["add_request"], "vllm/v1/engine/async_llm.py", "AsyncLLM.add_request"
    )
    guard(
        originals["output_add"],
        "vllm/v1/engine/output_processor.py",
        "OutputProcessor.add_request",
    )
    for cls, create_name, render_name, path in serving_types:
        guard(getattr(cls, create_name), path, f"{cls.__name__}.{create_name}")
        guard(getattr(cls, render_name), path, f"{cls.__name__}.{render_name}")

    def output_add(processor, request, *args, **kwargs):
        capture = _context.get()
        if capture is not None and capture.entry is not None:
            capture.entry.bind(request.external_req_id, request.request_id)
        return originals["output_add"](request, *args, **kwargs)

    async def add(front, request_id, *args, **kwargs):
        capture = _context.get()
        try:
            result = await originals["add_request"](request_id, *args, **kwargs)
        except BaseException:
            if capture is not None and capture.entry is not None:
                capture.entry.abandon()
            raise
        if capture is not None and capture.entry is not None:
            capture.entry.submitted(request_id)
        return result

    def generate(front, prompt, params, request_id, *args, **kwargs):
        capture = _context.get()
        if capture is None:
            return originals["generate"](prompt, params, request_id, *args, **kwargs)
        if capture.entry is None:
            raise RuntimeError("Rendered family required before generation")
        capture.entry.register_group(request_id, params.n)

        async def drive():
            iterator = originals["generate"](
                prompt, params, request_id, *args, **kwargs
            )
            try:
                # Fixed normal-input AsyncLLM finishes add_request before its
                # first output. Later token chunks need no capture operations.
                token = _context.set(capture)
                try:
                    output = await anext(iterator)
                except StopAsyncIteration:
                    return
                finally:
                    _context.reset(token)
                yield output
                async for output in iterator:
                    yield output
            finally:
                # Never carry a ContextVar token across yield. Streaming close
                # can run in a different task/context from the last next().
                token = _context.set(capture)
                try:
                    await iterator.aclose()
                finally:
                    _context.reset(token)

        return drive()

    def create_wrapper(original):
        async def create(serving, request, raw_request=None):
            attempt = (
                raw_request.headers.get("X-Request-Id")
                if raw_request is not None
                else None
            )
            if not isinstance(attempt, str) or not attempt.startswith(
                state.managed_prefix
            ):
                return await original(serving, request, raw_request)
            if state.disabled or state.journal.failed:
                raise RuntimeError(
                    "Terminal observation unavailable for managed requests"
                )
            if request.use_beam_search:
                raise ValueError("Terminal experiment does not support beam search")
            capture = Capture(attempt)
            token = _context.set(capture)
            try:
                return await original(serving, request, raw_request)
            except BaseException:
                if capture.entry is not None:
                    capture.entry.abandon()
                raise
            finally:
                _context.reset(token)

        return create

    def render_wrapper(original, chat):
        async def render(serving, request):
            result = await original(serving, request)
            capture = _context.get()
            inputs = result[1] if chat and isinstance(result, tuple) else result
            if capture is not None and isinstance(inputs, list):
                if capture.entry is not None:
                    raise RuntimeError("Rendered family was already registered")
                capture.entry = EntryCapture(
                    state.journal, capture.attempt, (request.n or 1,) * len(inputs)
                )
            return result

        return render

    try:
        patches.set(frontend, "generate", MethodType(generate, frontend))
        patches.set(frontend, "add_request", MethodType(add, frontend))
        patches.set(
            frontend.output_processor,
            "add_request",
            MethodType(output_add, frontend.output_processor),
        )
        for cls, create_name, render_name, _ in serving_types:
            patches.set(cls, create_name, create_wrapper(getattr(cls, create_name)))
            patches.set(
                cls,
                render_name,
                render_wrapper(getattr(cls, render_name), "chat" in render_name),
            )
    except BaseException:
        patches.restore()
        raise
    return patches


class TerminalEndpoint:
    name = PLUGIN_NAME
    required_tasks = ("generate",)

    def __init__(self):
        self.active = enabled()
        self.state = self.patches = None
        if self.active:
            self.token = os.environ.get("PREFIX_ROUTING_TERMINAL_TOKEN", "")
            if not self.token.isascii() or not 32 <= len(self.token) <= 128:
                raise ValueError("A private bounded terminal control token is required")

    def authorize(self, request):
        from fastapi import HTTPException

        supplied = request.headers.get("X-Prefix-Routing-Control", "")
        if not supplied.isascii() or not hmac.compare_digest(supplied, self.token):
            raise HTTPException(403, "Invalid terminal control credential")
        if self.state is None:
            raise HTTPException(503, "Terminal observation is not initialized")

    def attach_router(self, app):
        if not self.active:
            return
        from fastapi import HTTPException, Request

        async def read(
            request: Request,
            observer_epoch: str | None = None,
            after: int = 0,
            acknowledge: int = 0,
        ):
            self.authorize(request)
            try:
                self.patches.check_current()
                return await self.state.read(observer_epoch, after, acknowledge)
            except ValueError as error:
                status = 503 if self.state.journal.failed else 409
                raise HTTPException(status, str(error)) from error
            except Exception as error:
                raise HTTPException(503, "Terminal observation unavailable") from error

        async def disable(request: Request, dispatcher_stopped: bool = False):
            self.authorize(request)
            if not dispatcher_stopped:
                raise HTTPException(409, "Caller must stop managed dispatch first")
            async with self.state.lock:
                await self.state.poll_core()
                if any(
                    family.record is None
                    for family in self.state.journal.families.values()
                ):
                    raise HTTPException(409, "Unresolved managed families remain")
                self.patches.check_current()
                # Fence managed entry before yielding to the core RPC. A
                # failed/ambiguous disable must not reopen enhanced admission.
                self.state.disabled = True
                try:
                    result = await asyncio.wait_for(
                        self.state.core.call_utility_async(
                            CORE_METHOD,
                            self.state.core_epoch,
                            self.state.cursor,
                            self.state.cursor,
                            True,
                        ),
                        timeout=5,
                    )
                    if (
                        result.get("epoch") != self.state.core_epoch
                        or result.get("cleanup_observer_installed") is not False
                    ):
                        raise RuntimeError("Core observer restoration unconfirmed")
                    self.patches.restore()
                except (Exception, asyncio.CancelledError):
                    self.state.journal.failed = True
                    raise
                return {"restored": True, "binding": result["epoch"]}

        for path, handler, methods in (
            ("/prefix-routing/terminal", read, ["GET"]),
            ("/prefix-routing/terminal/disable", disable, ["POST"]),
        ):
            if any(getattr(route, "path", None) == path for route in app.routes):
                raise RuntimeError("Terminal endpoint route collision")
            app.add_api_route(path, handler, methods=methods, include_in_schema=False)

    async def init_state(self, engine_client, state, args):
        if not self.active:
            return
        verify_installation()
        if args.host != "127.0.0.1" or engine_client is None:
            raise RuntimeError("Terminal experiment requires loopback single frontend")
        config = engine_client.vllm_config
        if (
            config.parallel_config.tensor_parallel_size != 1
            or config.parallel_config.data_parallel_size != 1
            or not config.model_config.enforce_eager
        ):
            raise RuntimeError("Terminal experiment requires TP1/DP1 eager")
        core = engine_client.engine_core
        if type(core).__name__ != "AsyncMPClient":
            raise RuntimeError("Unqualified core client topology")
        verify_callable(
            core.call_utility_async,
            "vllm/v1/engine/core_client.py",
            "AsyncMPClient.call_utility_async",
        )
        bootstrap = await asyncio.wait_for(
            core.call_utility_async(CORE_METHOD, None, 0, 0), timeout=10
        )
        self.state = FrontendEnds(
            core,
            os.environ.get("PREFIX_ROUTING_TERMINAL_WORKER", "replica-a"),
            bootstrap,
        )
        self.state.managed_prefix = os.environ.get(
            "PREFIX_ROUTING_TERMINAL_PREFIX", "prm-"
        )
        if (
            not self.state.managed_prefix.isascii()
            or not 1 <= len(self.state.managed_prefix) <= 32
        ):
            raise ValueError("Bounded explicit managed prefix required")
        from vllm.entrypoints.openai.chat_completion.serving import OpenAIServingChat
        from vllm.entrypoints.openai.completion.serving import OpenAIServingCompletion

        self.patches = attach_frontend(
            engine_client,
            self.state,
            (
                (
                    OpenAIServingCompletion,
                    "_create_completion",
                    "render_completion_request",
                    "vllm/entrypoints/openai/completion/serving.py",
                ),
                (
                    OpenAIServingChat,
                    "_create_chat_completion",
                    "render_chat_request",
                    "vllm/entrypoints/openai/chat_completion/serving.py",
                ),
            ),
        )


def endpoint_plugin():
    return TerminalEndpoint()
