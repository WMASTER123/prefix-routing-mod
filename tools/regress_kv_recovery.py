"""Bounded real-HUST disconnect/replay/restart regression on one NPU.

Uses supervised empty origins and exact replay coverage. Native snapshots stay
unknown; this does not validate a production directory or complete recovery.
"""

import argparse
import copy
import importlib.metadata as metadata
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if __package__:
    from . import regress_kv_events as events
    from . import restore_runtime, smoke_backend
    from .kv_recovery_cursor import RecoveryCursor
else:
    import regress_kv_events as events
    import restore_runtime
    import smoke_backend
    from kv_recovery_cursor import RecoveryCursor


def recovery_command(executable, model, port, output, endpoints):
    command = events.test_command(executable, model, port, output)
    index = command.index("--kv-events-config") + 1
    config = json.loads(command[index])
    config["endpoint"] = smoke_backend.ipc_endpoint(endpoints / "events.sock")
    config["replay_endpoint"] = smoke_backend.ipc_endpoint(endpoints / "replay.sock")
    command[index] = json.dumps(config)
    return command


class OwnedBackend:
    """Each launch owns exactly one subprocess group and one private log."""

    def __init__(self, command, env, output, opener, origin):
        self.command, self.env, self.output = command, env, output
        self.opener, self.origin = opener, origin
        self.process = self.log = None
        self.cleanup = None

    def start(self):
        self.output.mkdir(exist_ok=False)
        self.log = (self.output / "backend.log").open("x", encoding="utf-8")
        self.process = subprocess.Popen(
            self.command,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            env=self.env,
            cwd=self.output,
            start_new_session=True,
        )
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Backend exited with {self.process.returncode}")
            try:
                with self.opener.open(self.origin + "/health", timeout=2) as reply:
                    if reply.status == 200:
                        return
            except (urllib.error.URLError, TimeoutError):
                pass
            time.sleep(1)
        raise TimeoutError("Backend readiness timed out")

    def stop(self):
        try:
            if self.process is not None and self.cleanup is None:
                self.cleanup = smoke_backend.stop_owned_group(self.process)
        finally:
            if self.log is not None:
                self.log.close()


def collect_live(subscriber, cursor, msgspec, generation, *, count, timeout=15):
    """Decode before advancing; retain only bounded counts, not raw payloads."""
    result = {"accepted": 0, "duplicate": 0, "old_connection": 0, "batches": []}
    deadline = time.monotonic() + timeout
    while len(result["batches"]) < count and time.monotonic() < deadline:
        if not subscriber.poll(100):
            continue
        frames = subscriber.recv_multipart()
        if len(frames) != 3 or len(frames[2]) > 1024**2:
            cursor.invalidate("invalid_frame")
            raise ValueError("Invalid received frame")
        try:
            summary = smoke_backend.summarize_event_batch(
                msgspec.msgpack.decode(frames[2])
            )
        except Exception:
            cursor.invalidate("invalid_live_payload")
            raise
        disposition = cursor.accept_live(frames, generation=generation)
        if disposition not in result:
            raise ValueError(f"Unexpected continuity state: {disposition}")
        result[disposition] += 1
        result["batches"].append(
            {
                "sequence": int.from_bytes(frames[1], "big"),
                "disposition": disposition,
                "events": summary,
            }
        )
    if len(result["batches"]) != count:
        raise TimeoutError("Expected live batches were not observed")
    return result


def checked_recovery(response, cursor, msgspec):
    # Validate every decoded batch before any continuity state is advanced.
    try:
        summary = events.response_summary(response, msgspec)
    except Exception:
        cursor.invalidate("invalid_replay_payload")
        raise
    disposition = cursor.recover(response, now=time.monotonic())
    return {"disposition": disposition, **summary}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def supervised_origin(response):
    require(response["next_seq"] == 0, "Fresh service already published events")
    snapshot = response.get("snapshot")
    require(snapshot is not None, "Fresh control response requires a snapshot")


