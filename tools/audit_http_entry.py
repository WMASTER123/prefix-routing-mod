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
from contextlib import contextmanager
from copy import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS

from prefix_routing_mod.contracts import (
    EventKind,
    Health,
    Observation,
    Precision,
    ReadContext,
    RequestFeatures,
    RouteDecision,
    WorkerRef,
    WorkerState,
)
from tools.audit_backend_terminal import (
    Request,
    Status,
    load_sources,
    output_fixture,
    scheduler_class,
    scheduler_fixture,
    source_class,
)
from tools.audit_request_capture import CAPTURE_HASHES, CoreRequest, Params
from tools.durable_ledger_prototype import DurableLedger
from tools.entry_capture_prototype import EntryCapture
from tools.hust_source_profiles import (
    CED_CAPTURE_HASHES,
    CED_ENTRY_HASHES,
    is_ced_profile,
)
from tools.terminal_observer_prototype import Binding, OnlineConsumer, TerminalJournal

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


@contextmanager
def source_ledger(binding):
    """Existing receipt owner with explicitly synthetic health/clock/outcome."""
    ref = WorkerRef(binding.worker_id, binding.instance_epoch)
    context = ReadContext("source-fixture-clock", 100, 50)
    worker = WorkerState(
        ref,
        "fixture-domain",
        0,
        Health.HEALTHY,
        False,
        context.clock_id,
        90,
        Observation(None, "fixture", "instance", context.clock_id, 90),
        Observation(0, "local_ledger", "router_session", context.clock_id, 90),
    )
    features = RequestFeatures(
        "fixture-request", worker.domain, None, None, None, None, Precision.UNKNOWN
    )
    decision = RouteDecision(
        "fixture-decision",
        features.request_id,
        "router-attempt",
        ref,
        0,
        "fixture-policy",
        "config-1",
        "fixture_only",
    )
    with (
        TemporaryDirectory(prefix="prefix-source-chain-") as directory,
        DurableLedger(
            Path(directory) / "receipts.sqlite",
            create=True,
            workers=(worker,),
            capacity=1,
            per_worker_limit=1,
            max_operations=16,
        ) as store,
    ):
        store.reserve(features, decision, context)
        store.notify_transport("router-attempt", EventKind.SEND_STARTED, context)
        yield store, context, ref


def terminal_ledger_checks(sources, newer):
    """Source abort/cleanup -> retained binding -> existing durable owner.

    IPC, detokenization and business results are explicit collaborators. No
    engine class or live backend is patched. Lost ACK is injected after commit.
    """
    namespace = {
        "RequestStatus": Status,
        "FinishReason": NS(STOP="stop", ABORT="abort", ERROR="error"),
        "OutputProcessorOutput": NS,
        "EngineCoreRequestType": NS(ABORT="abort"),
    }
    scheduler_type = scheduler_class(sources, newer)
    core_type = source_class(
        sources, "core.py", "EngineCore", ("abort_requests",), namespace
    )
    client_type = source_class(
        sources, "core_client.py", "AsyncMPClient", ("abort_requests_async",), namespace
    )
    processor_type = source_class(
        sources,
        "output_processor.py",
        "OutputProcessor",
        ("process_outputs", "_finish_request")
        + (("_update_admission_stats",) if newer else ()),
        namespace,
    )
    checks = {}
    for mode in ("normal", "stop_string", "cancel", "cleanup_failure"):
        binding = Binding("worker", "instance", "observer")
        journal = TerminalJournal(binding, max_families=1, max_children=1, max_events=1)
        entry = EntryCapture(journal, "router-attempt", (1,))
        entry.register_group("parent", 1)
        entry.bind("parent", "child")
        entry.submitted("parent")
        request = Request("child", last_step=1)
        sched = scheduler_fixture(scheduler_type, (request,), observer=journal)
        core = core_type()
        core.scheduler = sched
        client = client_type()
        client.resources = NS(engine_dead=False)
        sent = []

        async def send(kind, ids, *, sent=sent):
            sent.append((kind, tuple(ids)))

        client._send_input = send
        with source_ledger(binding) as (store, context, ref):
            kind = (
                EventKind.CANCEL_CONFIRMED if mode == "cancel" else EventKind.COMPLETED
            )
            consumer = OnlineConsumer(
                binding,
                lambda record, store=store, kind=kind, context=context: (
                    store.confirm_end(record, kind, context)
                ),
            )
            consumer.recover(journal)
            if mode == "normal":
                request.status = Status.FINISHED_STOPPED
                sched._free_request(request)
            elif mode == "stop_string":
                processor, queue, output = output_fixture(
                    processor_type, stop_string=True, core_finished=False
                )
                result = processor.process_outputs([output])
                checks["stop_string_mapping_removed_but_debt_retained"] = (
                    queue.get_nowait().finished
                    and not processor.external_req_ids
                    and result.reqs_to_abort == ["child"]
                    and store.outstanding(ref) == 1
                    and journal.snapshot(binding) == (0, ())
                )
            if mode != "normal":
                asyncio.run(client.abort_requests_async(["child"]))
                consumer.pump(journal)
                checks[mode + "_abort_send_retains_debt"] = (
                    sent == [("abort", ("child",))]
                    and store.outstanding(ref) == 1
                    and journal.snapshot(binding) == (0, ())
                )
                if mode == "cleanup_failure":

                    def fail_free(_):
                        raise SyntheticFailure("synthetic core cleanup failure")

                    sched.encoder_cache_manager.free = fail_free
                    try:
                        core.abort_requests(["child"])
                    except SyntheticFailure:
                        consumer.pump(journal)
                        checks["cleanup_failure_cannot_release_ledger"] = (
                            store.outstanding(ref) == 1
                            and journal.snapshot(binding) == (0, ())
                        )
                    else:
                        checks["cleanup_failure_cannot_release_ledger"] = False
                    continue
                core.abort_requests(["child"])
            if mode == "cancel":
                original_acknowledge = journal.acknowledge

                def lose_ack(*args):
                    raise ConnectionError("synthetic post-commit ACK loss")

                journal.acknowledge = lose_ack
                try:
                    consumer.pump(journal)
                except ConnectionError:
                    checks["cancel_commit_before_ack_retains_replay"] = (
                        store.outstanding(ref) == 0
                        and journal.acknowledged == 0
                        and consumer.requires_recovery
                    )
                else:
                    checks["cancel_commit_before_ack_retains_replay"] = False
                journal.acknowledge = original_acknowledge
            else:
                consumer.pump(journal)
            checks[mode + "_positive_end_releases_once"] = (
                store.outstanding(ref) == 0 and store.operation_count == 3
            )
            # Retained child association survives frontend mapping removal.
            # Re-delivery after a committed receipt must not allocate a new event.
            consumer.disconnect()
            consumer.recover(journal)
            checks[mode + "_recovery_idempotent"] = store.operation_count == 3
    return checks


