import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

from prefix_routing_mod import hust_terminal as adapter
from prefix_routing_mod.adapters.terminal_guard import PatchSet
from tests.test_terminal_transport import frontend


def test_default_plugin_import_and_loading_are_inert():
    env = os.environ.copy()
    env.pop("PREFIX_ROUTING_HUST_TERMINAL", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from prefix_routing_mod.hust_terminal "
                "import load_plugin,endpoint_plugin; "
                "load_plugin(); p=endpoint_plugin(); assert not p.active; "
                "assert all(n not in sys.modules for n in "
                "('torch','vllm','fastapi'))"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("value", ["", "yes", "2"])
def test_invalid_opt_in_rejected(monkeypatch, value):
    monkeypatch.setenv("PREFIX_ROUTING_HUST_TERMINAL", value)
    with pytest.raises(ValueError):
        adapter.load_plugin()


def test_opt_in_requires_allowlist(monkeypatch):
    monkeypatch.setenv("PREFIX_ROUTING_HUST_TERMINAL", "1")
    monkeypatch.delenv("VLLM_PLUGINS", raising=False)
    with pytest.raises(RuntimeError, match="allowlist"):
        adapter.load_plugin()


def test_restore_preserves_inheritance_and_refuses_foreign_patch():
    class Base:
        def method(self):
            return "original"

    target = Base()
    patches = PatchSet()

    def replacement():
        return "replacement"

    patches.set(target, "method", replacement)
    assert target.method() == "replacement"

    def foreign():
        return "foreign"

    target.method = foreign
    with pytest.raises(RuntimeError, match="Foreign"):
        patches.restore()
    assert target.method is foreign
    target.method = replacement
    patches.restore()
    assert "method" not in vars(target) and target.method() == "original"


def fixture(*, failure=None):
    core, state = frontend()

    class Output:
        def add_request(self, request):
            pass

    class Front:
        def __init__(self):
            self.output_processor = Output()
            self.calls = 0

        async def add_request(self, external, prompt, params):
            for _ in range(params.n):
                self.calls += 1
                request = NS(
                    request_id=f"opaque-{self.calls}", external_req_id=external
                )
                self.output_processor.add_request(request)
                if self.calls == failure:
                    raise RuntimeError("synthetic partial send")
                core.source.observe(request.request_id)
                await asyncio.sleep(0)

        async def generate(self, prompt, params, external):
            await self.add_request(external, prompt, params)
            yield "output"

    front = Front()

    class Serving:
        async def render(self, request):
            return ["first", "second"]

        async def create(self, request, raw_request=None):
            inputs = await self.render(request)
            generators = [
                front.generate(prompt, NS(n=request.n), f"external-{i}")
                for i, prompt in enumerate(inputs)
            ]

            async def consume():
                for generator in generators:
                    async for item in generator:
                        yield item

            if request.stream:
                return consume()
            return [item async for item in consume()]

    patches = adapter.attach_frontend(
        front, state, ((Serving, "create", "render", "fixture"),), guard=lambda *a: None
    )
    return core, state, front, Serving(), patches


@pytest.mark.parametrize("stream", [False, True])
def test_lazy_stream_retains_own_context_and_complete_family(stream):
    core, state, _, serving, patches = fixture()

    async def run():
        try:
            response = await serving.create(
                NS(n=2, stream=stream, use_beam_search=False),
                NS(headers={"X-Request-Id": "prm-a"}),
            )
            if stream:
                assert not state.journal.families["prm-a"].sealed
                assert adapter._context.get() is None
                assert [item async for item in response] == ["output", "output"]
            reply = await state.read(None, 0, 0)
            family = reply["families"][0]
            assert family == {
                "attempt_id": "prm-a",
                "expected": 4,
                "bound": 4,
                "ended": 4,
                "sealed": True,
                "abandoned": False,
            }
            assert reply["boundary"] == 1
        finally:
            patches.restore()

    asyncio.run(run())
    assert not patches.entries and core.source.boundary == 4


def test_partial_send_keeps_unsealed_family():
    _, state, _, serving, patches = fixture(failure=3)

    async def run():
        try:
            with pytest.raises(RuntimeError, match="partial"):
                await serving.create(
                    NS(n=2, stream=False, use_beam_search=False),
                    NS(headers={"X-Request-Id": "prm-a"}),
                )
            reply = await state.read(None, 0, 0)
            assert reply["boundary"] == 0
            family = reply["families"][0]
            assert family["abandoned"] and not family["sealed"]
            assert family["bound"] == 3 and family["ended"] == 2
        finally:
            patches.restore()

    asyncio.run(run())


def test_unmanaged_request_keeps_original_behavior():
    _, state, _, serving, patches = fixture()

    async def run():
        try:
            assert len(await serving.create(NS(n=1, stream=False), NS(headers={}))) == 2
            assert not state.journal.families
        finally:
            patches.restore()

    asyncio.run(run())


def test_core_observer_preserves_cleanup_and_restores_owned_instance(monkeypatch):
    monkeypatch.setattr(adapter, "verify_callable", lambda *a: None)

    class Scheduler:
        connector = ec_connector = None

        def _free_request(self, request):
            if request.fail:
                raise LookupError("original cleanup failure")
            return ("original", None)

    scheduler = Scheduler()
    core = NS(
        scheduler=scheduler,
        vllm_config=NS(
            parallel_config=NS(tensor_parallel_size=1, data_parallel_size=1)
        ),
    )
    bootstrap = adapter._core_poll(core, None, 0, 0)
    assert scheduler._free_request(NS(request_id="a", fail=False)) == ("original", None)
    with pytest.raises(LookupError, match="original"):
        scheduler._free_request(NS(request_id="b", fail=True))
    reply = adapter._core_poll(core, bootstrap["epoch"], 0, 0)
    assert reply["records"] == [{"sequence": 1, "child": "a"}]
    assert (
        adapter._core_poll(core, bootstrap["epoch"], 1, 1, True)[
            "cleanup_observer_installed"
        ]
        is False
    )
    assert "_free_request" not in vars(scheduler)


def test_observer_failure_cannot_raise_into_original_cleanup(monkeypatch):
    monkeypatch.setattr(adapter, "verify_callable", lambda *a: None)

    class Scheduler:
        connector = ec_connector = None

        def _free_request(self, request):
            return "original-result"

    core = NS(
        scheduler=Scheduler(),
        vllm_config=NS(
            parallel_config=NS(tensor_parallel_size=1, data_parallel_size=1)
        ),
    )
    adapter._core_poll(core, None, 0, 0)

    def fail(_):
        raise RuntimeError("observer failure")

    core._prefix_terminal_source.observe = fail
    assert core.scheduler._free_request(NS(request_id="a")) == "original-result"
    assert core._prefix_terminal_source.failed
    core._prefix_terminal_patches.restore()
