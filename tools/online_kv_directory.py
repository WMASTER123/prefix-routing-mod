"""Opt-in native subscription into the unchanged, long-lived Router index.

Qualification plumbing for controlled empty origins; no process/thread/network
work on import. Native END_SEQ is not a snapshot or publisher epoch.
"""

import collections
import hashlib
import json
import os
import selectors
import subprocess
import threading
import time
import urllib.parse
import uuid

from prefix_routing_mod.adapters.kv import native_live_batch, native_replay_batch
from prefix_routing_mod.terminal_journal import Binding
from tools.live_terminal_cases import request_json
from tools.router_terminal_chain import TerminalSource
from tools.smoke_backend import ipc_endpoint, stop_owned_group

PROFILE = "hust-cedb477-native-v1"
MAX_FRAME = 1024 * 1024


def receive_frames(sock, zmq, count):
    """Bound multipart count/aggregate before materializing a whole message."""
    frames = []
    total = 0
    for _ in range(count):
        frame = sock.recv()
        total += len(frame)
        if total > MAX_FRAME + 64:
            raise ValueError("Native multipart exceeds aggregate byte budget")
        frames.append(frame)
        if not sock.getsockopt(zmq.RCVMORE):
            return frames
    raise ValueError("Native multipart exceeds frame count")


def check_binding(reply, source):
    """Read-only identity check; another owner may ACK terminal records."""
    if (
        type(reply.get("schema_version")) is not int
        or reply["schema_version"] != 1
        or Binding(**reply["binding"]) != source.binding
        or type(reply.get("frontend_pid")) is not int
        or type(reply.get("core_pid")) is not int
        or reply["frontend_pid"] != source.frontend_pid
        or reply["core_pid"] != source.core_pid
        or reply.get("disabled") is not False
        or reply.get("mode") not in ("replay", "snapshot")
    ):
        raise ValueError("Native stream instance/control binding changed")