def run_cases(args, output, report):
    import msgspec
    import zmq

    context = zmq.Context()
    subscriber = None
    backends = []
    cursor = RecoveryCursor(recent_limit=4)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    origin = f"http://127.0.0.1:{args.port}"
    replay_endpoint = smoke_backend.ipc_endpoint(output / "replay.sock")
    cases = report["checks"]

    def subscription():
        client = smoke_backend.create_event_subscriber(
            context, zmq, None, output / "events.sock"
        )
        client.setsockopt(zmq.MAXMSGSIZE, 1024**2)
        return client

    def launch(name):
        directory = output / name
        backend = OwnedBackend(
            recovery_command(args.executable, args.model, args.port, directory, output),
            events.test_environment(args.device, directory),
            directory,
            opener,
            origin,
        )
        backends.append(backend)
        backend.start()
        return backend

    def request(number):
        smoke_backend.request(
            opener,
            origin,
            f"Recovery case {number}:\n"
            + "The routing experiment uses a shared prefix. " * 80
            + "\nWrite one sentence about efficient inference:",
            False,
        )

    def recovery():
        response = events.replay(
            context, zmq, msgspec, replay_endpoint, cursor.next_seq, cursor.epoch
        )
        return checked_recovery(response, cursor, msgspec)

    try:
        subscriber = subscription()
        first = launch("backend-a")
        initial = events.replay(context, zmq, msgspec, replay_endpoint, 0, force=True)
        supervised_origin(initial)
        initial_summary = events.response_summary(initial, msgspec)
        require(
            all(
                e.get("block_count", 0) == 0 for e in initial_summary["snapshot_events"]
            ),
            "Supervised fresh service already has resident blocks",
        )
        cursor.start_supervised_empty(
            initial["publisher_epoch"], 0, now=time.monotonic()
        )
        cases["supervised_empty_origin"] = initial_summary
        request(0)
        cases["initial_live"] = collect_live(
            subscriber, cursor, msgspec, cursor.generation, count=1
        )
        require(
            any(
                e["kind"] == "BlockStored"
                for e in cases["initial_live"]["batches"][0]["events"]
            ),
            "Initial request did not produce a storage event",
        )

        # Withhold consumption while PUB remains connected, then replay the
        # missing interval and verify actual buffered live frames are duplicates.
        cursor.invalidate("withheld_live_interval")
        start = cursor.next_seq
        for number in range(1, 4):
            request(number)
        multi = recovery()
        require(multi["disposition"] == "recovered", "Multi-batch replay failed")
        require(len(multi["replayed_sequences"]) == 3, "Expected three missing batches")
        require(cursor.next_seq == start + 3, "Multi-batch watermark mismatch")
        cases["multi_batch_gap_replay"] = multi
        duplicates = collect_live(
            subscriber, cursor, msgspec, cursor.generation, count=3
        )
        require(duplicates["duplicate"] == 3, "Replayed live frames were applied twice")
        cases["buffered_live_deduplication"] = duplicates

        subscriber.close(linger=0)
        subscriber = None
        cursor.replace_connection()
        require(not cursor.continuous, "Disconnect must revoke continuity")
        for number in range(4, 6):
            request(number)
        subscriber = subscription()
        disconnected = recovery()
        require(disconnected["disposition"] == "recovered", "Disconnect replay failed")
        require(len(disconnected["replayed_sequences"]) == 2, "Disconnect gap mismatch")
        cases["disconnect_replay"] = disconnected

        # No traffic and no new events is legitimate: control confirmation
        # should preserve continuity, rather than interpreting idle as failure.
        idle = recovery()
        require(idle["replayed_sequences"] == [], "Unexpected idle events")
        require(cursor.continuous, "Idle control confirmation lost continuity")
        cases["idle_control_confirmation"] = idle
        lease_witness = copy.deepcopy(cursor)

        # Six missing batches exceed the four-batch server window. The native
        # snapshot is diagnostic only; it cannot renew the consistent origin.
        cursor.invalidate("withheld_overflow_interval")
        for number in range(6, 12):
            request(number)
        overflow = recovery()
        require(
            overflow["disposition"] == "snapshot_unknown", "Overflow trusted snapshot"
        )
        require(
            not cursor.continuous and not cursor.has_origin, "Overflow retained origin"
        )
        cases["overflow_remains_unknown"] = overflow
        subscriber.close(linger=0)
        subscriber = None
        old_generation = cursor.generation
        cursor.replace_connection()
        first.stop()

        try:
            events.replay(
                context, zmq, msgspec, replay_endpoint, cursor.next_seq, cursor.epoch
            )
        except zmq.Again:
            require(
                lease_witness.continuous
                and lease_witness.expire(now=time.monotonic(), max_age=5),
                "Previously continuous checkpoint did not expire",
            )
            cases["stopped_control_timeout"] = {
                "expected_timeout": True,
                "checkpoint_was_continuous": True,
                "continuous": False,
            }
        else:
            raise ValueError("Stopped backend unexpectedly answered replay")

        subscriber = subscription()
        launch("backend-b")
        restarted = events.replay(
            context, zmq, msgspec, replay_endpoint, cursor.next_seq, cursor.epoch
        )
        supervised_origin(restarted)
        restart_summary = events.response_summary(restarted, msgspec)
        require(
            all(
                e.get("block_count", 0) == 0 for e in restart_summary["snapshot_events"]
            ),
            "Restarted supervised service already has resident blocks",
        )
        require(
            restarted["publisher_epoch"] != initial["publisher_epoch"],
            "Same-address restart reused the old epoch",
        )
        changed = checked_recovery(restarted, cursor, msgspec)
        require(
            changed["disposition"] == "new_epoch", "Restart failed to invalidate origin"
        )
        cases["same_address_restart"] = changed
        # This is an explicit local old-connection injection. Real live frames
        # lack epoch, so it is not claimed as network-level epoch rejection.
        disposition = cursor.accept_live(
            [b"", (0).to_bytes(8, "big"), b"synthetic-old-frame"],
            generation=old_generation,
        )
        require(disposition == "old_connection", "Old connection frame was accepted")
        cases["old_connection_injection"] = {
            "disposition": disposition,
            "synthetic": True,
        }
        cursor.start_supervised_empty(
            restarted["publisher_epoch"], 0, now=time.monotonic()
        )
        request(12)
        cases["new_epoch_live"] = collect_live(
            subscriber, cursor, msgspec, cursor.generation, count=1
        )
        cases["new_epoch_control"] = recovery()
        report["stream_continuity_validated"] = True
        report["status"] = "pass"
    finally:
        cleanup_errors = []
        for backend in reversed(backends):
            try:
                backend.stop()
            except (OSError, subprocess.SubprocessError) as error:
                cleanup_errors.append(type(error).__name__)
        report["backends"] = [
            {"command": backend.command, "cleanup": backend.cleanup}
            for backend in backends
        ]
        if subscriber is not None:
            subscriber.close(linger=0)
        context.term()
        if cleanup_errors:
            raise RuntimeError(f"Owned backend cleanup failed: {cleanup_errors}")
        if report.get("status") == "pass":
            require(
                all(
                    backend.cleanup is not None
                    and backend.cleanup["parent_returncode"] == 0
                    and not backend.cleanup["forced_group_cleanup"]
                    for backend in backends
                ),
                "Recovery subset requires normal shutdown of both services",
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--port", type=int, default=18242)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if sys.platform != "linux" or args.device < 0 or not 0 < args.port < 65536:
            raise ValueError("Requires Linux, a logical NPU and a valid port")
        output = restore_runtime.private_path(args.output)
        args.executable = args.executable.resolve(strict=True)
        args.model = args.model.resolve(strict=True)
        metadata.distribution("vllm-ascend-hust")
        if args.executable.parent != Path(sys.executable).parent.resolve():
            raise ValueError("Execute with the supplied HUST service venv")
        if not args.executable.is_file() or not (args.model / "config.json").is_file():
            raise ValueError("Executable and model configuration must exist")
        if output == args.model or args.model in output.parents:
            raise ValueError("Evidence must not be written inside the model")
        recovery_command(
            args.executable, args.model, args.port, output / "backend-a", output
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", args.port))
        output.mkdir(parents=True, exist_ok=False)
    except (OSError, ValueError, metadata.PackageNotFoundError) as error:
        parser.error(str(error))
    report = {
        "schema_version": 1,
        "scope": "supervised_same_host_native_disconnect_replay_restart_subset",
        "status": "fail",
        "device": args.device,
        "checks": {},
        "stream_continuity_validated": False,
        "snapshot_boundary_validated": False,
        "identity_validated": False,
        "complete_recovery_validated": False,
        "performance_validated": False,
    }
    try:
        run_cases(args, output, report)
    except Exception as error:
        report["status"] = "fail"
        report["stream_continuity_validated"] = False
        report["error"] = f"{type(error).__name__}: {error}"
    except KeyboardInterrupt:
        report["status"] = "fail"
        report["stream_continuity_validated"] = False
        report["error"] = "KeyboardInterrupt"
        return 130
    finally:
        with (output / "summary.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
    print(f"KV recovery subset: {report['status']}; {output / 'summary.json'}")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
