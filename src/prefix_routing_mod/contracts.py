"""component-v0.1 semantic reference; not a production Router or wire codec.

No engine imports or I/O. Rust/native bindings must preserve these invariants;
production request paths need not construct Python objects or serialize JSON.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

CONTRACT_VERSION = "component-v0.1"
MAX_TOKEN_IDS = 262_144
MAX_ID_BYTES = 128


def _identifier(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or not value.isprintable()
        or len(value.encode("utf-8")) > MAX_ID_BYTES
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError("identifier must be nonempty, bounded and whitespace-free")


def _uint(value: int) -> None:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("expected nonnegative signed-64-bit integer")


class Precision(StrEnum):
    EXACT = "exact"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class Trust(StrEnum):
    TRUSTED = "trusted"
    RECOVERING = "recovering"
    UNKNOWN = "unknown"


class Health(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ResidencySource(StrEnum):
    KV_EVENTS = "kv_events"
    SNAPSHOT_AND_EVENTS = "snapshot_and_events"


@dataclass(frozen=True)
class WorkerRef:
    worker_id: str
    instance_epoch: str

    def __post_init__(self) -> None:
        _identifier(self.worker_id)
        _identifier(self.instance_epoch)


@dataclass(frozen=True)
class CacheRef:
    worker: WorkerRef
    publisher_epoch: str

    def __post_init__(self) -> None:
        if not isinstance(self.worker, WorkerRef):
            raise ValueError("cache needs a service instance identity")
        _identifier(self.publisher_epoch)


@dataclass(frozen=True)
class SemanticIdentity:
    domain: str
    model: str
    tokenizer: str
    template: str
    kv_config: str

    def __post_init__(self) -> None:
        for value in (
            self.domain,
            self.model,
            self.tokenizer,
            self.template,
            self.kv_config,
        ):
            _identifier(value)


@dataclass(frozen=True)
class RequestFeatures:
    request_id: str
    domain: str
    identity: SemanticIdentity | None
    input_tokens: int | None
    token_ids: tuple[int, ...] | None
    block_size: int | None
    precision: Precision

    def __post_init__(self) -> None:
        _identifier(self.request_id)
        _identifier(self.domain)
        if not isinstance(self.precision, Precision):
            raise ValueError("invalid precision")
        if self.identity is not None:
            if not isinstance(self.identity, SemanticIdentity):
                raise ValueError("invalid semantic identity")
            if self.identity.domain != self.domain:
                raise ValueError("request domain differs from verified identity")
        if self.input_tokens is not None:
            _uint(self.input_tokens)
        if self.block_size is not None:
            _uint(self.block_size)
            if self.block_size == 0:
                raise ValueError("block_size must be positive")
        if self.precision is Precision.EXACT:
            if self.identity is None or self.block_size is None:
                raise ValueError("exact features require identity and block size")
            if type(self.token_ids) is not tuple:
                raise ValueError("exact token_ids must be an immutable tuple")
            if len(self.token_ids) > MAX_TOKEN_IDS:
                raise ValueError("token feature limit exceeded")
            if self.input_tokens != len(self.token_ids):
                raise ValueError("token count must equal complete token_ids length")
            for token in self.token_ids:
                _uint(token)
                if token > 2**32 - 1:
                    raise ValueError("token id must fit u32")
        elif self.token_ids is not None or self.block_size is not None:
            raise ValueError("non-exact features cannot carry exact block features")


@dataclass(frozen=True)
class ReadContext:
    clock_id: str
    now_ns: int
    max_age_ns: int

    def __post_init__(self) -> None:
        _identifier(self.clock_id)
        _uint(self.now_ns)
        _uint(self.max_age_ns)

    def fresh(self, clock_id: str, at_ns: int) -> bool:
        return clock_id == self.clock_id and 0 <= self.now_ns - at_ns <= self.max_age_ns


@dataclass(frozen=True)
class Observation:
    """Counts with explicit scope; backend and local counts are not additive."""

    value: int | None
    source: str
    coverage: str
    clock_id: str
    at_ns: int

    def __post_init__(self) -> None:
        for value in (self.source, self.coverage, self.clock_id):
            _identifier(value)
        _uint(self.at_ns)
        if self.value is not None:
            _uint(self.value)


@dataclass(frozen=True)
class PrefixMatch:
    cache: CacheRef
    identity: SemanticIdentity
    block_size: int
    matched_tokens: int | None
    trust: Trust
    clock_id: str
    at_ns: int
    source: ResidencySource

    def __post_init__(self) -> None:
        if not isinstance(self.cache, CacheRef) or not isinstance(
            self.identity, SemanticIdentity
        ):
            raise ValueError("match requires cache and semantic identities")
        if not isinstance(self.trust, Trust):
            raise ValueError("invalid trust")
        _uint(self.block_size)
        if self.block_size == 0:
            raise ValueError("block_size must be positive")
        _identifier(self.clock_id)
        if not isinstance(self.source, ResidencySource):
            raise ValueError("residency source must be an observed event protocol")
        _uint(self.at_ns)
        if self.trust is Trust.TRUSTED:
            _uint(self.matched_tokens)
            if self.matched_tokens % self.block_size:
                raise ValueError("match must cover contiguous complete blocks")
        elif self.matched_tokens is not None:
            raise ValueError("untrusted match must be unknown, not a fabricated miss")

    def usable_for(self, features: RequestFeatures, context: ReadContext) -> bool:
        return (
            self.trust is Trust.TRUSTED
            and features.precision is Precision.EXACT
            and self.identity == features.identity
            and self.block_size == features.block_size
            and context.fresh(self.clock_id, self.at_ns)
            and self.matched_tokens <= features.input_tokens
        )


@dataclass(frozen=True)
class WorkerState:
    ref: WorkerRef
    domain: str
    version: int
    health: Health
    draining: bool
    clock_id: str
    at_ns: int
    backend_running: Observation
    local_outstanding: Observation

    def __post_init__(self) -> None:
        if not isinstance(self.ref, WorkerRef) or not isinstance(self.health, Health):
            raise ValueError("invalid worker identity or health")
        if type(self.draining) is not bool:
            raise ValueError("draining must be boolean")
        for value in (self.domain, self.clock_id):
            _identifier(value)
        _uint(self.version)
        _uint(self.at_ns)
        if not isinstance(self.backend_running, Observation) or not isinstance(
            self.local_outstanding, Observation
        ):
            raise ValueError("worker counts need explicit observation scopes")
        if (
            self.local_outstanding.source != "local_ledger"
            or self.local_outstanding.coverage != "router_session"
            or self.local_outstanding.clock_id != self.clock_id
            or self.local_outstanding.value is None
        ):
            raise ValueError("local count must come from this Router ledger")

    def eligible_for(self, domain: str, context: ReadContext) -> bool:
        return (
            self.domain == domain
            and self.health is Health.HEALTHY
            and not self.draining
            and context.fresh(self.clock_id, self.at_ns)
        )


@dataclass(frozen=True)
class DispatchContext:
    """M5 allocates fresh attempt IDs; config is captured before selection."""

    request_id: str
    attempt_id: str
    config_version: str

    def __post_init__(self) -> None:
        for value in (self.request_id, self.attempt_id, self.config_version):
            _identifier(value)


@dataclass(frozen=True)
class RouteDecision:
    decision_id: str
    request_id: str
    attempt_id: str
    worker: WorkerRef
    worker_version: int
    policy_version: str
    config_version: str
    reason_code: str

    def __post_init__(self) -> None:
        for value in (
            self.decision_id,
            self.request_id,
            self.attempt_id,
            self.policy_version,
            self.config_version,
            self.reason_code,
        ):
            _identifier(value)
        if not isinstance(self.worker, WorkerRef):
            raise ValueError("decision requires a worker instance")
        _uint(self.worker_version)


class EventKind(StrEnum):
    SEND_STARTED = "send_started"
    ACCEPTED = "accepted"
    STREAMING = "streaming"
    NOT_SENT = "not_sent"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCEL_CONFIRMED = "cancel_confirmed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class BackendConfirmation(StrEnum):
    NONE = "none"
    NOT_ACCEPTED = "not_accepted"
    ENDED = "ended"


@dataclass(frozen=True)
class LifecycleEvent:
    event_id: str
    request_id: str
    attempt_id: str
    worker: WorkerRef
    sequence: int
    kind: EventKind
    confirmation: BackendConfirmation
    clock_id: str
    at_ns: int
    error_class: str | None = None

    def __post_init__(self) -> None:
        for value in (self.event_id, self.request_id, self.attempt_id, self.clock_id):
            _identifier(value)
        if not isinstance(self.worker, WorkerRef):
            raise ValueError("lifecycle requires a worker instance")
        _uint(self.sequence)
        _uint(self.at_ns)
        if self.sequence == 0:
            raise ValueError("event sequence starts at one")
        if not isinstance(self.kind, EventKind) or not isinstance(
            self.confirmation, BackendConfirmation
        ):
            raise ValueError("invalid lifecycle kind or confirmation")
        if self.error_class is not None:
            _identifier(self.error_class)
        if self.kind in (EventKind.COMPLETED, EventKind.CANCEL_CONFIRMED):
            if self.confirmation is not BackendConfirmation.ENDED:
                raise ValueError("completion/cancellation must confirm backend end")
        elif self.kind is EventKind.NOT_SENT:
            if self.confirmation is not BackendConfirmation.NOT_ACCEPTED:
                raise ValueError("not_sent needs definite non-acceptance")
        elif (
            self.kind is not EventKind.FAILED
            and self.confirmation is not BackendConfirmation.NONE
        ):
            raise ValueError("progress/unknown cannot claim backend termination")


class ReservationRejected(ValueError):
    """No charge made: refresh/reselect or reject before sending."""


class LifecycleRejected(ValueError):
    """No silent repair: suspend trust/dispatch and reconcile the attempt."""


class FeaturePort(Protocol):
    def extract(self, request_id: str, request: object) -> RequestFeatures: ...


class CachePort(Protocol):
    def lookup(
        self, features: RequestFeatures, context: ReadContext
    ) -> tuple[PrefixMatch, ...]: ...


class StatePort(Protocol):
    def snapshot(self) -> tuple[WorkerState, ...]: ...


class PolicyPort(Protocol):
    def select(
        self,
        features: RequestFeatures,
        matches: tuple[PrefixMatch, ...],
        workers: tuple[WorkerState, ...],
        dispatch: DispatchContext,
        context: ReadContext,
    ) -> RouteDecision | None: ...


class LedgerPort(StatePort, Protocol):
    def reserve(
        self,
        features: RequestFeatures,
        decision: RouteDecision,
        context: ReadContext,
    ) -> None:
        """Atomic compare-and-charge; duplicates never authorize another send."""
        ...

    def apply(self, event: LifecycleEvent) -> str:
        """Return applied/duplicate/stale; never silently release uncertain work."""
        ...
