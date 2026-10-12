"""Execute hash-pinned HUST methods with synthetic CPU collaborators.

This is a source-method audit, not an engine, HTTP, device or observer test.
No HUST module is imported, patched or installed. Only selected methods from
the exact audited files are compiled; all device/queue collaborators are fake.
"""

import argparse
import ast
import asyncio
import hashlib
import json
from collections import defaultdict, deque
from enum import IntEnum
from pathlib import Path
from queue import Queue
from types import SimpleNamespace as NS

if __package__:
    from .terminal_observer_prototype import Binding, OnlineConsumer, TerminalJournal
else:
    from terminal_observer_prototype import Binding, OnlineConsumer, TerminalJournal

CORE_COMMIT = "13f11da44455d2f85f00f810a04e1bd2eaf30ba6"
SOURCE_HASHES = {
    "scheduler.py": "95ee1826e098ff63162ff144f0951a90be19ac1278851867530427572a23db0f",
    "core.py": "fdb67615e58cce0e0366050d2002a7ff676444bc78b5db2a4c1837e74d207a00",
    "output_processor.py": (
        "464c3cda347512cf1090f8e6471d0d3f0f65e286eb9a7b97be52efbc1ffd572a"
    ),
    "core_client.py": (
        "4ed1f6bc628b586a3b33aa904fe0ef4b2d2cd767dfb2ec9ebbbaea738ea59eeb"
    ),
}


class Status(IntEnum):
    WAITING = 0
    RUNNING = 1
    WAITING_FOR_STREAMING_REQ = 2
    WAITING_FOR_REMOTE_KVS = 3
    FINISHED_STOPPED = 4
    FINISHED_ABORTED = 5

    @staticmethod
    def is_finished(status):
        return status >= Status.FINISHED_STOPPED


class Request:
    def __init__(self, name, status=Status.RUNNING, last_step=0):
        self.request_id = name
        self.client_index = 0
        self.status = status
        self.last_sched_seq = last_step

    def is_finished(self):
        return Status.is_finished(self.status)


class Waiting(list):
    def remove_requests(self, requests):
        self[:] = [item for item in self if item not in requests]


def load_sources(directory):
    """Reject any byte change before compiling any selected method."""
    sources = {}
    for name, digest in SOURCE_HASHES.items():
        payload = (directory / name).read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError(f"fixed source hash mismatch: {name}")
        sources[name] = ast.parse(payload.decode("utf-8"), filename=name)
    return sources


