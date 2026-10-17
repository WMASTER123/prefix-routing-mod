"""Explicit loopback CPU receiver; no server/thread starts on import.

This receives transport observations, never dispatch commands or backend ENDED.
The ledger must already contain reservations. A reopened ledger stays paused.
"""

import argparse
import hmac
import json
import os
import socket
import struct
import time
from pathlib import Path

from tools.durable_ledger_prototype import (
    MAX_RECORD_BYTES,
    DurableLedger,
    decode,
    encode,
    event_from,
)

SCHEMA = "transport-observer-cpu-v1"


def receive_exact(connection, size, deadline):
    parts = bytearray()
    while len(parts) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("frame deadline exceeded")
        connection.settimeout(remaining)
        part = connection.recv(size - len(parts))
        if not part:
            raise ConnectionError("incomplete frame")
        parts.extend(part)
    return bytes(parts)


def read_frame(connection, timeout=0.5):
    deadline = time.monotonic() + timeout
    size = struct.unpack("!I", receive_exact(connection, 4, deadline))[0]
    if not 0 < size <= MAX_RECORD_BYTES:
        raise ValueError("frame budget exceeded")
    return decode(receive_exact(connection, size, deadline).decode("utf-8"))


def write_frame(connection, value):
    payload = encode(value).encode()
    if not 0 < len(payload) <= MAX_RECORD_BYTES:
        raise ValueError("frame budget exceeded")
    connection.sendall(struct.pack("!I", len(payload)) + payload)


class Receiver:
    def __init__(self, store, token, after_commit=None):
        if (
            not isinstance(token, str)
            or len(token) != 64
            or any(char not in "0123456789abcdef" for char in token)
        ):
            raise ValueError("explicit 256-bit hex capability required")
        self.store = store
        self.token = token
        self.after_commit = after_commit or (lambda: None)

    def handle(self, message):
        if (
            not isinstance(message, dict)
            or set(message) != {"schema", "token", "event"}
            or message["schema"] != SCHEMA
            or not isinstance(message["token"], str)
            or not hmac.compare_digest(message["token"], self.token)
        ):
            raise ValueError("invalid capability or envelope")
        source = event_from(message["event"])
        result = self.store.receive_transport(source)
        self.after_commit()
        return {
            "schema": SCHEMA,
            "result": result,
            "event_id": source.event_id,
            "attempt_id": source.attempt_id,
            "clock_id": source.clock_id,
            "sequence": source.sequence,
        }

    def exchange(self, connection):
        # One bounded frame per connection; deadlines are set by the caller.
        # No positive receipt is sent when validation/storage/application fails.
        response = self.handle(read_frame(connection))
        write_frame(connection, response)


def serve(database: Path, token: str, *, lifetime: float = 60):
    if not 0 < lifetime <= 300:
        raise ValueError("bounded receiver lifetime required")
    with DurableLedger(database) as store, socket.socket() as listener:
        receiver = Receiver(store, token)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.2)
        print(
            json.dumps({"port": listener.getsockname()[1], "schema": SCHEMA}),
            flush=True,
        )
        deadline = time.monotonic() + lifetime
        while time.monotonic() < deadline:
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(0.5)
                try:
                    receiver.exchange(connection)
                except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                    # The source retains/retries an unacknowledged record.
                    # Deliberately do not log capabilities or source payloads.
                    continue


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--lifetime", type=float, default=60)
    args = parser.parse_args()
    serve(args.database, os.environ["PREFIX_PROBE_TOKEN"], lifetime=args.lifetime)


if __name__ == "__main__":
    main()
