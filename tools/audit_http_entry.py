"""Run complete fixed HUST HTTP-serving and AsyncLLM method bodies on CPU.

Not an HTTP server or installed hook: renderer, validation, params, output,
IPC and core cleanup are synthetic. Source guards precede any execution.
"""

import argparse
import asyncio
import json
import time
from collections import defaultdict
from collections.abc import AsyncGenerator
from copy import copy
from pathlib import Path
from types import SimpleNamespace as NS

from tools.audit_backend_terminal import load_sources, source_class
from tools.audit_request_capture import CAPTURE_HASHES, CoreRequest, Params
from tools.entry_capture_prototype import EntryCapture
from tools.terminal_observer_prototype import Binding, TerminalJournal

ENTRY_HASHES = {
    "base_serving.py": (
        "7d1509fc31f8ed2eddeaddf3a51ba0b456db2dbac5e649bc91922f9fe7d44cee"
    ),
    "completion_serving.py": (
        "96d7fbf0feddc3e93da8a4ac3bafbdd941ba7ba42529f757c998693f1b5e3f38"
    ),
    "chat_serving.py": (
        "6083723e3509a9a09ed6509151ab2beb3f6cbbe543548de0b26a74f72f15d1f5"
    ),
}


class Output:
    finished = True
    prompt = "synthetic"


class Collector:
    def __init__(self, kind, request_id):
        self.request_id = request_id

    def get_nowait(self):
        return Output()

    def close(self):
        pass


class SyntheticFailure(RuntimeError):
    pass


async def merge(*generators):
    # Sequential consumption deliberately permits a whole group to end before
    # another group submits. This collaborator is not HUST's iterator merger.
    for index, generator in enumerate(generators):
        async for output in generator:
            yield index, output


