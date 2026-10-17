"""Bounded, single-process observation state; no engine imports or I/O on import.

Uses the existing HUST core utility transport when explicitly connected. These
finite journals stop enhanced admission at their budgets. Core records and their
duplicate guards are reclaimed only after the frontend ACKs the same sequence;
frontend family retirement remains a separate, stronger ownership boundary.
"""

import asyncio
import os
import uuid
from collections import Counter, deque
from dataclasses import asdict

from .terminal_journal import Binding, TerminalJournal

CORE_METHOD = "_prefix_routing_terminal_poll_v1"
END_STATUSES = frozenset(
    {
        "unknown",
        "FINISHED_STOPPED",
        "FINISHED_LENGTH_CAPPED",
        "FINISHED_ABORTED",
        "FINISHED_IGNORED",
        "FINISHED_ERROR",
        "FINISHED_REPETITION",
    }
)


class CoreEnds:
    def __init__(self, limit=256):
        if type(limit) is not int or not 0 < limit <= 4096:
            raise ValueError("Invalid core observation limit")
        self.limit = limit
        self.epoch = uuid.uuid4().hex
        self.pid = os.getpid()
        self.records = deque()
        self.seen = {}
        self.boundary = self.acknowledged = 0
        self.failed = False

    def observe(self, child, status="unknown"):
        if self.failed:
            return False
        if (
            not isinstance(child, str)
            or not child
            or len(child.encode()) > 512
            or status not in END_STATUSES
        ):
            self.failed = True
            return False
        if child in self.seen:
            if self.seen[child][1] != status:
                self.failed = True
                return False
            return True
        if len(self.seen) >= self.limit:
            self.failed = True
            return False
        self.boundary += 1
        self.seen[child] = (self.boundary, status)
        self.records.append(
            {"sequence": self.boundary, "child": child, "status": status}
        )
        return True

    def poll(self, epoch, after, acknowledge):
        if self.failed:
            raise RuntimeError("Core terminal observation unavailable")
        if (
            type(after) is not int
            or type(acknowledge) is not int
            or not 0 <= acknowledge <= after <= self.boundary
            or acknowledge < self.acknowledged
            or after < self.acknowledged
            or (epoch is not None and epoch != self.epoch)
            or (epoch is None and (after != 0 or acknowledge != 0))
        ):
            raise ValueError("Core terminal cursor/epoch mismatch")
        self.acknowledged = acknowledge
        while self.records and self.records[0]["sequence"] <= acknowledge:
            record = self.records.popleft()
            # The fixed, hash-guarded HUST cleanup path removes the request
            # after invoking the observer. Once the sole frontend has ACKed
            # this record, retaining its duplicate guard would turn the limit
            # into a lifetime request cap rather than an unacknowledged-window
            # bound. Do not reclaim a newer colliding observation by mistake.
            if self.seen.get(record["child"]) == (
                record["sequence"],
                record["status"],
            ):
                del self.seen[record["child"]]
        return {
            "schema_version": 1,
            "epoch": self.epoch,
            "pid": self.pid,
            "boundary": self.boundary,
            "records": [
                dict(record) for record in self.records if record["sequence"] > after
            ],
        }


