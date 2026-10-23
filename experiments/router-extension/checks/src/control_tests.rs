// SPDX-License-Identifier: Apache-2.0
use crate::{
    prefix_extension::CloseEvidence,
    runtime_owner::{
        control_gate::{Clock, ControlSet, SourceKey, WorkerGate},
        CachePort, RuntimeOwner, TerminalResult, WorkerControl,
    },
    runtime_provider::{AdmissionPort, Decision, RequestView, SnapshotPort},
};
use std::{
    collections::BTreeMap,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
};

#[derive(Debug, Default)]
struct ManualClock(AtomicU64);
impl Clock for ManualClock {
    fn now_ns(&self) -> u64 {
        self.0.load(Ordering::Acquire)
    }
}
fn source() -> SourceKey {
    SourceKey {
        instance: "instance-a".into(),
        observer: "observer-a".into(),
        ownership: "exclusive_router_v1".into(),
    }
}
fn setup() -> (Arc<ManualClock>, Arc<WorkerGate>) {
    let clock = Arc::new(ManualClock::default());
    let gate = Arc::new(WorkerGate::new(source(), clock.clone(), 100).unwrap());
    (clock, gate)
}
fn verify(gate: &WorkerGate) {
    gate.complete(gate.begin().unwrap(), &source()).unwrap();
}

#[test]
fn starts_closed_and_expires_without_a_background_timer() {
    let (clock, gate) = setup();
    assert_eq!(gate.revision("instance-a"), None);
    verify(&gate);
    assert!(gate.revision("instance-a").is_some());
    clock.0.store(100, Ordering::Release);
    assert_eq!(gate.revision("instance-a"), None);
    verify(&gate);
    assert!(gate.revision("instance-a").is_some());
    assert_eq!(gate.revision("instance-b"), None);
}

#[test]
fn renewal_uses_send_time_and_does_not_invalidate_healthy_reservations() {
    let (clock, gate) = setup();
    verify(&gate);
    let revision = gate.revision("instance-a");
    clock.0.store(40, Ordering::Release);
    let probe = gate.begin().unwrap();
    clock.0.store(80, Ordering::Release);
    gate.complete(probe, &source()).unwrap();
    assert_eq!(gate.revision("instance-a"), revision);
    clock.0.store(140, Ordering::Release);
    assert_eq!(gate.revision("instance-a"), None);
}

#[test]
fn delayed_or_superseded_replies_cannot_reopen_the_gate() {
    let (clock, gate) = setup();
    let old = gate.begin().unwrap();
    let new = gate.begin().unwrap();
    gate.complete(new, &source()).unwrap();
    assert!(gate.complete(old, &source()).is_err());
    let delayed = gate.begin().unwrap();
    clock.0.store(101, Ordering::Release);
    assert!(gate.complete(delayed, &source()).is_err());
    assert_eq!(gate.revision("instance-a"), None);
    verify(&gate);
    assert!(gate.revision("instance-a").is_some());
}

#[test]
fn loss_recovers_only_after_new_validation_and_changes_send_fence() {
    let (_, gate) = setup();
    verify(&gate);
    let original = gate.revision("instance-a");
    gate.reject(gate.begin().unwrap()).unwrap();
    assert_eq!(gate.revision("instance-a"), None);
    verify(&gate);
    assert_ne!(gate.revision("instance-a"), original);
}

#[test]
fn changed_observer_instance_or_ownership_is_quarantined() {
    for field in 0..3 {
        let (_, gate) = setup();
        verify(&gate);
        let mut changed = source();
        match field {
            0 => changed.instance.push('x'),
            1 => changed.observer.push('x'),
            _ => changed.ownership = "observe_only".into(),
        }
        assert_eq!(
            gate.complete(gate.begin().unwrap(), &changed),
            Err("control_identity_changed")
        );
        assert_eq!(gate.revision("instance-a"), None);
        assert!(gate.begin().is_err());
    }
}

#[test]
fn tickets_from_other_gates_and_clock_regression_are_rejected() {
    let (clock, gate) = setup();
    let (_, other) = setup();
    assert!(gate.complete(other.begin().unwrap(), &source()).is_err());
    clock.0.store(10, Ordering::Release);
    verify(&gate);
    clock.0.store(9, Ordering::Release);
    assert_eq!(gate.revision("instance-a"), None);
    assert!(gate.begin().is_err());
}

#[test]
fn concurrent_reads_do_not_misclassify_clock_sampling_order_as_regression() {
    #[derive(Debug)]
    struct CountingClock(AtomicU64);
    impl Clock for CountingClock {
        fn now_ns(&self) -> u64 {
            self.0.fetch_add(1, Ordering::SeqCst)
        }
    }
    let gate = Arc::new(
        WorkerGate::new(
            source(),
            Arc::new(CountingClock(AtomicU64::new(0))),
            300_000_000_000,
        )
        .unwrap(),
    );
    verify(&gate);
    std::thread::scope(|scope| {
        for _ in 0..8 {
            let gate = gate.clone();
            scope.spawn(move || {
                for _ in 0..1000 {
                    assert!(gate.revision("instance-a").is_some());
                }
            });
        }
    });
}