class IndexOwner:
    """Explicit finite process adapter; not the selected production IPC."""

    def __init__(self, binary):
        with binary.open("rb") as stream:
            self.sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        self.lock = threading.Lock()
        self.process = subprocess.Popen(
            [str(binary), "online"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.buffer = bytearray()

    def call(self, command):
        payload = json.dumps(command, separators=(",", ":")).encode() + b"\n"
        if len(payload) > MAX_FRAME:
            raise ValueError("Index command exceeds frame budget")
        with self.lock:
            if self.process.poll() is not None:
                raise RuntimeError("Index owner exited")
            # All requests have a bounded frame. Use nonblocking writes and
            # reads so an exited/stalled consumer cannot block the caller.
            deadline = time.monotonic() + 3
            with selectors.DefaultSelector() as poll:
                stdin = self.process.stdin.fileno()
                stdout = self.process.stdout.fileno()
                os.set_blocking(stdin, False)
                os.set_blocking(stdout, False)
                poll.register(stdin, selectors.EVENT_WRITE)
                poll.register(stdout, selectors.EVENT_READ)
                offset = 0
                while time.monotonic() < deadline:
                    for key, _ in poll.select(0.05):
                        if key.fd == stdin:
                            offset += os.write(stdin, payload[offset:])
                            if offset == len(payload):
                                poll.unregister(stdin)
                        else:
                            chunk = os.read(stdout, 65536)
                            if not chunk:
                                raise RuntimeError("Index owner closed output")
                            self.buffer.extend(chunk)
                            if len(self.buffer) > MAX_FRAME:
                                raise ValueError("Index response exceeds budget")
                            if b"\n" in self.buffer:
                                line, _, tail = self.buffer.partition(b"\n")
                                self.buffer[:] = tail
                                reply = json.loads(line)
                                if reply.get("ok") is not True:
                                    raise ValueError(
                                        "Index rejected command: "
                                        + str(reply.get("fault"))
                                    )
                                return reply["value"]
                raise TimeoutError("Index owner did not acknowledge")

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                stop_owned_group(self.process)
        if not self.process.stdin.closed:
            self.process.stdin.close()
        self.process.stdout.close()


class NativeCursor:
    """Bounded continuity/dedup only; the original index owns residency."""

    def __init__(self, binding, send, *, window=128):
        if type(window) is not int or not 1 <= window <= 128:
            raise ValueError("Invalid duplicate retention window")
        self.binding = dict(binding)
        self.send = send
        self.next = 0
        self.window = window
        self.recent = collections.OrderedDict()
        self.failed = False
        self.accepted = self.duplicates = self.operations = 0

    def command(self, kind, **fields):
        return self.send({"kind": kind, "binding": self.binding, **fields})

    def invalidate(self):
        if not self.failed:
            self.failed = True
            self.command("disconnect")

    def accept(self, batch):
        if self.failed:
            raise RuntimeError("Native directory needs a new controlled origin")
        try:
            sequence = batch["sequence"]
            if type(sequence) is not int or not 0 <= sequence < 2**64 - 1:
                raise ValueError("Invalid native sequence")
            fingerprint = hashlib.sha256(
                json.dumps(batch, sort_keys=True, separators=(",", ":")).encode()
            ).digest()
            if sequence < self.next:
                if self.recent.get(sequence) != fingerprint:
                    raise ValueError("Conflicting or unretained duplicate")
                self.duplicates += 1
                return
            if sequence != self.next:
                raise ValueError("Native sequence gap")
            self.command("live", batch=batch)
            self.recent[sequence] = fingerprint
            if len(self.recent) > self.window:
                self.recent.popitem(last=False)
            self.next += 1
            self.accepted += 1
            self.operations += len(batch["ops"])
        except Exception:
            self.invalidate()
            raise


class OnlineDirectory:
    """One socket owner, independently bound to an owned cold backend."""

    def __init__(self, index, backend, opener, origin, output, token, identity):
        self.index, self.backend = index, backend
        self.opener, self.origin, self.output = opener, origin, output
        self.token, self.identity = token, identity
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.error = None
        self.cursor = None
        self.binding = None
        self.control_count = 0
        self.thread = threading.Thread(target=self._run, name="prm-kv-subscription")

    def start(self):
        self.thread.start()
        if not self.ready.wait(15):
            self.stopped.set()
            raise TimeoutError("Online directory bootstrap timed out")
        self.check()

    def check(self):
        if self.error is not None:
            raise RuntimeError("Online directory failed: " + self.error)

    def lookup(self, hashes):
        self.check()
        return self.index.call(
            {"kind": "lookup", "binding": self.binding, "hashes": hashes}
        )["matched_blocks"]

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise TimeoutError("Subscription did not stop within bounded deadline")

    def summary(self):
        return {
            "index_binary_sha256": self.index.sha256,
            "binding": self.binding,
            "accepted_batches": self.cursor.accepted if self.cursor else 0,
            "duplicate_batches": self.cursor.duplicates if self.cursor else 0,
            "operations": self.cursor.operations if self.cursor else 0,
            "control_roundtrips": self.control_count,
            "error": self.error,
            "source_scope": "owned-cold-native-published-events",
            "warm_attach_supported": False,
        }

    def _run(self):
        import msgspec
        import zmq
        from zmq.utils.monitor import parse_monitor_message

        context = zmq.Context()
        sub = context.socket(zmq.SUB)
        replay = context.socket(zmq.DEALER)
        monitor = sub.get_monitor_socket(events=zmq.EVENT_DISCONNECTED)
        try:
            for sock in (sub, replay):
                sock.setsockopt(zmq.LINGER, 0)
                sock.setsockopt(zmq.MAXMSGSIZE, MAX_FRAME)
                sock.setsockopt(zmq.RCVHWM, 128)
            sub.setsockopt(zmq.SUBSCRIBE, b"")
            sub.connect(ipc_endpoint(self.output / "events.sock"))
            replay.connect(ipc_endpoint(self.output / "replay.sock"))

            def read(epoch, after, acknowledge):
                query = {"after": after, "acknowledge": acknowledge}
                if epoch is not None:
                    query["observer_epoch"] = epoch
                return request_json(
                    self.opener,
                    self.origin,
                    "/prefix-routing/terminal?" + urllib.parse.urlencode(query),
                    headers={"X-Prefix-Routing-Control": self.token},
                    timeout=2,
                )

            source = TerminalSource(read)
            if source.frontend_pid != self.backend.process.pid:
                raise ValueError("Terminal identity is not the owned frontend")
            self.binding = {
                "worker": source.binding.worker_id,
                # Numeric incarnation belongs to this finite native-index run;
                # retain the full real instance identity in the session key.
                "instance": 1,
                "publisher": source.binding.instance_epoch + ":" + uuid.uuid4().hex,
                "identity": self.identity,
                "block_size": 128,
            }

            def check_source():
                if self.backend.process.poll() is not None:
                    raise RuntimeError("Owned backend exited")
                check_binding(read(source.binding.observer_epoch, 0, 0), source)

            def replay_from(start, accept):
                replay.send_multipart([b"", start.to_bytes(8, "big")])
                deadline = time.monotonic() + 2
                count = 0
                while time.monotonic() < deadline:
                    if not replay.poll(25):
                        continue
                    item = native_replay_batch(
                        receive_frames(replay, zmq, 4),
                        msgspec.msgpack.decode,
                        profile_id=PROFILE,
                        block_size=128,
                    )
                    if item is None:
                        return
                    count += 1
                    if count > 128:
                        raise ValueError("Replay exceeds bounded batch count")
                    accept(item)
                raise TimeoutError("Native replay control unavailable")

            def reject_warm(_):
                raise ValueError("Controlled empty start already contains events")

            check_source()
            replay_from(0, reject_warm)
            self.index.call({"kind": "cold", "binding": self.binding, "lease_ms": 5000})
            self.cursor = NativeCursor(self.binding, self.index.call)

            def control():
                nonce = self.cursor.command("begin_control")["nonce"]
                check_source()
                replay_from(self.cursor.next, self.cursor.accept)
                check_source()
                self.cursor.command("confirm", nonce=nonce, through=self.cursor.next)
                self.control_count += 1

            control()
            self.ready.set()
            next_control = time.monotonic() + 0.5
            while not self.stopped.is_set():
                if monitor.poll(0):
                    event = parse_monitor_message(monitor.recv_multipart())
                    if event["event"] == zmq.EVENT_DISCONNECTED:
                        raise ConnectionError("Native subscriber disconnected")
                if sub.poll(25):
                    batch = native_live_batch(
                        receive_frames(sub, zmq, 3),
                        msgspec.msgpack.decode,
                        profile_id=PROFILE,
                        block_size=128,
                    )
                    self.cursor.accept(batch)
                if time.monotonic() >= next_control:
                    control()
                    next_control = time.monotonic() + 0.5
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
        finally:
            if self.cursor is not None:
                try:
                    self.cursor.invalidate()
                except Exception as error:
                    if self.error is None:
                        self.error = f"Index owner unavailable during fence: {error}"
            self.ready.set()
            sub.disable_monitor()
            monitor.close()
            sub.close()
            replay.close()
            context.term()
