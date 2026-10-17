"""Bounded recovery oracle for the opt-in protocol, never a routing index."""

import time
import urllib.request

if __package__:
    from . import regress_kv_events, smoke_backend
else:
    import regress_kv_events
    import smoke_backend


def check_response(response, start, epoch):
    if response.get("schema_version") != 1 or response.get("valid") is not True:
        raise ValueError("Experimental recovery is unavailable or incompatible")
    return regress_kv_events.validate_response(response, start, epoch)


def live_epoch(frames):
    if len(frames) != 4 or len(frames[1]) != 8 or len(frames[2]) > 16 * 1024**2:
        raise ValueError("Invalid experimental live frames")
    epoch = frames[3].decode("ascii")
    if len(epoch) != 32 or any(c not in "0123456789abcdef" for c in epoch):
        raise ValueError("Invalid publisher epoch")
    return epoch


def apply_batch(payload, msgspec, catalog, residents):
    """Mutate only caller-private staging dictionaries, including ghost anchors."""
    batch = msgspec.msgpack.decode(payload)
    smoke_backend.summarize_event_batch(batch)
    for event in batch[1]:
        kind = event["type"]
        if kind == "AllBlocksCleared":
            catalog.clear()
            residents.clear()
        elif kind == "BlockRemoved":
            if event.get("group_idx") != 0 or event.get("medium") != "GPU":
                raise ValueError("Unsupported removed block")
            residents.difference_update((0, h) for h in event["block_hashes"])
        elif kind == "BlockStored":
            hashes, tokens, size = (
                event["block_hashes"],
                event["token_ids"],
                event["block_size"],
            )
            if (
                event.get("group_idx") != 0
                or event.get("medium") != "GPU"
                or len(tokens) != size * len(hashes)
                or not hashes
                or event.get("lora_name") is not None
                or event.get("lora_id") is not None
                or any(x is not None for x in (event.get("extra_keys") or []))
            ):
                raise ValueError("Incomplete snapshot identity")
            parent = event["parent_block_hash"]
            for i, block_hash in enumerate(hashes):
                if parent is not None and (0, parent) not in catalog:
                    raise ValueError("Snapshot or delta has a missing parent")
                key = (0, block_hash)
                value = (parent, tuple(tokens[i * size : (i + 1) * size]))
                if key in catalog and catalog[key] != value:
                    raise ValueError("Conflicting snapshot identity")
                catalog[key] = value
                residents.add(key)
                parent = block_hash
        if len(catalog) > 1024 or sum(len(v[1]) for v in catalog.values()) > 16384:
            raise ValueError("Recovery oracle exceeds controlled test bounds")