def run_audit(source_directory, entry_directory):
    sources = load_sources(source_directory, CAPTURE_HASHES)
    sources.update(load_sources(entry_directory, ENTRY_HASHES))
    common = {
        "asyncio": asyncio,
        "time": time,
        "logger": NS(warning_once=lambda *a: None, info=lambda *a: None),
        "SamplingParams": Params,
        "BeamSearchParams": type("Beam", (), {}),
        "PoolingParams": type("Pool", (), {}),
        "EngineCoreRequest": CoreRequest,
        "AsyncGenerator": AsyncGenerator,
        "RequestOutput": Output,
        "RequestOutputCollector": Collector,
        "STREAM_FINISHED": object(),
        "EngineDeadError": type("Dead", (Exception,), {}),
        "EngineGenerateError": type("GenerateError", (Exception,), {}),
        "InputStreamError": type("InputError", (Exception,), {}),
        "ErrorResponse": type("ErrorResponse", (), {}),
        "RequestResponseMetadata": lambda **kwargs: NS(**kwargs),
        "RequestOutputKind": NS(FINAL_ONLY="final"),
        "CompletionOutput": object,
        "copy": copy,
        "cast": lambda _type, value: value,
        "envs": NS(VLLM_DISABLE_REQUEST_ID_RANDOMIZATION=False),
        "random_uuid": lambda: "0123456789abcdef",
        "extract_prompt_components": lambda *a: ("synthetic", [1, 2], None),
        "get_max_tokens": lambda *a, **k: 4,
        "_get_mm_token_counts": lambda *a: None,
        "merge_async_iterators": merge,
    }
    input_type = source_class(
        sources,
        "input_processor.py",
        "InputProcessor",
        ("assign_request_id",),
        common,
        allowed_decorators=("staticmethod",),
    )
    common["ParentRequest"] = source_class(
        sources,
        "parallel_sampling.py",
        "ParentRequest",
        ("__init__", "_get_child_sampling_params", "get_child_info", "n"),
        common,
        allowed_decorators=("property",),
    )
    async_type = source_class(
        sources,
        "async_llm.py",
        "AsyncLLM",
        ("add_request", "_add_request", "generate"),
        common,
    )
    base_type = source_class(
        sources,
        "base_serving.py",
        "BaseServing",
        ("_base_request_id",),
        common,
        allowed_decorators=("staticmethod",),
    )
    output_type = source_class(
        sources,
        "output_processor.py",
        "OutputProcessor",
        ("add_request", "_finish_request"),
        {
            "RequestState": NS(
                from_new_request=lambda **k: NS(
                    request_id=k["request"].request_id,
                    external_req_id=k["request"].external_req_id,
                    parent_req=k["parent_req"],
                )
            )
        },
    )
    serving_types = {
        "completion": source_class(
            sources,
            "completion_serving.py",
            "OpenAIServingCompletion",
            ("render_completion_request", "_create_completion"),
            common,
        ),
        "chat": source_class(
            sources,
            "chat_serving.py",
            "OpenAIServingChat",
            ("render_chat_request", "_create_chat_completion"),
            common,
        ),
    }
    checks = {}

    async def scenario(route, groups, n, stream=False, fail_at=None, cancel_at=None):
        journal = TerminalJournal(
            Binding("worker", "instance", "observer"),
            max_families=1,
            max_children=16,
            max_events=1,
        )
        entry = None
        count = 0
        early_ended = []
        frontend = async_type()
        frontend.errored = False
        frontend.log_requests = False
        frontend.model_config = NS()
        frontend.vllm_config = NS(cache_config=NS(kv_sharing_fast_prefill=False))
        frontend._run_output_handler = lambda: None
        frontend.input_processor = input_type()

        async def supported():
            return ("generate",)

        frontend.get_supported_tasks = supported
        frontend.input_processor.process_inputs = (
            lambda request_id, prompt, params, **k: CoreRequest(request_id, params)
        )
        out = output_type()
        out.request_states, out.parent_requests = {}, {}
        out.external_req_ids = defaultdict(list)
        out.tokenizer, out.log_stats, out.stream_interval = None, False, 1
        original_output_add = out.add_request

        def capture_child(request, prompt, parent, index, queue):
            entry.bind(request.external_req_id, request.request_id)
            original_output_add(request, prompt, parent, index, queue)

        out.add_request = capture_child
        frontend.output_processor = out

        async def submit(request):
            nonlocal count
            count += 1
            if count == fail_at:
                raise SyntheticFailure("synthetic partial IPC failure")
            if count == cancel_at:
                raise asyncio.CancelledError
            assert journal.observe_core_end(request.request_id)
            out._finish_request(out.request_states[request.request_id])
            early_ended.append(journal.snapshot(journal.binding)[0] == 0)
            await asyncio.sleep(0)

        frontend.engine_core = NS(add_request_async=submit)
        original_add = frontend.add_request

        async def add(request_id, *args, **kwargs):
            try:
                result = await original_add(request_id, *args, **kwargs)
            except BaseException:
                entry.abandon()
                raise
            entry.submitted(request_id)
            return result

        frontend.add_request = add

        def generate(prompt, params, external, **kwargs):
            entry.register_group(external, params.n)
            return frontend.generate(prompt, params, external, **kwargs)

        serving = serving_types[route]()
        serving.engine_client = NS(errored=False, generate=generate)

        async def render(request):
            nonlocal entry
            entry = EntryCapture(journal, "router-attempt", (request.n,) * groups)
            inputs = [{"prompt_token_ids": [1, 2]} for _ in range(groups)]
            return ([], inputs) if route == "chat" else inputs

        async def valid(request):
            return None

        async def traces(headers):
            return None

        async def consume_chat(request, generator, *args, **kwargs):
            async for _ in generator:
                pass
            return NS()

        async def consume_stream(request, *args, **kwargs):
            generator = args[0] if route == "chat" else args[1]
            async for _ in generator:
                yield "synthetic-output"

        serving.online_renderer = NS(render_completion=render, render_chat=render)
        serving._check_model = valid
        serving._base_request_id = base_type._base_request_id
        serving._maybe_get_adapters = lambda *a, **k: None
        serving._get_data_parallel_rank = lambda *a: None
        serving._get_trace_headers = traces
        serving._log_inputs = lambda *a, **k: None
        serving.model_config = NS(max_model_len=32)
        serving.default_sampling_params, serving.override_max_tokens = {}, None
        serving.models = NS(model_name=lambda *a: "fixture-model")
        serving.renderer = NS(tokenizer=NS())
        serving.parser_cls = None
        serving._effective_chat_template_kwargs = lambda *a: {}
        serving._extract_prompt_components = lambda *a: NS(token_ids=[1, 2])
        serving._extract_prompt_len = lambda *a: 2
        serving.request_output_to_completion_response = lambda *a: NS()
        serving.chat_completion_full_generator = consume_chat
        serving.completion_stream_generator = consume_stream
        serving.chat_completion_stream_generator = consume_stream
        serving.create_error_response = lambda *a: NS(error=True)
        request = NS(
            n=n,
            stream=stream,
            use_beam_search=False,
            request_id="untrusted-body",
            max_tokens=4,
            max_completion_tokens=None,
            truncate_prompt_tokens=None,
            priority=0,
            include_reasoning=False,
            to_sampling_params=lambda *a: Params(n),
        )
        raw = NS(headers={"X-Request-Id": "router-owned-id"}, state=NS())
        entrypoint = (
            serving._create_completion
            if route == "completion"
            else serving._create_chat_completion
        )
        try:
            response = await entrypoint(request, raw)
            if stream:
                async for _ in response:
                    pass
        except (Exception, asyncio.CancelledError):
            if fail_at is None and cancel_at is None:
                raise
            entry.abandon()
        return entry, count, early_ended, out.external_req_ids, raw

    for route, groups, n, stream in (
        ("completion", 2, 3, False),
        ("completion", 2, 2, True),
        ("completion", 1, 1, False),
        ("chat", 1, 3, False),
        ("chat", 1, 2, True),
    ):
        entry, count, early, maps, raw = asyncio.run(scenario(route, groups, n, stream))
        key = f"{route}_groups{groups}_n{n}_stream{stream}"
        checks[key + "_complete"] = (
            count == groups * n
            and len(entry.groups) == groups
            and len(entry.journal.owners) == count
            and all(early)
            and not maps
            and entry.journal.snapshot(entry.journal.binding)[0] == 1
        )
        checks[key + "_header_carrier"] = (
            "router-owned-id" in raw.state.request_metadata.request_id
            and all("router-owned-id" in value for value in entry.groups)
        )
    for failure in ({"fail_at": 4}, {"cancel_at": 4}):
        entry, count, _, _, _ = asyncio.run(scenario("completion", 2, 3, **failure))
        checks[next(iter(failure)) + "_retains_whole_family"] = (
            count == 4
            and entry.abandoned
            and entry.journal.snapshot(entry.journal.binding) == (0, ())
        )
    return {
        "schema_version": 1,
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "source_sha256": CAPTURE_HASHES | ENTRY_HASHES,
        "scope": "fixed HTTP serving and normal AsyncLLM branch; fake collaborators",
        "network_http_validated": False,
        "observer_installed": False,
        "npu_validated": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-directory", required=True, type=Path)
    parser.add_argument("--entry-directory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; preserve evidence")
    report = run_audit(args.source_directory, args.entry_directory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps(report))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
