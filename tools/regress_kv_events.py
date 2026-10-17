"""Bounded real-NPU storage/eviction/reset/replay regression for fixed HUST.

This test receiver is not a production index. Full recovery remains unverified.
"""

import argparse
import importlib.metadata as metadata
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

if __package__:
    from . import restore_runtime, smoke_backend
else:
    import restore_runtime
    import smoke_backend


def test_command(executable: Path, model: Path, port: int, output: Path) -> list[str]:
    command = smoke_backend.build_command(
        executable, model, port, events_ipc=output / "events.sock"
    )
    command[command.index("--max-model-len") + 1] = "1024"
    command.extend(["--kv-cache-memory-bytes", str(1024**3)])
    position = command.index("--kv-events-config") + 1
    config = json.loads(command[position])
    config["replay_endpoint"] = smoke_backend.ipc_endpoint(output / "replay.sock")
    config["buffer_steps"] = 4
    command[position] = json.dumps(config)
    return command


def test_environment(device: int, output: Path) -> dict:
    env = smoke_backend.build_environment(device, output)
    env["PYTHONPATH"] = restore_runtime.read_manifest()["cann_python_path"]
    # The fixed HUST reset route is a dev API. Enable it only in this owned,
    # loopback test service, never in the general smoke or production recipe.
    env["VLLM_SERVER_DEV_MODE"] = "1"
    return env


def validate_response(response: dict, start: int, epoch: str | None) -> dict:
    if not isinstance(response, dict):
        raise ValueError("Invalid replay envelope")
    current_epoch = response.get("publisher_epoch")
    end = response.get("next_seq")
    batches = response.get("replayed_batches")
    snapshot = response.get("snapshot")
    if not isinstance(current_epoch, str) or not current_epoch:
        raise ValueError("Replay publisher identity missing")
    if type(end) is not int or end < 0 or not isinstance(batches, list):
        raise ValueError("Invalid replay range")
    if snapshot is not None:
        if not isinstance(snapshot, bytes) or not snapshot or batches:
            raise ValueError("Invalid snapshot response")
    else:
        if current_epoch != epoch or end < start or len(batches) != end - start:
            raise ValueError("Incomplete replay coverage")
        for offset, batch in enumerate(batches):
            if (
                not isinstance(batch, list)
                or len(batch) != 2
                or type(batch[0]) is not int
                or batch[0] != start + offset
                or not isinstance(batch[1], bytes)
            ):
                raise ValueError("Non-contiguous replay batch")
    return response


class TestReceiver:
    """Keep bounded summaries only; no directory is advertised as trustworthy."""

    def __init__(self, subscriber, msgspec):
        self.subscriber = subscriber
        self.msgspec = msgspec
        self.batches = []
        self.counts = Counter()
        self.next_seq = None
        self.floor_seq = 0

    def collect(self, *, required_kind=None, timeout=10):
        before = self.counts[required_kind] if required_kind else 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.subscriber.poll(100):
                if required_kind is None or self.counts[required_kind] > before:
                    return
                continue
            frames = self.subscriber.recv_multipart()
            if len(frames) != 3 or len(frames[1]) != 8:
                raise ValueError("Invalid live event framing")
            seq = int.from_bytes(frames[1], "big")
            if seq < self.floor_seq:
                # Frames buffered before the cold-start snapshot watermark.
                continue
            if self.next_seq is not None and seq != self.next_seq:
                raise ValueError("Live subscription lost continuity")
            summaries = smoke_backend.summarize_event_batch(
                self.msgspec.msgpack.decode(frames[2])
            )
            self.next_seq = seq + 1
            if len(self.batches) >= 512:
                raise ValueError("Test receiver summary bound exceeded")
            self.batches.append({"sequence": seq, "events": summaries})
            self.counts.update(item["kind"] for item in summaries)
        raise TimeoutError("Required event was not observed within deadline")


def replay(
    context, zmq, msgspec, endpoint: str, start: int, epoch=None, *, force=False
):
    client = context.socket(zmq.REQ)
    client.setsockopt(zmq.LINGER, 0)
    client.setsockopt(zmq.SNDTIMEO, 10000)
    client.setsockopt(zmq.RCVTIMEO, 10000)
    client.setsockopt(zmq.MAXMSGSIZE, 1024**2)
    try:
        client.connect(endpoint)
        client.send(
            msgspec.msgpack.encode(
                {
                    "publisher_epoch": epoch,
                    "start_seq": start,
                    "force_snapshot": force,
                }
            )
        )
        return validate_response(msgspec.msgpack.decode(client.recv()), start, epoch)
    finally:
        client.close(linger=0)


