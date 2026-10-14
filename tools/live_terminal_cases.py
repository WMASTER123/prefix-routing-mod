"""Controlled real HTTP/core terminal -> existing receipt-owner experiment.

Caller owns the backend process. Request payloads and control credentials are
never written into evidence. This is not a native Router or capacity benchmark.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import suppress

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
from prefix_routing_mod.terminal_journal import Binding, Ended
from tools.durable_ledger_prototype import DurableLedger

MAX_REPLY = 1024 * 1024
MODEL = "prefix-qwen14b-probe"


def request_json(opener, origin, route, *, body=None, headers=None):
    headers = dict(headers or {})
    payload = None if body is None else json.dumps(body).encode()
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(origin + route, data=payload, headers=headers)
    try:
        with opener.open(request, timeout=60) as response:
            raw = response.read(MAX_REPLY + 1)
    except urllib.error.HTTPError as error:
        if route.startswith("/v1/"):
            raw = error.read(MAX_REPLY + 1)
            detail = "unavailable"
            if len(raw) <= MAX_REPLY:
                with suppress(ValueError, AttributeError):
                    detail = str(
                        json.loads(raw).get("error", {}).get("message", "unavailable")
                    )[:512]
            raise RuntimeError(
                f"Controlled HUST request {route}: HTTP {error.code}: {detail}"
            ) from error
        raise
    if len(raw) > MAX_REPLY:
        raise ValueError("Live terminal reply exceeds budget")
    return json.loads(raw)


def run(opener, origin, output, token):
    report = {
        "status": "fail",
        "native_router_verified": False,
        "full_recovery_verified": False,
        "performance_verified": False,
    }
    try:
        return _run(opener, origin, output, token, report)
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        with (output / "terminal-report.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")


def _run(opener, origin, output, token, report):
    headers = {"X-Prefix-Routing-Control": token}
    route = "/prefix-routing/terminal"
    try:
        request_json(opener, origin, route)
    except urllib.error.HTTPError as error:
        if error.code != 403:
            raise
        report["control_auth_rejected"] = True
    else:
        raise ValueError("Unauthenticated terminal control was accepted")
    bootstrap = request_json(opener, origin, route, headers=headers)
    binding = Binding(**bootstrap["binding"])
    if (
        bootstrap["boundary"] != 0
        or bootstrap["records"]
        or bootstrap["core_pid"] == bootstrap["frontend_pid"]
        or bootstrap["frontend_pid"] == os.getpid()
    ):
        raise ValueError("Actual frontend/core process identity differs from expected")
    report["binding"] = bootstrap["binding"]
    report["core_pid"] = bootstrap["core_pid"]
    report["frontend_pid"] = bootstrap["frontend_pid"]
    ref = WorkerRef(binding.worker_id, binding.instance_epoch)
    clock = "live-probe-" + uuid.uuid4().hex

    def context():
        return ReadContext(clock, time.monotonic_ns(), 600_000_000_000)

    now = context().now_ns
    worker = WorkerState(
        ref,
        "live-probe-domain",
        0,
        Health.HEALTHY,
        False,
        clock,
        now,
        Observation(None, "controlled_health_probe", "whole_instance", clock, now),
        Observation(0, "local_ledger", "router_session", clock, now),
    )
    path = output / "terminal-receipts.sqlite"
    cursor = 0
    outcomes = {}
    completed = []
    report["cases"] = completed

    def control(after, acknowledge):
        query = urllib.parse.urlencode(
            {
                "observer_epoch": binding.observer_epoch,
                "after": after,
                "acknowledge": acknowledge,
            }
        )
        return request_json(opener, origin, route + "?" + query, headers=headers)

    def reserve(store, attempt):
        features = RequestFeatures(
            attempt, worker.domain, None, None, None, None, Precision.UNKNOWN
        )
        decision = RouteDecision(
            attempt,
            attempt,
            attempt,
            ref,
            0,
            "live-probe",
            "config-1",
            "controlled_single_backend",
        )
        store.reserve(features, decision, context())
        store.notify_transport(attempt, EventKind.SEND_STARTED, context())

    def settle(store, attempt, expected):
        nonlocal cursor
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            reply = control(cursor, cursor)
            if Binding(**reply["binding"]) != binding:
                raise ValueError("Terminal observer changed during a request")
            records = reply["records"]
            if len(records) > 64 or reply["mode"] != "replay":
                raise ValueError("Unexpected controlled replay scope")
            for sequence, raw in enumerate(records, cursor + 1):
                record = Ended(
                    Binding(**raw["binding"]), raw["sequence"], raw["attempt_id"]
                )
                if (
                    record.binding != binding
                    or record.sequence != sequence
                    or record.attempt_id not in outcomes
                ):
                    raise ValueError("Terminal identity or sequence mismatch")
                if (
                    store.confirm_end(record, outcomes[record.attempt_id], context())
                    != "applied"
                ):
                    raise ValueError("First terminal receipt was not applied")
            if records:
                if records[-1]["attempt_id"] != attempt:
                    raise ValueError("Controlled attempt was not the terminal tail")
                family = next(
                    item for item in reply["families"] if item["attempt_id"] == attempt
                )
                if not (
                    family["expected"] == family["bound"] == family["ended"] == expected
                    and family["sealed"]
                    and not family["abandoned"]
                ):
                    raise ValueError("Actual HUST family capture incomplete")
                if store.outstanding(ref) != 0:
                    raise ValueError("Positive end did not settle the existing owner")
                # Model a lost delivery reply/ACK by fetching the SAME record again.
                repeated = control(cursor, cursor)
                if repeated["records"] != records:
                    raise ValueError("Unacknowledged terminal replay changed")
                for raw in repeated["records"]:
                    record = Ended(
                        Binding(**raw["binding"]), raw["sequence"], raw["attempt_id"]
                    )
                    if (
                        store.confirm_end(
                            record, outcomes[record.attempt_id], context()
                        )
                        != "duplicate"
                    ):
                        raise ValueError("Repeated terminal was not idempotent")
                cursor = records[-1]["sequence"]
                control(cursor, cursor)
                return family
            time.sleep(0.05)
        raise TimeoutError("Actual core terminal did not arrive")

    with DurableLedger(
        path,
        create=True,
        workers=(worker,),
        capacity=16,
        per_worker_limit=1,
        max_operations=64,
    ) as store:
        for name, endpoint, body, expected in (
            (
                "completion",
                "/v1/completions",
                {"prompt": ["List two fruits.", "List two colors."], "n": 2},
                4,
            ),
            (
                "chat",
                "/v1/chat/completions",
                {"messages": [{"role": "user", "content": "Name two fruits."}], "n": 2},
                2,
            ),
        ):
            attempt = "prm-" + name + "-" + uuid.uuid4().hex[:12]
            outcomes[attempt] = EventKind.COMPLETED
            reserve(store, attempt)
            result = request_json(
                opener,
                origin,
                endpoint,
                body={"model": MODEL, "max_tokens": 8, "temperature": 0.7, **body},
                headers={"X-Request-Id": attempt},
            )
            if len(result.get("choices", [])) != expected or any(
                choice.get("finish_reason") is None for choice in result["choices"]
            ):
                raise ValueError("Real HTTP family did not complete")
            if store.outstanding(ref) != 1:
                raise ValueError("HTTP response must not itself release debt")
            completed.append({"case": name, "family": settle(store, attempt, expected)})

        seed = request_json(
            opener,
            origin,
            "/v1/completions",
            body={
                "model": MODEL,
                "prompt": "Count from one to twenty in English.",
                "temperature": 0,
                "max_tokens": 24,
                "ignore_eos": True,
            },
        )
        text = seed["choices"][0]["text"]
        if len(text) < 4:
            raise ValueError("Stop-string seed is too short")
        attempt = "prm-stop-" + uuid.uuid4().hex[:12]
        outcomes[attempt] = EventKind.COMPLETED
        reserve(store, attempt)
        result = request_json(
            opener,
            origin,
            "/v1/completions",
            body={
                "model": MODEL,
                "prompt": "Count from one to twenty in English.",
                "temperature": 0,
                "max_tokens": 64,
                "ignore_eos": True,
                "stop": [text[:4]],
            },
            headers={"X-Request-Id": attempt},
        )
        if (
            result["choices"][0]["finish_reason"] != "stop"
            or store.outstanding(ref) != 1
        ):
            raise ValueError("Actual stop-string response differs")
        completed.append({"case": "stop_string", "family": settle(store, attempt, 1)})

        attempt = "prm-cancel-" + uuid.uuid4().hex[:12]
        outcomes[attempt] = EventKind.CANCEL_CONFIRMED
        reserve(store, attempt)
        request = urllib.request.Request(
            origin + "/v1/completions",
            data=json.dumps(
                {
                    "model": MODEL,
                    "prompt": "Write a long numbered list of interesting facts.",
                    "temperature": 0,
                    "max_tokens": 512,
                    "ignore_eos": True,
                    "stream": True,
                }
            ).encode(),
            headers={"Content-Type": "application/json", "X-Request-Id": attempt},
        )
        with opener.open(request, timeout=60) as response:
            for _ in range(64):
                line = response.readline(65537)
                if len(line) > 65536:
                    raise ValueError("SSE line exceeds budget")
                if line.startswith(b"data: ") and b"[DONE]" not in line:
                    event = json.loads(line[6:])
                    if any(choice.get("text") for choice in event.get("choices", [])):
                        break
            else:
                raise ValueError("No real streaming output before cancellation")
        store.notify_transport(attempt, EventKind.OUTCOME_UNKNOWN, context())
        if store.outstanding(ref) != 1:
            raise ValueError("Client disconnect released debt")
        completed.append(
            {"case": "stream_disconnect", "family": settle(store, attempt, 1)}
        )
        operations = store.operation_count
    with DurableLedger(path) as store:
        recovered = control(0, 0)
        if recovered["mode"] != "snapshot" or recovered["boundary"] != cursor:
            raise ValueError("Positive retained recovery differs")
        for raw in recovered["records"]:
            record = Ended(
                Binding(**raw["binding"]), raw["sequence"], raw["attempt_id"]
            )
            if (
                store.confirm_end(record, outcomes[record.attempt_id], context())
                != "duplicate"
            ):
                raise ValueError("Reopened ledger duplicated terminal transitions")
        if (
            store.outstanding(ref) != 0
            or store.operation_count != operations
            or store.admission_ready
        ):
            raise ValueError("Reopened ledger count or admission state differs")
    disabled = request_json(
        opener,
        origin,
        route + "/disable?dispatcher_stopped=true",
        body={},
        headers=headers,
    )
    if disabled.get("restored") is not True:
        raise ValueError("Observer restoration failed")
    request_json(
        opener,
        origin,
        "/v1/completions",
        body={"model": MODEL, "prompt": "Say hello.", "max_tokens": 4},
        headers={"X-Request-Id": "prm-after-disable"},
    )
    final = control(cursor, cursor)
    if not final["disabled"] or len(final["families"]) != len(completed):
        raise ValueError("Disabled native path still captured managed traffic")
    report.update(
        status="pass",
        cases=completed,
        terminal_count=cursor,
        receipt_operations=operations,
        independent_http_core_processes=True,
        actual_http_frontend_verified=True,
        same_existing_ledger_verified=True,
        client_disconnect_retains_debt=True,
        retained_positive_recovery_verified=True,
        owned_restoration_verified=True,
        finite_retention=True,
    )
    return report