#[derive(Debug)]
struct NoCache;
impl CachePort for NoCache {
    fn lookup(&self, _: &str, _: &str, _: &str, _: &[String]) -> Option<u32> {
        None
    }
}
fn owner(gate: Arc<WorkerGate>) -> RuntimeOwner {
    let owner = RuntimeOwner::new(
        Arc::new(NoCache),
        "process".into(),
        1,
        vec![WorkerControl {
            worker_url: "http://worker".into(),
            instance_epoch: "instance-a".into(),
            worker_version: 1,
            cache_identity: "identity".into(),
            healthy: true,
            draining: false,
            capacity: 8,
        }],
        16,
        16,
    )
    .unwrap();
    owner
        .install_controls(Arc::new(ControlSet(BTreeMap::from([(
            "http://worker".into(),
            gate,
        )]))))
        .unwrap();
    owner
}
fn request(id: &str) -> RequestView {
    RequestView {
        request_id: id.into(),
        identity: "identity".into(),
        block_hashes: Arc::from([]),
        exact: false,
    }
}
fn decision(owner: &RuntimeOwner, request: &RequestView) -> Decision {
    Decision {
        request_id: request.request_id.clone(),
        attempt_id: owner.allocate_attempt(&request.request_id).unwrap(),
        worker_url: "http://worker".into(),
        instance_epoch: "instance-a".into(),
        worker_version: 1,
        config_version: 1,
    }
}

#[tokio::test]
async fn loss_blocks_reserve_and_old_unsent_lease_but_keeps_sent_debt() {
    let (_, gate) = setup();
    let owner = owner(gate.clone());
    verify(&gate);
    let first = request("sent");
    let first_decision = decision(&owner, &first);
    let sent = owner.reserve_async(&first, &first_decision).await.unwrap();
    let id = sent.request_id_header().unwrap().to_owned();
    sent.before_send().unwrap();
    sent.sent();
    sent.closed(true, CloseEvidence::TransportEof);
    let pending = request("pending");
    let pending_decision = decision(&owner, &pending);
    let unsent = owner
        .reserve_async(&pending, &pending_decision)
        .await
        .unwrap();
    gate.reject(gate.begin().unwrap()).unwrap();
    assert!(!owner.candidates(&first, &["http://worker".into()]).unwrap()[0].healthy);
    let blocked = request("blocked");
    assert!(owner
        .reserve_async(&blocked, &decision(&owner, &blocked))
        .await
        .is_err());
    assert_eq!(owner.active_len(), Ok(2));
    verify(&gate);
    assert!(unsent.before_send().is_err());
    unsent.closed(false, CloseEvidence::NotSent);
    assert_eq!(owner.active_len(), Ok(1));
    assert_eq!(
        owner.apply_terminal(&id, "http://worker", "instance-a"),
        Ok(TerminalResult::Applied)
    );
    assert_eq!(
        owner.apply_terminal(&id, "http://worker", "instance-a"),
        Ok(TerminalResult::Duplicate)
    );
    assert_eq!(owner.active_len(), Ok(0));
    let after = request("after");
    assert!(owner
        .reserve_async(&after, &decision(&owner, &after))
        .await
        .is_ok());
}

#[tokio::test]
async fn drain_rejects_admission_but_reliable_terminal_still_settles() {
    let (_, gate) = setup();
    let owner = owner(gate.clone());
    verify(&gate);
    let request = request("sent");
    let lease = owner
        .reserve_async(&request, &decision(&owner, &request))
        .await
        .unwrap();
    let id = lease.request_id_header().unwrap().to_owned();
    lease.before_send().unwrap();
    lease.sent();
    gate.drain().unwrap();
    assert!(gate.begin().is_err());
    assert_eq!(owner.active_len(), Ok(1));
    assert_eq!(
        owner.apply_terminal(&id, "http://worker", "instance-a"),
        Ok(TerminalResult::Applied)
    );
    assert_eq!(owner.active_len(), Ok(0));
}

#[test]
fn draining_one_replica_does_not_close_another_and_late_probe_cannot_undo_drain() {
    let (_, first) = setup();
    let (_, second) = setup();
    verify(&first);
    verify(&second);
    let pending = first.begin().unwrap();
    first.drain().unwrap();
    assert!(first.complete(pending, &source()).is_err());
    let controls = ControlSet(BTreeMap::from([
        ("http://first".into(), first),
        ("http://second".into(), second),
    ]));
    assert_eq!(controls.revision("http://first", "instance-a"), None);
    assert!(controls.revision("http://second", "instance-a").is_some());
    controls.drain().unwrap();
    assert_eq!(controls.revision("http://second", "instance-a"), None);
}
