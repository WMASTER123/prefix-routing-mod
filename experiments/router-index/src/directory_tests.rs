// SPDX-License-Identifier: Apache-2.0
use super::{directory::*, directory_tests_support::*};
use parking_lot::RwLock;
use std::{sync::Arc, thread};

fn ready(records: usize) -> (Directory, Ticket, Binding) {
    let mut dir = Directory::new(limits(records)).unwrap();
    let bind = binding();
    let ticket = dir.begin(bind.clone()).unwrap();
    dir.finish(&ticket, vec![block(0)], 10, 10).unwrap();
    (dir, ticket, bind)
}
fn batch(sequence: u64, ops: Vec<Op>) -> Batch {
    Batch { sequence, ops }
}

#[test]
fn common_cut_discards_covered_frames_and_applies_continuous_tail() {
    let (mut dir, _, bind) = ready(8);
    let ticket = dir.begin(bind.clone()).unwrap();
    assert_eq!(dir.lookup(&bind, &[block(0).local.clone()]), None);
    dir.queue(&ticket, batch(9, vec![Op::Clear])).unwrap();
    dir.queue(&ticket, batch(11, vec![Op::Remove(block(0).id)]))
        .unwrap();
    dir.queue(&ticket, batch(10, vec![Op::Store(block(1))]))
        .unwrap();
    dir.finish(&ticket, vec![block(0)], 10, 11).unwrap();
    assert_eq!(dir.lookup(&bind, &[block(0).local]), Some(0));
    assert_eq!(dir.lookup(&bind, &[block(1).local]), Some(1));
}

#[test]
fn snapshot_not_queryable_if_replay_has_gap() {
    for seq in [9, 11] {
        let (mut dir, _, bind) = ready(8);
        let t = dir.begin(bind.clone()).unwrap();
        dir.queue(&t, batch(seq, vec![Op::Store(block(1))]))
            .unwrap();
        assert_eq!(dir.finish(&t, vec![block(0)], 10, 12), Err(Fault::Gap));
        assert_eq!(dir.lookup(&bind, &[]), None);
    }
}

#[test]
fn empty_snapshot_is_known_miss_only_at_valid_cut() {
    let (mut dir, _, bind) = ready(8);
    let t = dir.begin(bind.clone()).unwrap();
    dir.finish(&t, vec![], 22, 22).unwrap();
    assert_eq!(dir.lookup(&bind, &[block(0).local]), Some(0));
}

#[test]
fn missing_parent_and_cycle_never_install() {
    let mut child = block(1);
    child.parent = Some(block(0).id);
    let mut parent = block(0);
    parent.parent = Some(child.id.clone());
    for (snapshot, fault) in [
        (vec![child.clone()], Fault::MissingParent),
        (vec![parent, child], Fault::Cycle),
    ] {
        let (mut dir, _, bind) = ready(8);
        let t = dir.begin(bind.clone()).unwrap();
        assert_eq!(dir.finish(&t, snapshot, 10, 10), Err(fault));
        assert_eq!(dir.lookup(&bind, &[]), None);
    }
}

#[test]
fn removed_ancestor_is_topology_not_residency() {
    let (mut dir, _, bind) = ready(8);
    let t = dir.begin(bind.clone()).unwrap();
    let mut parent = block(0);
    parent.resident = false;
    let mut child = block(1);
    child.parent = Some(parent.id.clone());
    // Deliberately child-first snapshot, unlike normalized live store order.
    dir.finish(&t, vec![child.clone(), parent.clone()], 10, 10)
        .unwrap();
    assert_eq!(
        dir.lookup(&bind, &[parent.local.clone(), child.local.clone()]),
        Some(0)
    );
    dir.event(
        &t,
        batch(
            10,
            vec![Op::Store(Block {
                resident: true,
                ..parent.clone()
            })],
        ),
    )
    .unwrap();
    assert_eq!(dir.lookup(&bind, &[parent.local, child.local]), Some(2));
}

#[test]
fn old_connection_cannot_install_or_invalidate_new_recovery() {
    let (mut dir, old, mut bind) = ready(8);
    bind.publisher = Arc::from("publisher-2");
    let new = dir.begin(bind.clone()).unwrap();
    assert_eq!(
        dir.queue(&old, batch(10, vec![Op::Clear])),
        Err(Fault::Stale)
    );
    assert_eq!(dir.finish(&old, vec![], 0, 0), Err(Fault::Stale));
    dir.finish(&new, vec![block(1)], 0, 0).unwrap();
    assert_eq!(
        dir.event(&old, batch(10, vec![Op::Clear])),
        Err(Fault::Stale)
    );
    assert_eq!(dir.lookup(&bind, &[block(1).local]), Some(1));
}