def response_summary(response: dict, msgspec) -> dict:
    snapshot = response.get("snapshot")
    return {
        "publisher_epoch": response["publisher_epoch"],
        "next_seq": response["next_seq"],
        "replayed_sequences": [batch[0] for batch in response["replayed_batches"]],
        "replayed_events": [
            {
                "sequence": batch[0],
                "events": smoke_backend.summarize_event_batch(
                    msgspec.msgpack.decode(batch[1])
                ),
            }
            for batch in response["replayed_batches"]
        ],
        "snapshot_events": (
            smoke_backend.summarize_event_batch(msgspec.msgpack.decode(snapshot))
            if snapshot is not None
            else None
        ),
        "snapshot_boundary_validated": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--port", type=int, default=18230)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux" or args.device < 0 or not 0 < args.port < 65536:
        parser.error("Requires Linux, logical device and valid loopback port")
    try:
        output = restore_runtime.private_path(args.output)
        executable = args.executable.resolve(strict=True)
        model = args.model.resolve(strict=True)
        metadata.distribution("vllm-ascend-hust")
        if executable.parent != Path(sys.executable).parent.resolve():
            raise ValueError("Run this test with the supplied HUST executable's venv")
        if not executable.is_file() or not (model / "config.json").is_file():
            raise ValueError("Model and executable must exist")
        if output == model or model in output.parents:
            raise ValueError("Never write test evidence into the model directory")
        command = test_command(executable, model, args.port, output)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", args.port))
        output.mkdir(parents=True, exist_ok=False)
    except (OSError, ValueError, metadata.PackageNotFoundError) as error:
        parser.error(str(error))
    report = {
        "schema_version": 1,
        "scope": "real_hust_event_storage_eviction_reset_replay_subset",
        "status": "fail",
        "command": command,
        "device": args.device,
        "checks": {},
        "complete_recovery_validated": False,
        "kv_events_validated": False,
        "performance_validated": False,
        "loopback_test_dev_api": True,
    }
    process = context = subscriber = receiver = None
    try:
        import msgspec
        import zmq

        context = zmq.Context()
        subscriber = smoke_backend.create_event_subscriber(
            context, zmq, None, output / "events.sock"
        )
        subscriber.setsockopt(zmq.MAXMSGSIZE, 1024**2)
        env = test_environment(args.device, output)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        origin = f"http://127.0.0.1:{args.port}"
        with (output / "backend.log").open("x", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=output,
                start_new_session=True,
            )
            deadline = time.monotonic() + 600
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"Backend exited with {process.returncode}")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Backend readiness timed out")
                try:
                    with opener.open(origin + "/health", timeout=2) as result:
                        if result.status == 200:
                            break
                except (urllib.error.URLError, TimeoutError):
                    pass
                time.sleep(1)
            endpoint = smoke_backend.ipc_endpoint(output / "replay.sock")
            initial = replay(context, zmq, msgspec, endpoint, 0, force=True)
            report["checks"]["cold_snapshot"] = response_summary(initial, msgspec)
            receiver = TestReceiver(subscriber, msgspec)
            receiver.next_seq = initial["next_seq"]
            receiver.floor_seq = initial["next_seq"]
            for number in range(16):
                prompt = (
                    f"Unique prefix case {number}:\n"
                    + ("The routing experiment uses a shared prefix. " * 80)
                    + "\nWrite one sentence about efficient inference:"
                )
                smoke_backend.request(opener, origin, prompt, False)
                receiver.collect(required_kind="BlockStored")
            if receiver.counts["BlockRemoved"] == 0:
                raise ValueError(
                    "Natural eviction was not observed; reset is not a substitute"
                )
            report["checks"]["natural_eviction"] = True
            end = receiver.next_seq
            tail = replay(
                context, zmq, msgspec, endpoint, end - 1, initial["publisher_epoch"]
            )
            if tail.get("snapshot") is not None or not tail["replayed_batches"]:
                raise ValueError(
                    "Within-window replay unexpectedly required a snapshot"
                )
            tail_summary = response_summary(tail, msgspec)
            live = {batch["sequence"]: batch["events"] for batch in receiver.batches}
            if any(
                live.get(batch["sequence"]) != batch["events"]
                for batch in tail_summary["replayed_events"]
            ):
                raise ValueError(
                    "Replay differs from the observed live event summaries"
                )
            report["checks"]["within_window_replay"] = tail_summary
            overflow = replay(
                context, zmq, msgspec, endpoint, 0, initial["publisher_epoch"]
            )
            if overflow.get("snapshot") is None:
                raise ValueError("Replay window overflow did not return a snapshot")
            report["checks"]["overflow_snapshot"] = response_summary(overflow, msgspec)
            request = urllib.request.Request(
                origin + "/reset_prefix_cache", method="POST"
            )
            with opener.open(request, timeout=30) as result:
                report["checks"]["reset_http_status"] = result.status
            # Idle reset can defer publication until the next scheduler step.
            smoke_backend.request(
                opener, origin, "After reset: " + "test " * 200, False
            )
            receiver.collect(required_kind="AllBlocksCleared")
            report["checks"]["cleared_event"] = True
            report["status"] = "pass"
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if receiver is not None:
            report["event_counts"] = dict(receiver.counts)
            report["event_batches"] = receiver.batches
        if process is not None:
            try:
                report["cleanup"] = smoke_backend.stop_owned_group(process)
            except (OSError, subprocess.SubprocessError) as error:
                report["status"] = "fail"
                report["cleanup_error"] = type(error).__name__
        if subscriber is not None:
            subscriber.close(linger=0)
        if context is not None:
            context.term()
        with (output / "summary.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
    print(f"Real KV event subset: {report['status']}; {output / 'summary.json'}")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
