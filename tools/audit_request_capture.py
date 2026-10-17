"""Hash-pinned AsyncLLM submission drives bounded, sealed CPU associations.

Actual source ID allocation, parent fanout and submission method bodies run.
Params, EngineCoreRequest, IPC and output collaborators are explicit fakes.
The experiment does not install a hook into an engine or decode HTTP input.
"""

import argparse
import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncGenerator
from copy import copy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace as NS

if __package__:
    from .audit_backend_terminal import SOURCE_HASHES, load_sources, source_class
    from .terminal_observer_prototype import Binding, TerminalJournal
else:
    from audit_backend_terminal import SOURCE_HASHES, load_sources, source_class
    from terminal_observer_prototype import Binding, TerminalJournal

CAPTURE_HASHES = SOURCE_HASHES | {
    "async_llm.py": "ff8f9c021dab4c057886266fe3fa845b35924828ae0c6684d4beb0da66a1e680",
    "input_processor.py": (
        "c5673988c0f7cfec268220e3f044e718702c015a4f236c020937cfd40a793f15"
    ),
    "parallel_sampling.py": (
        "bf3f0a3c6640aaf706d1b6f7ec3a8e09a970f3dd9e7e96fa6a901779ea84f61d"
    ),
}


@dataclass
class Params:
    n: int
    seed: int | None = None
    output_kind: str = "delta"
    prompt_logprobs: bool = False


class CoreRequest:
    def __init__(self, external, params):
        self.request_id = external
        self.external_req_id = None
        self.sampling_params = params

    @property
    def params(self):
        return self.sampling_params


class SyntheticSubmissionFailure(RuntimeError):
    pass


def run_audit(directory):
    sources = load_sources(directory, CAPTURE_HASHES)
    logger = NS(warning_once=lambda *args: None, info=lambda *args: None)
    common = {
        "logger": logger,
        "SamplingParams": Params,
        "PoolingParams": type("Pool", (), {}),
        "EngineCoreRequest": CoreRequest,
        "AsyncGenerator": AsyncGenerator,
        "copy": copy,
        "RequestOutputKind": NS(FINAL_ONLY="final"),
        "cast": lambda _type, value: value,
        "CompletionOutput": object,
        "RequestOutputCollector": lambda *args: NS(),
        "envs": NS(VLLM_DISABLE_REQUEST_ID_RANDOMIZATION=False),
        "random_uuid": lambda: "0123456789abcdef",
    }
    input_type = source_class(
        sources,
        "input_processor.py",
        "InputProcessor",
        ("assign_request_id",),
        common,
        allowed_decorators=("staticmethod",),
    )
    parent_type = source_class(
        sources,
        "parallel_sampling.py",
        "ParentRequest",
        ("__init__", "_get_child_sampling_params", "get_child_info", "n"),
        common,
        allowed_decorators=("property",),
    )
    common["ParentRequest"] = parent_type
    async_type = source_class(
        sources, "async_llm.py", "AsyncLLM", ("add_request", "_add_request"), common
    )
    output_type = source_class(
        sources,
        "output_processor.py",
        "OutputProcessor",
        ("add_request", "_finish_request"),
        {
            "RequestState": NS(
                from_new_request=lambda **kwargs: NS(
                    request_id=kwargs["request"].request_id,
                    external_req_id=kwargs["request"].external_req_id,
                    parent_req=kwargs["parent_req"],
                )
            ),
        },
    )
    checks = {}
    binding = Binding("fixture-worker", "fixture-instance", "capture-1")

    async def scenario(n, failure_at=None, cancel_at=None):
        journal = TerminalJournal(binding, max_families=1, max_children=8, max_events=1)
        frontend = async_type()
        frontend.errored = False
        frontend.vllm_config = NS(cache_config=NS(kv_sharing_fast_prefill=False))
        frontend.input_processor = input_type()
        frontend._run_output_handler = lambda: None
        frontend.log_requests = False
        associations = []
        maps = defaultdict(list)
        output_processor = output_type()
        output_processor.request_states = {}
        output_processor.parent_requests = {}
        output_processor.external_req_ids = maps
        output_processor.tokenizer = None
        output_processor.log_stats = False
        output_processor.stream_interval = 1
        original_add = output_processor.add_request
        submit_count = 0
        checks_for_run = []

        def capture_add(request, prompt, parent, index, queue):
            expected = parent.n if parent is not None else 1
            if not journal.families:
                journal.begin("attempt", expected)
            journal.bind_child("attempt", request.request_id)
            associations.append(
                (request.external_req_id, request.request_id, index, expected)
            )
            original_add(request, prompt, parent, index, queue)

        async def submit(request):
            nonlocal submit_count
            submit_count += 1
            if failure_at == submit_count:
                raise SyntheticSubmissionFailure("synthetic IPC send failure")
            if cancel_at == submit_count:
                raise asyncio.CancelledError
            # Model an earlier child finishing before the next await returns.
            assert journal.observe_core_end(request.request_id)
            output_processor._finish_request(
                output_processor.request_states[request.request_id]
            )
            checks_for_run.append(journal.snapshot(binding)[0] == 0)
            await asyncio.sleep(0)

        output_processor.add_request = capture_add
        frontend.output_processor = output_processor
        frontend.engine_core = NS(add_request_async=submit)
        request = CoreRequest("opaque-external", Params(n))
        try:
            await frontend.add_request("opaque-external", request, request.params)
        except (SyntheticSubmissionFailure, asyncio.CancelledError):
            journal.abandon("attempt")
        else:
            journal.seal("attempt")
        return journal, associations, maps, checks_for_run

    for n in (1, 3):
        journal, associations, maps, before_seal = asyncio.run(scenario(n))
        cut, records = journal.snapshot(binding)
        checks[f"n{n}_native_assignment_captured"] = (
            len(associations) == n
            and all(external == "opaque-external" for external, *_ in associations)
            and len({internal for _, internal, *_ in associations}) == n
            and all(internal != external for external, internal, *_ in associations)
        )
        checks[f"n{n}_native_parent_count_and_index"] = [
            index for _, _, index, _ in associations
        ] == list(range(n)) and all(expected == n for *_, expected in associations)
        checks[f"n{n}_no_end_before_complete_submission"] = all(before_seal)
        checks[f"n{n}_sealed_end_survives_frontend_mapping_removal"] = (
            not maps and cut == 1 and records[0].attempt_id == "attempt"
        )
    journal, associations, _, _ = asyncio.run(scenario(3, failure_at=2))
    checks["partial_send_failure_retains_unsealed_family"] = (
        len(associations) == 2
        and journal.families["attempt"].abandoned
        and not journal.families["attempt"].sealed
        and journal.snapshot(binding) == (0, ())
    )
    journal, _, _, _ = asyncio.run(scenario(3, cancel_at=2))
    checks["submission_task_cancel_retains_unsealed_family"] = journal.families[
        "attempt"
    ].abandoned and journal.snapshot(binding) == (0, ())
    return {
        "schema_version": 1,
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "source_sha256": CAPTURE_HASHES,
        "scope": "complete fixed-source AsyncLLM methods with synthetic collaborators",
        "engine_started": False,
        "observer_installed": False,
        "npu_validated": False,
        "http_multi_prompt_validated": False,
        "cross_process_binding_validated": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; preserve evidence")
    report = run_audit(args.source_directory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps(report))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