#[test]
fn delayed_same_publisher_handshake_is_fenced_by_generation() {
    let (mut dir, old, bind) = ready(8);
    let new = dir.begin(bind.clone()).unwrap();
    assert_eq!(dir.finish(&old, vec![], 10, 10), Err(Fault::Stale));
    dir.finish(&new, vec![block(0)], 10, 10).unwrap();
}

#[test]
fn identity_instance_and_layout_mismatch_are_unknown() {
    let (dir, _, bind) = ready(8);
    let mut alternatives = vec![bind.clone(); 5];
    alternatives[0].worker = Arc::from("other-worker");
    alternatives[1].instance += 1;
    alternatives[2].publisher = Arc::from("other-publisher");
    alternatives[3].identity = block(9).local;
    alternatives[4].block_size *= 2;
    for other in alternatives {
        assert_eq!(dir.lookup(&other, &[block(0).local]), None);
    }
}

#[test]
fn duplicate_buffer_is_idempotent_conflicting_buffer_invalidates() {
    let (mut dir, _, bind) = ready(8);
    let t = dir.begin(bind.clone()).unwrap();
    let b = batch(10, vec![Op::Store(block(1))]);
    dir.queue(&t, b.clone()).unwrap();
    dir.queue(&t, b).unwrap();
    assert_eq!(
        dir.queue(&t, batch(10, vec![Op::Clear])),
        Err(Fault::Conflict)
    );
    assert_eq!(dir.lookup(&bind, &[]), None);
}

#[test]
fn invalid_late_operation_withdraws_entire_view() {
    let (mut dir, t, bind) = ready(8);
    let mut orphan = block(2);
    orphan.parent = Some(Arc::from("absent"));
    assert_eq!(
        dir.event(&t, batch(10, vec![Op::Store(block(1)), Op::Store(orphan)])),
        Err(Fault::MissingParent)
    );
    assert_eq!(dir.lookup(&bind, &[block(1).local]), None);
}

#[test]
fn live_sequence_gap_withdraws_trust() {
    let (mut dir, t, bind) = ready(8);
    assert_eq!(dir.event(&t, batch(11, vec![Op::Clear])), Err(Fault::Gap));
    assert_eq!(dir.lookup(&bind, &[block(0).local]), None);
}

#[test]
fn clear_then_store_installs_one_atomic_final_view() {
    let (mut dir, t, bind) = ready(8);
    dir.event(&t, batch(10, vec![Op::Clear, Op::Store(block(1))]))
        .unwrap();
    assert_eq!(dir.lookup(&bind, &[block(0).local]), Some(0));
    assert_eq!(dir.lookup(&bind, &[block(1).local]), Some(1));
}

#[test]
fn live_set_over_budget_does_not_truncate_and_claim_trusted() {
    let (mut dir, t, bind) = ready(1);
    assert_eq!(
        dir.event(&t, batch(10, vec![Op::Store(block(1))])),
        Err(Fault::Budget)
    );
    assert_eq!(dir.lookup(&bind, &[]), None);
}

#[test]
fn ancestors_count_toward_budget() {
    let (mut dir, _, bind) = ready(1);
    let t = dir.begin(bind.clone()).unwrap();
    let mut parent = block(0);
    parent.resident = false;
    let mut child = block(1);
    child.parent = Some(parent.id.clone());
    assert!(dir.finish(&t, vec![parent, child], 0, 0).is_err());
    assert_eq!(dir.lookup(&bind, &[]), None);
}

#[test]
fn unique_churn_rebuilds_without_losing_live_claims() {
    let (mut dir, t, bind) = ready(4);
    for seq in 10..1010 {
        let b = block(seq as usize);
        dir.event(&t, batch(seq, vec![Op::Store(b.clone()), Op::Remove(b.id)]))
            .unwrap();
        assert!(dir.retained_records() <= 4);
        assert_eq!(dir.lookup(&bind, &[block(0).local.clone()]), Some(1));
    }
    assert!(dir.rebuilds > 100);
}

#[test]
fn queue_count_and_byte_budgets_withdraw_trust() {
    for bytes in [true, false] {
        let mut l = limits(8);
        if bytes {
            l.queue_bytes = 100;
        } else {
            l.queued_batches = 1;
        }
        let mut dir = Directory::new(l).unwrap();
        let t = dir.begin(binding()).unwrap();
        let first = dir.queue(&t, batch(10, vec![Op::Store(block(0))]));
        if bytes {
            assert_eq!(first, Err(Fault::Budget));
        } else {
            first.unwrap();
            assert_eq!(
                dir.queue(&t, batch(11, vec![Op::Clear])),
                Err(Fault::Budget)
            );
        }
        assert_eq!(dir.lookup(&binding(), &[]), None);
    }
}