def source_class(sources, filename, class_name, methods, namespace):
    """Preserve complete method bodies, with postponed annotations only."""
    classes = [
        node
        for node in sources[filename].body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    if len(classes) != 1:
        raise ValueError("source class missing or ambiguous")
    selected = [
        node
        for node in classes[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in methods
    ]
    if {node.name for node in selected} != set(methods):
        raise ValueError("source method set changed")
    if any(node.decorator_list for node in selected):
        raise ValueError("decorated methods require separate audit")
    cls = ast.ClassDef(
        name=class_name, bases=[], keywords=[], body=selected, decorator_list=[]
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            cls,
        ],
        type_ignores=[],
    )
    env = dict(namespace)
    exec(compile(ast.fix_missing_locations(module), filename, "exec"), env)
    return env[class_name]


def run_audit(directory):
    sources = load_sources(directory)
    common = {
        "RequestStatus": Status,
        "remove_all": lambda items, removed: [x for x in items if x not in removed],
        "FinishReason": NS(STOP="stop", ABORT="abort", ERROR="error"),
        "OutputProcessorOutput": NS,
        "EngineCoreRequestType": NS(ABORT="abort"),
    }
    scheduler_type = source_class(
        sources,
        "scheduler.py",
        "Scheduler",
        (
            "finish_requests",
            "_free_request",
            "_free_blocks",
            "_free_request_blocks",
            "_drain_deferred_frees",
        ),
        common,
    )
    core_type = source_class(
        sources, "core.py", "EngineCore", ("abort_requests",), common
    )
    processor_type = source_class(
        sources,
        "output_processor.py",
        "OutputProcessor",
        ("process_outputs", "_finish_request"),
        common,
    )
    client_type = source_class(
        sources, "core_client.py", "AsyncMPClient", ("abort_requests_async",), common
    )
    proc_type = source_class(
        sources,
        "core.py",
        "EngineCoreProc",
        (
            "has_work",
            "_pause_complete",
            "_send_abort_outputs",
            "_send_abort_outputs_to_client",
            "_send_finish_outputs_to_client",
        ),
        {
            **common,
            "defaultdict": defaultdict,
            "EngineCoreOutput": lambda req_id, tokens, **kwargs: NS(
                request_id=req_id, new_token_ids=tokens, **kwargs
            ),
            "EngineCoreOutputs": NS,
        },
    )
    checks = {}

    def scheduler(requests, export=False, observer=None):
        value = scheduler_type()
        value.requests = {r.request_id: r for r in requests}
        value.running = [r for r in requests if r.status == Status.RUNNING]
        value.waiting = Waiting(r for r in requests if r.status != Status.RUNNING)
        value.skipped_waiting = Waiting()
        value.num_waiting_for_streaming_input = 0
        value.finished_recving_kv_req_ids = set()
        value.failed_recving_kv_req_ids = set()
        value._inflight_prefills = set()
        value._connector_finished = lambda request: (False, None)
        value.encoder_cache_manager = NS(free=lambda request: None)
        value.finished_req_ids = set()
        value.finished_req_ids_dict = defaultdict(set) if export else None
        value._cancel_kv_cache_compression_transaction = lambda *args: None
        value._pending_kv_cache_compression_block_table_updates = None
        value.defer_block_free = True
        value.processed_step_seq = 0
        value.sched_step_seq = 1
        value.deferred_frees = deque()
        value.freed = []
        value.kv_cache_manager = NS(
            free=lambda request: value.freed.append(request.request_id),
            pop_blocks_for_free=lambda request: [request.request_id],
            block_pool=NS(free_blocks=lambda blocks: value.freed.extend(blocks)),
        )
        if observer is not None:
            original_cleanup = value._free_request

            def observed_cleanup(request, delay_free_blocks=False):
                result = original_cleanup(request, delay_free_blocks=delay_free_blocks)
                observer.observe_core_end(request.request_id)
                return result

            # Only this synthetic object is wrapped; no imported HUST class,
            # registry, file or running engine is modified.
            value._free_request = observed_cleanup
        return value

    # Normal completion enters _free_request after the scheduler's stop branch.
    # Stop detection and the surrounding update_from_output loop are not executed.
    normal = Request("normal", Status.FINISHED_STOPPED)
    sched = scheduler([normal])
    sched._free_request(normal)
    checks["normal_cleanup_records_finished_id"] = (
        sched.finished_req_ids == {"normal"}
        and not sched.requests
        and sched.freed == ["normal"]
    )
    checks["ordinary_cleanup_has_no_per_client_finished_set"] = (
        sched.finished_req_ids_dict is None
    )

    binding = Binding("fixture-worker", "fixture-instance", "fixture-observer")
    observer = TerminalJournal(binding, max_families=2, max_children=2, max_events=2)
    observer.register("family", ("normal-child", "cancel-child"))
    online_records = []
    consumer = OnlineConsumer(binding, online_records.append)
    consumer.recover(observer)
    normal_child = Request("normal-child", Status.FINISHED_STOPPED)
    cancel_child = Request("cancel-child", last_step=1)
    observed = scheduler([normal_child, cancel_child], observer=observer)
    observed._free_request(normal_child)
    consumer.pump(observer)
    checks["source_cleanup_partial_family_not_delivered"] = not online_records
    observed.finish_requests(["cancel-child"], Status.FINISHED_ABORTED)
    consumer.pump(observer)
    checks["source_cleanup_complete_family_delivered_online"] = (
        len(online_records) == 1
        and online_records[0].attempt_id == "family"
        and len(observed.deferred_frees) == 1
    )

    failing = TerminalJournal(binding, max_families=1, max_children=1, max_events=1)
    failing.register("failed-cleanup", ("bad",))
    failed_request = Request("bad", Status.FINISHED_STOPPED)
    failed_scheduler = scheduler([failed_request], observer=failing)

    def fail_free(request):
        raise RuntimeError("synthetic cleanup failure")

    failed_scheduler.kv_cache_manager.free = fail_free
    try:
        failed_scheduler._free_request(failed_request)
    except RuntimeError:
        checks["failed_cleanup_not_observed_as_terminal"] = failing.next_sequence == 1
    else:
        checks["failed_cleanup_not_observed_as_terminal"] = False

    req = Request("cancel", last_step=1)
    sched = scheduler([req])
    core = core_type()
    core.scheduler = sched
    client = client_type()
    client.resources = NS(engine_dead=False)
    sent = []

    async def send(kind, ids):
        sent.append((kind, tuple(ids)))

    client._send_input = send
    asyncio.run(client.abort_requests_async(["cancel"]))
    checks["abort_send_does_not_execute_core"] = (
        sent == [("abort", ("cancel",))] and "cancel" in sched.requests
    )
    result = core.abort_requests(["cancel"])
    checks["core_abort_discards_returned_ids"] = (
        result is None and sched.finished_req_ids == {"cancel"}
    )
    checks["core_abort_removes_request_before_block_release"] = (
        not sched.requests
        and not sched.running
        and not sched.freed
        and len(sched.deferred_frees) == 1
    )
    sched._drain_deferred_frees()
    checks["pending_fence_prevents_release"] = not sched.freed
    sched.processed_step_seq = 1
    sched._drain_deferred_frees()
    checks["completed_fence_releases_blocks"] = (
        sched.freed == ["cancel"] and not sched.deferred_frees
    )
    checks["duplicate_abort_returns_no_new_confirmation"] = (
        sched.finish_requests(["cancel", "missing"], Status.FINISHED_ABORTED) == []
    )

    # Administrative abort has an explicit output helper; ordinary per-request
    # EngineCore.abort_requests above does not invoke it. Do not conflate them.
    proc = proc_type()
    proc.output_queue = Queue()
    proc._send_abort_outputs([("admin", 0)])
    client_index, batch = proc.output_queue.get_nowait()
    checks["admin_abort_helper_emits_explicit_output"] = (
        client_index == 0
        and batch.finished_requests == ["admin"]
        and batch.outputs[0].finish_reason == "abort"
    )
    proc.scheduler = NS(has_requests=lambda: False)
    proc.engines_running = False
    proc.batch_queue = [object()]
    checks["empty_request_set_not_idle_with_pending_batch"] = not proc._pause_complete()
    proc.batch_queue = []
    checks["no_requests_no_batch_idle"] = proc._pause_complete()
    proc.engines_running = True
    checks["running_engine_not_idle"] = not proc._pause_complete()

    for status in (Status.RUNNING, Status.WAITING):
        sched = scheduler([Request("all", status)], export=True)
        result = sched.finish_requests(None, Status.FINISHED_ABORTED)
        checks[f"finish_all_{status.name.lower()}"] = (
            result == [("all", 0)]
            and not sched.requests
            and not sched.running
            and not sched.waiting
            and sched.finished_req_ids_dict[0] == {"all"}
        )

    # Exercise the complete front-end method with an explicitly fake detokenizer.
    for stop_string, core_finished in ((True, False), (False, True)):
        queue = Queue()
        processor = processor_type()
        state = NS(
            request_id="child",
            external_req_id="parent",
            parent_req=None,
            is_prefilling=False,
            streaming_input=False,
            queue=queue,
            detokenizer=NS(
                update=lambda *args, stop=stop_string: "STOP" if stop else None
            ),
            logprobs_processor=NS(update_from_output=lambda output: None),
            make_request_output=lambda tokens, pool, reason, stop, kv: NS(
                finished=reason is not None
            ),
        )
        processor.request_states = {"child": state}
        processor.external_req_ids = {"parent": {"child"}}
        processor._update_stats_from_output = lambda *args: None
        processor._update_stats_from_finished = lambda *args: None
        processor.tracing_enabled = False
        output = NS(
            request_id="child",
            new_token_ids=[1],
            pooling_output=None,
            finish_reason="stop" if core_finished else None,
            stop_reason=None,
            kv_transfer_params=None,
            routed_experts=None,
            finished=core_finished,
        )
        result = processor.process_outputs([output])
        name = "stop_string" if stop_string else "core_finished"
        checks[f"{name}_frontend_finished"] = queue.get_nowait().finished
        checks[f"{name}_abort_list"] = result.reqs_to_abort == (
            ["child"] if stop_string else []
        )
        checks[f"{name}_mapping_removed"] = (
            not processor.external_req_ids and not processor.request_states
        )
        later = processor.process_outputs([output])
        checks[f"{name}_late_output_ignored"] = (
            not later.request_outputs and not later.reqs_to_abort and queue.empty()
        )

    return {
        "schema_version": 1,
        "status": "pass" if all(checks.values()) else "fail",
        "source_commit": CORE_COMMIT,
        "source_sha256": SOURCE_HASHES,
        "scope": "hash-pinned source methods with synthetic CPU collaborators",
        "checks": checks,
        "engine_started": False,
        "npu_validated": False,
        "observer_installed": False,
        "online_delivery_validated": False,
        "limitations": [
            "No actual scheduler step, detokenizer, IPC, HTTP or shutdown loop",
            "Normal completion starts at cleanup, not model output generation",
            "Synthetic block fence progress is not NPU synchronization evidence",
            "Finished-set export configuration/output loop require source review",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; preserve previous evidence")
    try:
        report = run_audit(args.source_directory)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps(report))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