class FrontendEnds:
    def __init__(self, engine_core, worker_id, bootstrap):
        if (
            not isinstance(bootstrap, dict)
            or type(bootstrap.get("schema_version")) is not int
            or bootstrap.get("schema_version") != 1
            or bootstrap.get("boundary") != 0
            or bootstrap.get("records") != []
            or type(bootstrap.get("pid")) is not int
            or bootstrap["pid"] <= 0
            or not isinstance(bootstrap.get("epoch"), str)
            or len(bootstrap["epoch"]) != 32
        ):
            raise ValueError("Controlled empty observer bootstrap required")
        self.core = engine_core
        self.core_epoch = bootstrap["epoch"]
        self.core_pid = bootstrap["pid"]
        self.cursor = 0
        self.child_statuses = {}
        self.journal = TerminalJournal(
            Binding(worker_id, self.core_epoch, uuid.uuid4().hex),
            max_families=64,
            max_children=256,
            max_events=64,
        )
        self.disabled = False
        self.ownership_mode = "observe_only"
        self.lock = asyncio.Lock()

    async def poll_core(self):
        try:
            reply = await asyncio.wait_for(
                self.core.call_utility_async(
                    CORE_METHOD, self.core_epoch, self.cursor, self.cursor
                ),
                timeout=5,
            )
            if (
                not isinstance(reply, dict)
                or type(reply.get("schema_version")) is not int
                or reply.get("schema_version") != 1
                or reply.get("epoch") != self.core_epoch
                or reply.get("pid") != self.core_pid
                or reply.get("cleanup_observer_installed") is not (not self.disabled)
                or type(reply.get("boundary")) is not int
                or not isinstance(reply.get("records"), list)
                or len(reply["records"]) > 256
                or reply["boundary"] != self.cursor + len(reply["records"])
            ):
                raise ValueError("Core terminal reply identity/coverage differs")
            # Validate the whole batch before advancing any observation.
            for sequence, record in enumerate(reply["records"], self.cursor + 1):
                if (
                    not isinstance(record, dict)
                    or set(record) != {"sequence", "child", "status"}
                    or type(record["sequence"]) is not int
                    or record["sequence"] != sequence
                    or not isinstance(record["child"], str)
                    or not record["child"]
                    or len(record["child"].encode()) > 512
                    or not isinstance(record["status"], str)
                    or record["status"] not in END_STATUSES
                ):
                    raise ValueError("Core terminal record malformed or gapped")
            for record in reply["records"]:
                # Managed children bind before their first core IPC await. Other
                # traffic has no matching owner and cannot close a managed family.
                if record["child"] in self.journal.owners:
                    if not self.journal.observe_core_end(record["child"]):
                        raise RuntimeError("Frontend terminal journal unavailable")
                    self.child_statuses[record["child"]] = record["status"]
            self.cursor = reply["boundary"]
        except (Exception, asyncio.CancelledError):
            self.journal.failed = True
            raise

    async def identity(self):
        """Read instance liveness without reading/ACKing consumer history."""
        async with self.lock:
            if self.journal.failed:
                raise RuntimeError("Frontend terminal observation unavailable")
            await self.poll_core()
            return {
                "schema_version": 1,
                "binding": asdict(self.journal.binding),
                "frontend_pid": os.getpid(),
                "core_pid": self.core_pid,
                "core_boundary": self.cursor,
                "frontend_boundary": self.journal.next_sequence - 1,
                "family_count": len(self.journal.families),
                "reserved_children": self.journal.reserved_children,
                "acknowledged": self.journal.acknowledged,
                "retired_through": self.journal.retired_through,
                "ownership_mode": self.ownership_mode,
                "disabled": self.disabled,
            }

    async def retire(self, observer_epoch, through, checkpointed_through):
        """Reclaim a durably consumed prefix of trusted terminal families."""
        async with self.lock:
            if (
                observer_epoch != self.journal.binding.observer_epoch
                or type(through) is not int
                or type(checkpointed_through) is not int
            ):
                raise ValueError("Frontend terminal retirement binding mismatch")
            if self.ownership_mode != "exclusive_router_v1":
                raise RuntimeError("Managed retirement requires exclusive Router mode")
            retiring_children = {
                child
                for family in self.journal.families.values()
                if family.record is not None and family.record.sequence <= through
                for child in family.children
            }
            count = self.journal.retire(
                self.journal.binding,
                through,
                checkpointed_through=checkpointed_through,
            )
            for child in retiring_children:
                self.child_statuses.pop(child, None)
            return {
                "schema_version": 1,
                "binding": asdict(self.journal.binding),
                "retired": count,
                "retired_through": self.journal.retired_through,
                "family_count": len(self.journal.families),
                "reserved_children": self.journal.reserved_children,
            }

    async def read(self, observer_epoch, after, acknowledge):
        async with self.lock:
            if (
                (
                    observer_epoch is not None
                    and observer_epoch != self.journal.binding.observer_epoch
                )
                or (observer_epoch is None and (after != 0 or acknowledge != 0))
                or type(after) is not int
                or type(acknowledge) is not int
                or not 0 <= acknowledge <= after < self.journal.next_sequence
            ):
                raise ValueError("Frontend terminal cursor/epoch mismatch")
            await self.poll_core()
            if acknowledge:
                self.journal.acknowledge(self.journal.binding, acknowledge)
            boundary, records = self.journal.snapshot_after(self.journal.binding, after)
            snapshot = after < self.journal.acknowledged
            return {
                "schema_version": 1,
                "binding": asdict(self.journal.binding),
                "core_pid": self.core_pid,
                "frontend_pid": os.getpid(),
                "boundary": boundary,
                "mode": "snapshot" if snapshot else "replay",
                "records": [
                    asdict(record)
                    for record in records
                    if snapshot or record.sequence > after
                ],
                "families": [
                    {
                        "attempt_id": name,
                        "expected": family.expected,
                        "bound": len(family.children),
                        "ended": len(family.ended),
                        "sealed": family.sealed,
                        "abandoned": family.abandoned,
                        "core_statuses": dict(
                            Counter(
                                self.child_statuses[child] for child in family.ended
                            )
                        ),
                    }
                    for name, family in self.journal.families.items()
                ],
                "ownership_mode": self.ownership_mode,
                "disabled": self.disabled,
            }
