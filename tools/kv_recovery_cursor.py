"""CPU-only continuity guard for regression tools, not a production KV index.

Live frames have no epoch. Connection generations are local provenance only;
the caller must close old sockets when replacing a backend or subscription.
"""

import hashlib
import math
from collections import OrderedDict

if __package__:
    from .regress_kv_events import validate_response
else:
    from regress_kv_events import validate_response


class RecoveryCursor:
    """Bounded sequence checks from an explicitly supervised empty origin."""

    def __init__(self, *, recent_limit=128):
        if type(recent_limit) is not int or not 1 <= recent_limit <= 512:
            raise ValueError("Invalid recent sequence bound")
        self.recent_limit = recent_limit
        self.recent = OrderedDict()
        self.epoch = None
        self.next_seq = 0
        self.generation = 0
        self.continuous = False
        self.has_origin = False
        self.last_confirmed = None
        self.reason = "no_origin"

    @staticmethod
    def _time(now):
        if isinstance(now, bool) or not isinstance(now, int | float):
            raise ValueError("Confirmation requires a monotonic timestamp")
        if not math.isfinite(now) or now < 0:
            raise ValueError("Invalid monotonic timestamp")

    def start_supervised_empty(self, epoch, next_seq, *, now):
        """Caller attests a fresh exclusive backend before any model request."""
        self._time(now)
        if not isinstance(epoch, str) or not epoch or next_seq != 0:
            raise ValueError("A supervised origin requires sequence zero")
        if type(next_seq) is not int:
            raise ValueError("Invalid starting sequence")
        self.epoch = epoch
        self.next_seq = 0
        self.recent.clear()
        self.has_origin = self.continuous = True
        self.last_confirmed = now
        self.reason = "supervised_empty_origin"

    def invalidate(self, reason, *, forget_origin=False):
        self.continuous = False
        self.reason = reason
        if forget_origin:
            self.has_origin = False
            self.recent.clear()

    def replace_connection(self):
        self.generation += 1
        self.invalidate("connection_replaced")
        return self.generation

    def expire(self, *, now, max_age):
        self._time(now)
        self._time(max_age)
        if max_age <= 0:
            raise ValueError("Confirmation lease must be positive")
        if self.last_confirmed is None or now < self.last_confirmed:
            self.invalidate("confirmation_missing_or_clock_reversed")
        elif now - self.last_confirmed >= max_age:
            self.invalidate("control_confirmation_expired")
        return not self.continuous

    def _remember(self, seq, payload):
        self.recent[seq] = hashlib.sha256(payload).digest()
        while len(self.recent) > self.recent_limit:
            self.recent.popitem(last=False)

    def accept_live(self, frames, *, generation):
        if generation != self.generation:
            return "old_connection"
        if (
            len(frames) != 3
            or not isinstance(frames[1], bytes)
            or len(frames[1]) != 8
            or not isinstance(frames[2], bytes)
            or not frames[2]
            or len(frames[2]) > 1024**2
        ):
            self.invalidate("invalid_frame")
            raise ValueError("Invalid live framing")
        seq = int.from_bytes(frames[1], "big")
        if seq < self.next_seq:
            digest = self.recent.get(seq)
            if digest is None or digest != hashlib.sha256(frames[2]).digest():
                self.invalidate("unverified_duplicate")
                raise ValueError("Duplicate payload cannot be verified")
            return "duplicate"
        if seq != self.next_seq:
            self.invalidate("sequence_gap")
            return "gap"
        if not self.continuous:
            return "unknown"
        self._remember(seq, frames[2])
        self.next_seq += 1
        return "accepted"

    def recover(self, response, *, now):
        self._time(now)
        try:
            validate_response(response, self.next_seq, self.epoch)
            if len(response["replayed_batches"]) > self.recent_limit:
                raise ValueError("Replay exceeds the bounded regression window")
            if any(
                not batch[1] or len(batch[1]) > 1024**2
                for batch in response["replayed_batches"]
            ):
                raise ValueError("Invalid replay payload size")
        except (ValueError, TypeError, KeyError):
            self.invalidate("invalid_replay")
            raise
        if response["publisher_epoch"] != self.epoch:
            self.epoch = response["publisher_epoch"]
            self.next_seq = 0
            self.invalidate("publisher_restarted", forget_origin=True)
            self.last_confirmed = None
            return "new_epoch"
        if response.get("snapshot") is not None:
            self.invalidate("snapshot_boundary_unverified", forget_origin=True)
            return "snapshot_unknown"
        if not self.has_origin:
            self.invalidate("no_consistent_origin")
            return "unknown"
        if self.last_confirmed is not None and now < self.last_confirmed:
            self.invalidate("confirmation_clock_reversed")
            raise ValueError("Monotonic confirmation time moved backwards")
        for seq, payload in response["replayed_batches"]:
            self._remember(seq, payload)
        self.next_seq = response["next_seq"]
        self.continuous = True
        self.last_confirmed = now
        self.reason = "complete_replay"
        return "recovered"