def run_audit(source_directory, entry_directory, *, profile="hust-legacy-publisher-v1"):
    newer = is_ced_profile(profile)
    capture_hashes = CED_CAPTURE_HASHES if newer else CAPTURE_HASHES
    entry_hashes = CED_ENTRY_HASHES if newer else ENTRY_HASHES
    sources = load_sources(source_directory, capture_hashes)
    sources.update(load_sources(entry_directory, entry_hashes))
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
        "VLLMClientError": type("ClientError", (Exception,), {}),
        "GracefulHTTPError": type("HTTPError", (Exception,), {}),
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
        "engine_serving.py" if newer else "base_serving.py",
        "BaseServing",
        ("_base_request_id",),
        common,
        allowed_decorators=("staticmethod",),
    )
    output_type = source_class(
        sources,
        "output_processor.py",
        "OutputProcessor",
        ("add_request", "_finish_request")
        + (("_update_admission_stats",) if newer else ()),
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
    core_scheduler = scheduler_class(sources, newer)

    async def execute(
        journal,
        store,
        context,
        ref,
        route,
        groups,
        n,
        stream=False,
        fail_at=None,
        cancel_at=None,
        cleanup_at=None,
        deny_at=None,
    ):
        entry = None
        count = 0
        early_ended = []
        admissions = 0
        triggered_fault = None
        consumer = OnlineConsumer(
            journal.binding,
            # This test supplies the known synthetic business outcome. Generic
            # core ENDED alone does not establish a real request's success.
            lambda record: store.confirm_end(record, EventKind.COMPLETED, context),
        )
        consumer.recover(journal)
        scheduler = scheduler_fixture(core_scheduler, (), observer=journal)
        frontend = async_type()
        frontend.errored = False
        frontend.log_requests = False
        frontend.model_config = NS()
        frontend.vllm_config = NS(cache_config=NS(kv_sharing_fast_prefill=False))
        frontend._run_output_handler = lambda: None

        def check_admission(*args, **kwargs):
            nonlocal admissions, triggered_fault
            admissions += 1
            if admissions == deny_at:
                triggered_fault = "admission"
                raise common["GracefulHTTPError"]("synthetic backend admission denial")

        frontend.check_admission = check_admission
        frontend.input_processor = input_type()

        async def supported():
            return ("generate",)

        frontend.get_supported_tasks = supported
        frontend.input_processor.process_inputs = (
            lambda request_id, prompt, params, **k: CoreRequest(request_id, params)
        )

        async def process_inputs_async(*args, **kwargs):
            return frontend.input_processor.process_inputs(*args, **kwargs)

        frontend.input_processor.process_inputs_async = process_inputs_async
        out = output_type()
        out.request_states, out.parent_requests = {}, {}
        out.external_req_ids = defaultdict(list)
        out.tokenizer, out.log_stats, out.stream_interval = None, False, 1
        out.admission_stats = None
        out.has_request = lambda request_id: request_id in out.request_states
        original_output_add = out.add_request

        def capture_child(request, prompt, parent, index, queue):
            entry.bind(request.external_req_id, request.request_id)
            original_output_add(request, prompt, parent, index, queue)

        out.add_request = capture_child
        frontend.output_processor = out

        async def submit(request):
            nonlocal count, triggered_fault
            count += 1
            if count == fail_at:
                triggered_fault = "submission"
                raise SyntheticFailure("synthetic partial IPC failure")
            if count == cancel_at:
                triggered_fault = "cancellation"
                raise asyncio.CancelledError
            core_request = Request(request.request_id, Status.FINISHED_STOPPED)
            scheduler.requests[request.request_id] = core_request
            if count == cleanup_at:

                def fail_free(request):
                    nonlocal triggered_fault
                    triggered_fault = "cleanup"
                    raise SyntheticFailure("synthetic allocator cleanup failure")

                scheduler.kv_cache_manager.free = fail_free
            scheduler._free_request(core_request)
            consumer.pump(journal)
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
            consumer.pump(journal)
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
            inputs = [
                {"type": "token", "prompt_token_ids": [1, 2]} for _ in range(groups)
            ]
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
        serving._get_session_id = lambda *a: None
        serving._get_priority = lambda request, raw: request.priority
        serving._preflight = lambda *a: None
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
        except (Exception, asyncio.CancelledError) as error:
            cause = error
            while cause.__cause__ is not None:
                cause = cause.__cause__
            if triggered_fault is None or not isinstance(
                cause,
                (SyntheticFailure, asyncio.CancelledError, common["GracefulHTTPError"]),
            ):
                raise
            entry.abandon()
        consumer.pump(journal)
        return (
            entry,
            count,
            early_ended,
            out.external_req_ids,
            raw,
            store.outstanding(ref),
            triggered_fault,
        )

    async def scenario(*args, **kwargs):
        journal = TerminalJournal(
            Binding("worker", "instance", "observer"),
            max_families=1,
            max_children=16,
            max_events=1,
        )
        with source_ledger(journal.binding) as (store, context, ref):
            return await execute(journal, store, context, ref, *args, **kwargs)

    for route, groups, n, stream in (
        ("completion", 2, 3, False),
        ("completion", 2, 2, True),
        ("completion", 1, 1, False),
        ("chat", 1, 3, False),
        ("chat", 1, 2, True),
    ):
        entry, count, early, maps, raw, outstanding, fault = asyncio.run(
            scenario(route, groups, n, stream)
        )
        key = f"{route}_groups{groups}_n{n}_stream{stream}"
        checks[key + "_complete"] = (
            count == groups * n
            and len(entry.groups) == groups
            and len(entry.journal.owners) == count
            and all(early)
            and not maps
            and entry.journal.snapshot(entry.journal.binding)[0] == 1
            and outstanding == 0
            and fault is None
        )
        checks[key + "_header_carrier"] = (
            "router-owned-id" in raw.state.request_metadata.request_id
            and all("router-owned-id" in value for value in entry.groups)
        )
    for failure in ({"fail_at": 4}, {"cancel_at": 4}, {"cleanup_at": 4}):
        entry, count, _, _, _, outstanding, fault = asyncio.run(
            scenario("completion", 2, 3, **failure)
        )
        checks[next(iter(failure)) + "_retains_whole_family"] = (
            count == 4
            and entry.abandoned
            and entry.journal.snapshot(entry.journal.binding) == (0, ())
            and outstanding == 1
            and fault
            == {
                "fail_at": "submission",
                "cancel_at": "cancellation",
                "cleanup_at": "cleanup",
            }[next(iter(failure))]
        )
    if newer:
        entry, count, _, _, _, outstanding, fault = asyncio.run(
            scenario("completion", 2, 3, deny_at=2)
        )
        checks["second_group_admission_denial_retains_debt"] = (
            count == 3
            and entry.abandoned
            and outstanding == 1
            and entry.journal.snapshot(entry.journal.binding) == (0, ())
            and fault == "admission"
        )
    checks.update(terminal_ledger_checks(sources, newer))
    return {
        "schema_version": 1,
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "source_sha256": capture_hashes | entry_hashes,
        "profile_id": profile,
        "source_methods_to_existing_ledger_verified": all(checks.values()),
        "outcome_health_clock_and_ipc_are_synthetic": True,
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
    parser.add_argument("--profile", default="hust-legacy-publisher-v1")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; preserve evidence")
    report = run_audit(
        args.source_directory, args.entry_directory, profile=args.profile
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps(report))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