def run_cases(
    args, output, report, backends, context, subscriber, opener, origin, env, command
):
    import msgspec
    import zmq

    if __package__:
        from .regress_kv_identity import post
        from .regress_kv_recovery import OwnedBackend
    else:
        from regress_kv_identity import post
        from regress_kv_recovery import OwnedBackend

    endpoint = smoke_backend.ipc_endpoint(output / "replay.sock")

    def recovery(start=0, epoch=None, force=False):
        response = regress_kv_events.replay(
            context, zmq, msgspec, endpoint, start, epoch, force=force
        )
        return check_response(response, start, epoch)

    def request(number):
        post(
            opener,
            origin,
            "/v1/completions",
            {
                "model": smoke_backend.MODEL_NAME,
                "prompt": f"Hook case {number}:\n"
                + "Shared prefix residency matters. " * 80,
                "max_tokens": 1,
                "ignore_eos": True,
                "temperature": 0,
            },
        )

    for number in range(6):
        request(number)
    # Cold consumer joins a service whose KV has already been populated.
    cold = recovery(force=True)
    catalog, residents = {}, set()
    apply_batch(cold["snapshot"], msgspec, catalog, residents)
    if not residents or not catalog:
        raise ValueError("Warm snapshot has no token-bearing residency")
    overflowing = recovery(0, cold["publisher_epoch"])
    if overflowing.get("snapshot") is None or overflowing["next_seq"] <= 4:
        raise ValueError("Test did not exceed the four-batch replay window")
    overflow_catalog, overflow_residents = {}, set()
    apply_batch(overflowing["snapshot"], msgspec, overflow_catalog, overflow_residents)
    if cold["next_seq"] == overflowing["next_seq"] and (
        residents != overflow_residents or catalog != overflow_catalog
    ):
        raise ValueError("Cold and overflow snapshots differ at an idle cut")
    initial_catalog, initial_residents = dict(catalog), set(residents)
    request(99)
    old_wire_frame = None
    deadline = time.monotonic() + 15
    # Drain through the post-snapshot event; old buffered frames are skipped by
    # cut, and epoch is verified on real four-frame PUB messages.
    while time.monotonic() < deadline:
        if not subscriber.poll(100):
            continue
        frames = subscriber.recv_multipart()
        if live_epoch(frames) != cold["publisher_epoch"]:
            raise ValueError("Unexpected live epoch before restart")
        old_wire_frame = frames
        if int.from_bytes(frames[1], "big") >= cold["next_seq"]:
            break
    else:
        raise TimeoutError("Post-snapshot live delta was not observed")
    delta = recovery(cold["next_seq"], cold["publisher_epoch"])
    if delta.get("snapshot") is not None:
        raise ValueError("Unexpected overflow in one-delta test")
    for _, payload in delta["replayed_batches"]:
        apply_batch(payload, msgspec, initial_catalog, initial_residents)
    for _ in range(4):
        later = recovery(force=True)
        if later["next_seq"] == delta["next_seq"]:
            break
        delta = recovery(delta["next_seq"], cold["publisher_epoch"])
        if delta.get("snapshot") is not None:
            raise ValueError("Unexpected replay overflow during convergence")
        for _, payload in delta["replayed_batches"]:
            apply_batch(payload, msgspec, initial_catalog, initial_residents)
    else:
        raise ValueError("Could not observe a common final snapshot/replay cut")
    later_catalog, later_residents = {}, set()
    apply_batch(later["snapshot"], msgspec, later_catalog, later_residents)
    if later_residents != initial_residents:
        raise ValueError("Snapshot plus delta differs from the later snapshot cut")
    if any(later_catalog[k] != initial_catalog[k] for k in later_catalog):
        raise ValueError("Recovered token metadata differs")
    cases = report["hook_checks"] = {
        "warm_cold_join": True,
        "overflow_token_snapshot": True,
        "snapshot_plus_delta": True,
        "live_epoch_present": True,
    }
    report["hook_snapshot"] = {
        "next_seq": cold["next_seq"],
        "resident_blocks": len(residents),
        "metadata_blocks": len(catalog),
        "metadata_tokens": sum(len(v[1]) for v in catalog.values()),
        "payload_bytes": len(cold["snapshot"]),
    }
    # Native reset is loopback-only in this test; observe its event by replay.
    with opener.open(
        urllib.request.Request(origin + "/reset_prefix_cache", data=b"", method="POST"),
        timeout=10,
    ) as reply:
        if reply.status != 200:
            raise ValueError("Reset HTTP request failed")
    request(100)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not subscriber.poll(100):
            continue
        frames = subscriber.recv_multipart()
        if live_epoch(frames) != cold["publisher_epoch"]:
            raise ValueError("Unexpected reset epoch")
        if int.from_bytes(frames[1], "big") < delta["next_seq"]:
            continue
        summaries = smoke_backend.summarize_event_batch(
            msgspec.msgpack.decode(frames[2])
        )
        if any(event["kind"] == "AllBlocksCleared" for event in summaries):
            break
    else:
        raise TimeoutError("Native reset clear was not published")
    reset = recovery(delta["next_seq"], cold["publisher_epoch"])
    if reset.get("snapshot") is not None:
        raise ValueError("Reset exceeded the controlled replay window")
    clear_seen = any(
        event["kind"] == "AllBlocksCleared"
        for _, payload in reset["replayed_batches"]
        for event in smoke_backend.summarize_event_batch(
            msgspec.msgpack.decode(payload)
        )
    )
    if not clear_seen:
        raise ValueError("Native clear event was not observed")
    cases["real_clear_event"] = True
    backends[-1].stop()
    # Invalidate the old directory/connection before adopting a new origin.
    # A recreated IPC publisher is not guaranteed to revive an old SUB pipe.
    subscriber.close(linger=0)
    initial_catalog.clear()
    initial_residents.clear()
    restarted_sub = smoke_backend.create_event_subscriber(
        context, zmq, None, output / "events.sock"
    )
    try:
        restarted_sub.setsockopt(zmq.MAXMSGSIZE, 1024**2)
        restarted = OwnedBackend(
            command, env, output / "backend-restart", opener, origin
        )
        backends.append(restarted)
        restarted.start()
        new_origin = recovery(force=True)
        if (
            new_origin["publisher_epoch"] == cold["publisher_epoch"]
            or new_origin["next_seq"] != 0
        ):
            raise ValueError("Restart did not establish a distinct empty origin")
        apply_batch(new_origin["snapshot"], msgspec, initial_catalog, initial_residents)
        if initial_catalog or initial_residents:
            raise ValueError("Restart snapshot inherited the old directory")
        # Actual retained wire bytes, not a synthetic epoch injection. Reject
        # before decoding/applying any old data to the new staging directory.
        if live_epoch(old_wire_frame) == new_origin["publisher_epoch"]:
            raise ValueError("Old wire event was not rejected by epoch")
        cases["real_old_wire_epoch_rejected"] = True
        request(101)
        new_frame_seen = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not restarted_sub.poll(100):
                continue
            frames = restarted_sub.recv_multipart()
            if live_epoch(frames) != new_origin["publisher_epoch"]:
                continue
            smoke_backend.summarize_event_batch(msgspec.msgpack.decode(frames[2]))
            new_frame_seen = True
            break
        if not new_frame_seen:
            raise TimeoutError("New publisher wire event was not observed")
        cases["same_address_restart"] = True
        cases["new_epoch_live_event"] = True
    finally:
        restarted_sub.close(linger=0)