#[test]
fn snapshot_plus_queue_and_second_view_share_total_charge() {
    let mut l = limits(8);
    l.total_bytes = 1500;
    l.queue_bytes = 1000;
    let mut dir = Directory::new(l).unwrap();
    let t = dir.begin(binding()).unwrap();
    dir.queue(&t, batch(10, vec![Op::Store(block(1))])).unwrap();
    assert_eq!(dir.finish(&t, vec![block(0)], 10, 11), Err(Fault::Budget));
}

#[test]
fn sequence_overflow_does_not_install_wrapped_state() {
    let (mut dir, _, bind) = ready(8);
    let t = dir.begin(bind.clone()).unwrap();
    dir.finish(&t, vec![], u64::MAX, u64::MAX).unwrap();
    assert_eq!(
        dir.event(&t, batch(u64::MAX, vec![Op::Clear])),
        Err(Fault::Overflow)
    );
    assert_eq!(dir.lookup(&bind, &[]), None);
}

#[test]
fn concurrent_queries_only_see_complete_views_no_old_arc_escapes() {
    let (dir, t, bind) = ready(8);
    let shared = Arc::new(RwLock::new(dir));
    let mut readers = vec![];
    for _ in 0..4 {
        let d = shared.clone();
        let b = bind.clone();
        readers.push(thread::spawn(move || {
            for _ in 0..1000 {
                assert_eq!(d.read().lookup(&b, &[block(0).local.clone()]), Some(1));
            }
        }));
    }
    for seq in 10..1010 {
        let b = block(seq as usize);
        shared
            .write()
            .event(&t, batch(seq, vec![Op::Store(b.clone()), Op::Remove(b.id)]))
            .unwrap();
    }
    for r in readers {
        r.join().unwrap();
    }
    assert!(shared.read().retained_records() <= 8);
}

#[test]
fn query_budget_downgrades_instead_of_truncating() {
    let (dir, _, bind) = ready(8);
    assert_eq!(dir.lookup(&bind, &vec![block(0).local; 4097]), None);
    assert_eq!(dir.lookup(&bind, &[Arc::from("not-a-verified-hash")]), None);
}

#[test]
fn same_path_with_distinct_engine_anchor_is_rejected() {
    let (mut dir, t, bind) = ready(8);
    let mut alias = block(1);
    alias.local = block(0).local;
    assert_eq!(
        dir.event(&t, batch(10, vec![Op::Store(alias)])),
        Err(Fault::Conflict)
    );
    assert_eq!(dir.lookup(&bind, &[]), None);
}

#[test]
fn branching_snapshot_and_deep_chain_preserve_contiguous_matches() {
    let (mut dir, _, bind) = ready(256);
    let t = dir.begin(bind.clone()).unwrap();
    let mut chain = Vec::new();
    for value in 0..128 {
        let mut b = block(value);
        if value > 0 {
            b.parent = Some(block(value - 1).id);
        }
        chain.push(b);
    }
    let hashes: Vec<_> = chain.iter().map(|b| b.local.clone()).collect();
    let mut sibling = block(129);
    sibling.parent = Some(block(0).id);
    chain.push(sibling.clone());
    chain.reverse();
    dir.finish(&t, chain, 10, 10).unwrap();
    assert_eq!(dir.lookup(&bind, &hashes), Some(128));
    assert_eq!(
        dir.lookup(&bind, &[block(0).local.clone(), sibling.local]),
        Some(2)
    );
    dir.event(&t, batch(10, vec![Op::Remove(block(64).id)]))
        .unwrap();
    assert_eq!(dir.lookup(&bind, &hashes), Some(64));
}

#[test]
fn compacted_away_parent_requires_recovery_not_root_fallback() {
    let (mut dir, t, bind) = ready(1);
    let old = block(1);
    dir.event(
        &t,
        batch(10, vec![Op::Store(old.clone()), Op::Remove(old.id.clone())]),
    )
    .unwrap();
    let mut child = block(2);
    child.parent = Some(old.id);
    assert_eq!(
        dir.event(&t, batch(11, vec![Op::Store(child)])),
        Err(Fault::MissingParent)
    );
    assert_eq!(dir.lookup(&bind, &[]), None);
}

#[test]
fn deep_topology_is_rejected_before_upstream_construction() {
    let mut l = limits(128);
    l.parent_depth = 2;
    let mut dir = Directory::new(l).unwrap();
    let t = dir.begin(binding()).unwrap();
    let mut a = block(1);
    a.parent = Some(block(0).id);
    let mut b = block(2);
    b.parent = Some(a.id.clone());
    assert_eq!(
        dir.finish(&t, vec![block(0), a.clone(), b.clone()], 0, 0),
        Err(Fault::Budget)
    );
    let t = dir.begin(binding()).unwrap();
    dir.finish(&t, vec![block(0), a], 0, 0).unwrap();
    assert_eq!(
        dir.event(&t, batch(0, vec![Op::Store(b)])),
        Err(Fault::Budget)
    );
    assert_eq!(dir.lookup(&binding(), &[]), None);
}
