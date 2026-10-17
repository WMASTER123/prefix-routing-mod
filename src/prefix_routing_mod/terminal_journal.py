"""Bounded single-owner terminal journal for optional observation experiments.

Explicit capture supplies complete families before core submission. This journal
never infers ownership from ID suffixes or HTTP completion. Transport, persistence,
authentication and device operations belong to callers, not this state machine.
"""

from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256


@dataclass(frozen=True)
class Binding:
    worker_id: str
    instance_epoch: str
    observer_epoch: str

    def __post_init__(self):
        for value in (self.worker_id, self.instance_epoch, self.observer_epoch):
            _identifier(value)


def _identifier(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 96
        or not value.isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise ValueError("bounded nonempty printable ASCII identifier required")


@dataclass(frozen=True)
class Ended:
    binding: Binding
    sequence: int
    attempt_id: str

    def __post_init__(self):
        if not isinstance(self.binding, Binding):
            raise ValueError("binding required")
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("positive sequence required")
        _identifier(self.attempt_id)


@dataclass
class Family:
    children: frozenset[str]
    expected: int
    sealed: bool = False
    abandoned: bool = False
    ended: set[str] = field(default_factory=set)
    record: Ended | None = None
    managed: bool = False
    scoped_children: bool = False


class TerminalJournal:
    """One owner, bounded retention, no silent wraparound or terminal inference.

    Admission integration is not implemented. A caller must reserve journal
    capacity before dispatch; registration failure must not send new work.
    Completed families remain tombstones until explicit observer retirement.
    This finite prototype deliberately stops admitting at that limit.
    """

    def __init__(self, binding, *, max_families, max_children, max_events):
        if not isinstance(binding, Binding):
            raise ValueError("binding required")
        for value in (max_families, max_children, max_events):
            if type(value) is not int or value <= 0:
                raise ValueError("positive integer budgets required")
        self.binding = binding
        self.max_families = max_families
        self.max_children = max_children
        self.max_events = max_events
        self.families = {}
        self.owners = {}
        self.events = deque()
        self.next_sequence = 1
        self.acknowledged = 0
        self.retired_through = 0
        self.failed = False
        self.reserved_children = 0
        self.next_attempt = 1

    def register(self, attempt_id, children):
        """Require an explicit, complete tuple, never a lazy/unbounded iterable."""
        _identifier(attempt_id)
        if not isinstance(children, tuple) or not children:
            raise ValueError("complete nonempty child tuple required")
        if len(children) > self.max_children:
            raise ValueError("child budget exceeded")
        for child in children:
            _identifier(child)
        child_set = frozenset(children)
        if len(child_set) != len(children):
            raise ValueError("duplicate child")
        if any(child in self.owners for child in children):
            raise ValueError("mapping already retained; IDs must not be reused")
        self.begin(attempt_id, len(children))
        for child in children:
            self.bind_child(attempt_id, child)
        self.seal(attempt_id)

    def begin(self, attempt_id, expected):
        """Reserve the COMPLETE child budget before any simulated submission."""
        self._begin(attempt_id, expected, managed=False, scoped_children=False)

    def begin_managed(self, attempt_id, expected):
        """Reserve a trusted, externally allocated attempt for later retirement.

        The caller must establish ownership before using this method.  A string
        prefix is not proof.  The HUST adapter exposes this only in its explicit
        exclusive-Router mode, with the backend bound to loopback.
        """
        self._begin(attempt_id, expected, managed=True, scoped_children=False)

    def begin_owned(self, expected):
        """Allocate a non-reused attempt ID within this observer epoch.

        Only owner-allocated families can be retired. Callers using arbitrary
        external IDs retain the original finite behavior because the journal
        cannot prove those IDs will never be reused. Production restart must
        restore this counter or allocate a new observer epoch before dispatch.
        """
        counter = self.next_attempt
        self.next_attempt += 1
        prefix = sha256(self.binding.observer_epoch.encode()).hexdigest()
        attempt_id = f"{prefix}:{counter}"
        self._begin(attempt_id, expected, managed=True, scoped_children=True)
        return attempt_id

    def allocate_child(self, attempt_id):
        """Return the next scoped correlation ID before binding a real child."""
        family = self.families[attempt_id]
        if not family.managed:
            raise ValueError("owner-managed family required")
        return f"{attempt_id}/{len(family.children) + 1}"

    def _begin(self, attempt_id, expected, *, managed, scoped_children):
        _identifier(attempt_id)
        if type(expected) is not int or not 0 < expected <= self.max_children:
            raise ValueError("bounded positive expected child count required")
        if self.failed:
            raise RuntimeError("observer unavailable; do not dispatch")
        if attempt_id in self.families:
            raise ValueError("mapping already retained; IDs must not be reused")
        if (
            len(self.families) >= self.max_families
            or self.reserved_children + expected > self.max_children
        ):
            raise RuntimeError("retention full; do not dispatch")
        self.families[attempt_id] = Family(
            frozenset(),
            expected,
            managed=managed,
            scoped_children=scoped_children,
        )
        self.reserved_children += expected

    def bind_child(self, attempt_id, child):
        _identifier(child)
        family = self.families[attempt_id]
        if self.failed or family.sealed or family.abandoned:
            raise RuntimeError("capture unavailable or already closed")
        if child in self.owners or len(family.children) >= family.expected:
            raise ValueError("duplicate child or expected count exceeded")
        if family.scoped_children and not child.startswith(attempt_id + "/"):
            raise ValueError("managed child must use owner-scoped identity")
        family.children = family.children | {child}
        self.owners[child] = attempt_id

    def seal(self, attempt_id):
        family = self.families[attempt_id]
        if self.failed or family.abandoned:
            raise RuntimeError("capture unavailable or abandoned")
        if len(family.children) != family.expected:
            raise ValueError("incomplete family; do not seal")
        family.sealed = True
        if len(family.ended) == family.expected and family.record is None:
            return self._emit(attempt_id, family)
        return True

    def abandon(self, attempt_id):
        """Partial submission failure is unresolved, even if known children end."""
        family = self.families[attempt_id]
        if family.sealed:
            raise ValueError("already sealed")
        family.abandoned = True

    def observe_core_end(self, child):
        """Called AFTER successful core cleanup in the experiment.

        Known overload/identity failures poison observation and return False,
        without raising into the simulated scheduler. No device-free claim.
        Unexpected interpreter failures are not modeled as safely recoverable.
        """
        if self.failed:
            return False
        if not isinstance(child, str) or child not in self.owners:
            self.failed = True
            return False
        family = self.families[self.owners[child]]
        if child in family.ended:
            return True
        completes = (
            family.sealed
            and not family.abandoned
            and len(family.ended) + 1 == family.expected
        )
        if completes and len(self.events) >= self.max_events:
            self.failed = True
            return False
        family.ended.add(child)
        if completes:
            return self._emit(self.owners[child], family)
        return True

    def _emit(self, attempt_id, family):
        if len(self.events) >= self.max_events:
            self.failed = True
            return False
        record = Ended(self.binding, self.next_sequence, attempt_id)
        family.record = record
        self.events.append(record)
        self.next_sequence += 1
        return True

    def replay(self, binding, after):
        self._validate_cursor(binding, after)
        if after < self.acknowledged:
            raise RuntimeError("replay window exhausted; use complete snapshot")
        return tuple(event for event in self.events if event.sequence > after)

    def snapshot(self, binding):
        return self.snapshot_after(binding, 0)

    def snapshot_after(self, binding, after):
        self._validate_cursor(binding, after)
        if after < self.retired_through:
            raise RuntimeError("snapshot window exhausted; restore durable cursor")
        # Only positive ended evidence. Absence is NOT a not-accepted proof.
        return self.next_sequence - 1, tuple(
            sorted(
                (
                    family.record
                    for family in self.families.values()
                    if family.record is not None and family.record.sequence > after
                ),
                key=lambda record: record.sequence,
            )
        )

    def acknowledge(self, binding, through):
        self._validate_cursor(binding, through)
        if through < self.acknowledged:
            raise ValueError("acknowledgement regressed")
        self.acknowledged = through
        while self.events and self.events[0].sequence <= through:
            self.events.popleft()

    def retire(self, binding, through, *, checkpointed_through):
        """Reclaim only durably consumed, owner-allocated terminal families.

        checkpointed_through is supplied by the consumer after its own durable
        cursor commit. A lost consumer cursor therefore cannot be hidden by
        source-side reclamation.
        """
        self._validate_cursor(binding, through)
        if (
            type(checkpointed_through) is not int
            or checkpointed_through != through
            or through > self.acknowledged
            or through < self.retired_through
        ):
            raise ValueError("durable consumer cursor must match acknowledged boundary")
        retiring = [
            (attempt_id, family)
            for attempt_id, family in self.families.items()
            if family.record is not None and family.record.sequence <= through
        ]
        if any(not family.managed for _, family in retiring):
            raise RuntimeError("external attempt identity cannot be retired safely")
        for attempt_id, family in retiring:
            for child in family.children:
                if self.owners.get(child) != attempt_id:
                    raise RuntimeError("terminal ownership changed before retirement")
                del self.owners[child]
            self.reserved_children -= family.expected
            del self.families[attempt_id]
        self.retired_through = through
        return len(retiring)

    def _validate_cursor(self, binding, cursor):
        if self.failed:
            raise RuntimeError("observer unavailable; retain unresolved debt")
        if binding != self.binding:
            raise ValueError("observer/instance binding mismatch")
        if type(cursor) is not int or not 0 <= cursor < self.next_sequence:
            raise ValueError("invalid cursor")


class OnlineConsumer:
    """Synchronous CPU delivery seam, not a cross-process reliable transport.

    apply_terminal must durably/idempotently apply the SAME record on retry.
    The tests use the existing reference ledger, without claiming durability.
    No acknowledgement is sent until the entire delivery succeeds.
    """

    def __init__(self, binding, apply_terminal):
        self.binding = binding
        self.apply_terminal = apply_terminal
        self.cursor = 0
        self.requires_recovery = True

    def recover(self, journal):
        self.requires_recovery = True
        if self.cursor == 0 and journal.retired_through == 0:
            boundary, records = journal.snapshot(self.binding)
        else:
            boundary, records = journal.snapshot_after(self.binding, self.cursor)
        if boundary < self.cursor:
            raise ValueError("snapshot regressed")
        self._validate_records(records, self.cursor)
        if len(records) != boundary - self.cursor:
            raise ValueError("incomplete snapshot")
        for record in records:
            self.apply_terminal(record)
        journal.acknowledge(self.binding, boundary)
        self.cursor = boundary
        self.requires_recovery = False

    def pump(self, journal):
        if self.requires_recovery:
            raise RuntimeError("complete recovery required")
        try:
            records = journal.replay(self.binding, self.cursor)
            self._validate_records(records, self.cursor)
            boundary = self.cursor
            for record in records:
                if record.sequence != boundary + 1:
                    raise ValueError("terminal sequence gap")
                self.apply_terminal(record)
                boundary = record.sequence
            journal.acknowledge(self.binding, boundary)
            self.cursor = boundary
        except Exception:
            self.requires_recovery = True
            raise

    def disconnect(self):
        self.requires_recovery = True

    def _validate_records(self, records, after):
        for offset, record in enumerate(records, 1):
            if (
                not isinstance(record, Ended)
                or record.binding != self.binding
                or record.sequence != after + offset
            ):
                raise ValueError("terminal sequence gap or binding mismatch")
