// SPDX-License-Identifier: Apache-2.0
use crate::{
    durable_runtime::{DurableRuntime, TerminalRecord},
    prefix_extension::CloseEvidence,
    runtime_owner::{CachePort, RuntimeOwner, WorkerControl},
    runtime_provider::{AdmissionPort, Decision, RequestView},
    runtime_store::{RuntimeStore, StoreError, StoredRuntime, TerminalCursor},
    sqlite_row_runtime_store::SqliteRowRuntimeStore,
    sqlite_runtime_store::DEFAULT_MAX_SNAPSHOT_BYTES,
    terminal_driver::{
        DriverPhase, DriverProgress, Ownership, PumpError, RpcFault, RpcFuture, Step, StopReason,
        TerminalBatch, TerminalDriver, TerminalTask, TerminalTransport,
    },
};
use std::{
    collections::VecDeque,
    path::PathBuf,
    sync::{
        atomic::{AtomicU64, AtomicU8, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};

#[derive(Debug)]
struct EmptyCache;
impl CachePort for EmptyCache {
    fn lookup(&self, _: &str, _: &str, _: &str, _: &[String]) -> Option<u32> {
        None
    }
}

struct PrivateDirectory(PathBuf);
impl Drop for PrivateDirectory {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[derive(Debug)]
struct FaultStore {
    inner: SqliteRowRuntimeStore,
    fault: AtomicU8,
}
impl RuntimeStore for FaultStore {
    fn initialize(&self, initial: &StoredRuntime) -> Result<(), StoreError> {
        self.inner.initialize(initial)
    }
    fn load(&self) -> Result<StoredRuntime, StoreError> {
        self.inner.load()
    }
    fn commit(&self, owner: &str, generation: u64, next: &StoredRuntime) -> Result<(), StoreError> {
        let fault = self.fault.load(Ordering::Acquire);
        if fault == 1 {
            return Err(StoreError::Busy);
        }
        self.inner.commit(owner, generation, next)?;
        if fault == 2 {
            return Err(StoreError::Busy);
        }
        Ok(())
    }
    fn takeover(
        &self,
        owner: &str,
        generation: u64,
        next: &StoredRuntime,
    ) -> Result<(), StoreError> {
        self.inner.takeover(owner, generation, next)
    }
}

struct Fixture {
    runtime: Arc<DurableRuntime>,
    store: Arc<FaultStore>,
    _directory: PrivateDirectory,
}

fn cursor(through: u64) -> TerminalCursor {
    TerminalCursor {
        source_id: "source-a".into(),
        worker_url: "http://worker-a".into(),
        instance_epoch: "instance-a".into(),
        observer_epoch: "observer-a".into(),
        through,
    }
}

fn fixture() -> Fixture {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let directory = std::env::temp_dir().join(format!(
        "prefix-terminal-driver-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir(&directory).unwrap();
    let directory = PrivateDirectory(directory);
    let store = Arc::new(FaultStore {
        inner: SqliteRowRuntimeStore::create(
            directory.0.join("runtime.sqlite"),
            DEFAULT_MAX_SNAPSHOT_BYTES,
        )
        .unwrap(),
        fault: AtomicU8::new(0),
    });
    let owner = Arc::new(
        RuntimeOwner::new(
            Arc::new(EmptyCache),
            "process-a".into(),
            1,
            vec![WorkerControl {
                worker_url: "http://worker-a".into(),
                instance_epoch: "instance-a".into(),
                worker_version: 1,
                cache_identity: "identity-a".into(),
                healthy: true,
                draining: false,
                capacity: 8,
            }],
            16,
            16,
        )
        .unwrap(),
    );
    let runtime = Arc::new(
        DurableRuntime::create_with_cursors(owner, store.clone(), vec![cursor(0)]).unwrap(),
    );
    Fixture {
        runtime,
        store,
        _directory: directory,
    }
}

async fn sent(runtime: &DurableRuntime, id: &str, sequence: u64) -> TerminalRecord {
    let request = RequestView {
        request_id: id.into(),
        identity: "identity-a".into(),
        block_hashes: Arc::from([]),
        exact: false,
    };
    let decision = Decision {
        request_id: id.into(),
        attempt_id: runtime.allocate_attempt(id).unwrap(),
        worker_url: "http://worker-a".into(),
        instance_epoch: "instance-a".into(),
        worker_version: 1,
        config_version: 1,
    };
    let lease = runtime.reserve_async(&request, &decision).await.unwrap();
    let attempt_id = lease.request_id_header().unwrap().to_owned();
    lease.before_send().unwrap();
    lease.sent();
    lease.closed(true, CloseEvidence::TransportEof);
    TerminalRecord {
        sequence,
        attempt_id,
        worker_url: "http://worker-a".into(),
        instance_epoch: "instance-a".into(),
    }
}

#[derive(Default)]
struct Calls {
    values: Vec<(&'static str, u64)>,
    ack_losses: usize,
    retire_losses: usize,
}

struct Transport {
    batches: VecDeque<Result<TerminalBatch, RpcFault>>,
    calls: Arc<Mutex<Calls>>,
    store: Arc<FaultStore>,
}
impl Transport {
    fn new(store: Arc<FaultStore>, records: Vec<TerminalRecord>) -> Self {
        Self {
            batches: VecDeque::from([Ok(TerminalBatch {
                after: cursor(0),
                records,
            })]),
            calls: Arc::new(Mutex::new(Calls::default())),
            store,
        }
    }
}
impl TerminalTransport for Transport {
    fn poll<'a>(&'a mut self, after: &'a TerminalCursor) -> RpcFuture<'a, TerminalBatch> {
        Box::pin(async move {
            self.calls
                .lock()
                .unwrap()
                .values
                .push(("poll", after.through));
            self.batches.pop_front().unwrap_or_else(|| {
                Ok(TerminalBatch {
                    after: after.clone(),
                    records: vec![],
                })
            })
        })
    }
    fn acknowledge<'a>(&'a mut self, through: &'a TerminalCursor) -> RpcFuture<'a, ()> {
        Box::pin(async move {
            assert_eq!(
                self.store.load().unwrap().terminal_cursors,
                vec![through.clone()]
            );
            let mut calls = self.calls.lock().unwrap();
            calls.values.push(("ack", through.through));
            if through.through > 0 && calls.ack_losses > 0 {
                calls.ack_losses -= 1;
                return Err(RpcFault::Unavailable);
            }
            Ok(())
        })
    }
    fn retire<'a>(&'a mut self, through: &'a TerminalCursor) -> RpcFuture<'a, ()> {
        Box::pin(async move {
            assert_eq!(
                self.store.load().unwrap().terminal_cursors,
                vec![through.clone()]
            );
            let mut calls = self.calls.lock().unwrap();
            calls.values.push(("retire", through.through));
            if through.through > 0 && calls.retire_losses > 0 {
                calls.retire_losses -= 1;
                return Err(RpcFault::Unavailable);
            }
            Ok(())
        })
    }
}

#[tokio::test]
async fn commit_precedes_ack_and_exclusive_retirement() {
    let f = fixture();
    let record = sent(&f.runtime, "a", 1).await;
    let transport = Transport::new(f.store.clone(), vec![record]);
    let calls = transport.calls.clone();
    let mut driver = TerminalDriver::new(
        f.runtime.clone(),
        transport,
        cursor(0),
        Ownership::ExclusiveRouterV1,
    )
    .unwrap();
    assert_eq!(driver.step().await, Ok(Step::Committed(1)));
    assert_eq!(f.runtime.owner().active_len().unwrap(), 0);
    assert!(!driver.pending_remote_confirmation());
    assert_eq!(
        calls.lock().unwrap().values,
        [
            ("ack", 0),
            ("retire", 0),
            ("poll", 0),
            ("ack", 1),
            ("retire", 1),
        ]
    );
}

#[tokio::test]
async fn lost_ack_or_retire_is_retried_even_when_debt_is_zero() {
    for ack in [true, false] {
        let f = fixture();
        let record = sent(&f.runtime, "a", 1).await;
        let transport = Transport::new(f.store.clone(), vec![record]);
        let calls = transport.calls.clone();
        if ack {
            calls.lock().unwrap().ack_losses = 1;
        } else {
            calls.lock().unwrap().retire_losses = 1;
        }
        let mut driver = TerminalDriver::new(
            f.runtime.clone(),
            transport,
            cursor(0),
            Ownership::ExclusiveRouterV1,
        )
        .unwrap();
        assert_eq!(driver.step().await, Err(PumpError::Retry));
        assert_eq!(f.runtime.owner().active_len().unwrap(), 0);
        assert!(driver.pending_remote_confirmation());
        let generation = f.store.load().unwrap().generation;
        assert_eq!(driver.step().await, Ok(Step::Idle));
        assert_eq!(f.store.load().unwrap().generation, generation);
        let calls = &calls.lock().unwrap().values;
        assert_eq!(calls.iter().filter(|(op, _)| *op == "poll").count(), 2);
        assert_eq!(
            calls
                .iter()
                .filter(|(op, n)| *op == if ack { "ack" } else { "retire" } && *n == 1)
                .count(),
            2
        );
    }
}

#[tokio::test]
async fn restart_reissues_confirmation_at_the_persisted_cursor_without_releasing_again() {
    let f = fixture();
    let record = sent(&f.runtime, "a", 1).await;
    f.runtime
        .apply_terminal_batch(cursor(0), &[record])
        .await
        .unwrap();
    let before = f.store.load().unwrap().generation;
    let recovered = Arc::new(
        DurableRuntime::recover(
            Arc::new(EmptyCache),
            "process-b".into(),
            f.store.clone(),
            16,
            16,
        )
        .unwrap(),
    );
    assert!(!recovered.admission_ready());
    let mut transport = Transport::new(f.store.clone(), vec![]);
    transport.batches.clear();
    let calls = transport.calls.clone();
    let mut driver = TerminalDriver::new(
        recovered.clone(),
        transport,
        cursor(1),
        Ownership::ExclusiveRouterV1,
    )
    .unwrap();
    assert_eq!(driver.step().await, Ok(Step::Idle));
    assert_eq!(f.store.load().unwrap().generation, before);
    assert!(!recovered.admission_ready());
    assert_eq!(
        calls.lock().unwrap().values,
        [("ack", 1), ("retire", 1), ("poll", 1)]
    );
}

#[tokio::test]
async fn observe_only_never_retires_and_unavailable_poll_keeps_debt() {
    let f = fixture();
    let record = sent(&f.runtime, "a", 1).await;
    let mut transport = Transport::new(f.store.clone(), vec![record]);
    transport.batches.push_front(Err(RpcFault::Unavailable));
    let calls = transport.calls.clone();
    let mut driver = TerminalDriver::new(
        f.runtime.clone(),
        transport,
        cursor(0),
        Ownership::ObserveOnly,
    )
    .unwrap();
    assert_eq!(driver.step().await, Err(PumpError::Retry));
    assert_eq!(f.runtime.owner().active_len().unwrap(), 1);
    assert_eq!(driver.step().await, Ok(Step::Committed(1)));
    assert!(calls
        .lock()
        .unwrap()
        .values
        .iter()
        .all(|(op, _)| *op != "retire"));
}

#[tokio::test]
async fn invalid_source_or_later_record_fails_closed_without_acknowledging_the_batch() {
    for kind in 0..4 {
        let f = fixture();
        let first = sent(&f.runtime, "a", 1).await;
        let mut second = sent(&f.runtime, "b", 2).await;
        if kind == 0 {
            second.sequence = 3;
        }
        let mut transport = Transport::new(f.store.clone(), vec![first, second]);
        if kind == 1 {
            transport
                .batches
                .front_mut()
                .unwrap()
                .as_mut()
                .unwrap()
                .after
                .observer_epoch = "other".into();
        }
        if kind == 2 {
            transport.batches = VecDeque::from([Err(RpcFault::InvalidSource)]);
        }
        if kind == 3 {
            transport
                .batches
                .front_mut()
                .unwrap()
                .as_mut()
                .unwrap()
                .records[1]
                .attempt_id = "unknown".into();
        }
        let calls = transport.calls.clone();
        let mut driver = TerminalDriver::new(
            f.runtime.clone(),
            transport,
            cursor(0),
            Ownership::ExclusiveRouterV1,
        )
        .unwrap();
        assert!(matches!(driver.step().await, Err(PumpError::Fatal(_))));
        assert_eq!(f.store.load().unwrap().checkpoint.active.len(), 2);
        assert!(f.runtime.owner().is_failed());
        assert_eq!(
            calls.lock().unwrap().values,
            [("ack", 0), ("retire", 0), ("poll", 0)]
        );
        let count = calls.lock().unwrap().values.len();
        assert!(matches!(driver.step().await, Err(PumpError::Fatal(_))));
        assert_eq!(calls.lock().unwrap().values.len(), count);
    }
}

#[tokio::test]
async fn commit_failure_before_or_after_storage_never_acknowledges_or_keeps_serving() {
    for fault in [1, 2] {
        let f = fixture();
        let record = sent(&f.runtime, "a", 1).await;
        let transport = Transport::new(f.store.clone(), vec![record]);
        let calls = transport.calls.clone();
        let mut driver = TerminalDriver::new(
            f.runtime.clone(),
            transport,
            cursor(0),
            Ownership::ObserveOnly,
        )
        .unwrap();
        f.store.fault.store(fault, Ordering::Release);
        assert_eq!(
            driver.step().await,
            Err(PumpError::Fatal("terminal_driver_commit_failed"))
        );
        assert!(!f.runtime.admission_ready());
        assert_eq!(calls.lock().unwrap().values, [("ack", 0), ("poll", 0)]);
        assert_eq!(
            f.store.load().unwrap().terminal_cursors[0].through,
            if fault == 1 { 0 } else { 1 }
        );
    }
}

#[tokio::test]
async fn source_cursor_must_exactly_match_existing_durable_binding() {
    let f = fixture();
    for kind in 0..5 {
        let mut other = cursor(0);
        match kind {
            0 => other.source_id.push('x'),
            1 => other.worker_url.push('x'),
            2 => other.instance_epoch.push('x'),
            3 => other.observer_epoch.push('x'),
            _ => other.through = 1,
        }
        assert!(TerminalDriver::new(
            f.runtime.clone(),
            Transport::new(f.store.clone(), vec![]),
            other,
            Ownership::ObserveOnly
        )
        .is_err());
    }
}

#[tokio::test]
async fn background_loop_settles_without_any_http_trigger_and_stops_without_clearing_other_debt() {
    let f = fixture();
    let record = sent(&f.runtime, "a", 1).await;
    let _unresolved = sent(&f.runtime, "b", 2).await;
    let mut driver = TerminalDriver::new(
        f.runtime.clone(),
        Transport::new(f.store.clone(), vec![record]),
        cursor(0),
        Ownership::ObserveOnly,
    )
    .unwrap();
    let (stop, receiver) = tokio::sync::watch::channel(false);
    let owner = f.runtime.owner().clone();
    let request_stop = async move {
        while owner.active_len().unwrap() != 1 {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
        stop.send(true).unwrap();
    };
    let (result, ()) = tokio::time::timeout(Duration::from_secs(2), async {
        tokio::join!(driver.run(receiver, Duration::from_millis(5)), request_stop)
    })
    .await
    .unwrap();
    assert_eq!(result, Ok(StopReason::Requested));
    assert_eq!(f.store.load().unwrap().checkpoint.active.len(), 1);
    assert_eq!(driver.cursor(), &cursor(1));
}

#[tokio::test]
async fn stop_before_start_does_not_touch_remote_and_supervisor_loss_is_explicit() {
    for closed in [false, true] {
        let f = fixture();
        let transport = Transport::new(f.store.clone(), vec![]);
        let calls = transport.calls.clone();
        let mut driver = TerminalDriver::new(
            f.runtime.clone(),
            transport,
            cursor(0),
            Ownership::ObserveOnly,
        )
        .unwrap();
        let (stop, receiver) = tokio::sync::watch::channel(!closed);
        if closed {
            drop(stop);
        }
        assert_eq!(
            driver.run(receiver, Duration::from_millis(1)).await,
            if closed {
                Err(PumpError::Fatal("terminal_driver_supervisor_gone"))
            } else {
                Ok(StopReason::Requested)
            }
        );
        assert!(f.runtime.owner().is_failed());
        if !closed {
            assert!(calls.lock().unwrap().values.is_empty());
        }
    }
}

#[tokio::test]
async fn oversized_batch_is_rejected_before_any_debt_mutation() {
    let f = fixture();
    let record = sent(&f.runtime, "a", 1).await;
    let transport = Transport::new(f.store.clone(), vec![record; 65]);
    let before = f.store.load().unwrap().generation;
    let mut driver = TerminalDriver::new(
        f.runtime.clone(),
        transport,
        cursor(0),
        Ownership::ObserveOnly,
    )
    .unwrap();
    assert_eq!(
        driver.step().await,
        Err(PumpError::Fatal("terminal_driver_batch_invalid"))
    );
    assert_eq!(f.store.load().unwrap().generation, before);
    assert_eq!(f.runtime.owner().active_len().unwrap(), 1);
}

struct HoldAcknowledgement {
    inner: Transport,
    entered: Arc<tokio::sync::Notify>,
    release: Arc<tokio::sync::Notify>,
}
impl TerminalTransport for HoldAcknowledgement {
    fn poll<'a>(&'a mut self, after: &'a TerminalCursor) -> RpcFuture<'a, TerminalBatch> {
        self.inner.poll(after)
    }
    fn acknowledge<'a>(&'a mut self, through: &'a TerminalCursor) -> RpcFuture<'a, ()> {
        Box::pin(async move {
            if through.through == 1 {
                self.entered.notify_one();
                self.release.notified().await;
            }
            self.inner.acknowledge(through).await
        })
    }
    fn retire<'a>(&'a mut self, through: &'a TerminalCursor) -> RpcFuture<'a, ()> {
        self.inner.retire(through)
    }
}

#[tokio::test]
async fn stop_during_confirmation_joins_the_operation_instead_of_cancelling_it() {
    let f = fixture();
    let record = sent(&f.runtime, "a", 1).await;
    let entered = Arc::new(tokio::sync::Notify::new());
    let release = Arc::new(tokio::sync::Notify::new());
    let transport = HoldAcknowledgement {
        inner: Transport::new(f.store.clone(), vec![record]),
        entered: entered.clone(),
        release: release.clone(),
    };
    let mut driver = TerminalDriver::new(
        f.runtime.clone(),
        transport,
        cursor(0),
        Ownership::ExclusiveRouterV1,
    )
    .unwrap();
    let (stop, receiver) = tokio::sync::watch::channel(false);
    let task = tokio::spawn(async move {
        let result = driver.run(receiver, Duration::from_millis(5)).await;
        (result, driver)
    });
    tokio::time::timeout(Duration::from_secs(2), entered.notified())
        .await
        .unwrap();
    stop.send(true).unwrap();
    tokio::task::yield_now().await;
    assert!(
        !task.is_finished(),
        "stop must not abort pending confirmation"
    );
    release.notify_one();
    let (result, driver) = tokio::time::timeout(Duration::from_secs(2), task)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(result, Ok(StopReason::Requested));
    assert!(!driver.pending_remote_confirmation());
    assert_eq!(driver.cursor(), &cursor(1));
    assert_eq!(f.store.load().unwrap().terminal_cursors, vec![cursor(1)]);
}

#[tokio::test]
async fn aborted_pump_fails_admission_closed_and_keeps_pending_confirmation_recoverable() {
    let f = fixture();
    let record = sent(&f.runtime, "a", 1).await;
    let entered = Arc::new(tokio::sync::Notify::new());
    let transport = HoldAcknowledgement {
        inner: Transport::new(f.store.clone(), vec![record]),
        entered: entered.clone(),
        release: Arc::new(tokio::sync::Notify::new()),
    };
    let mut driver = TerminalDriver::new(
        f.runtime.clone(),
        transport,
        cursor(0),
        Ownership::ObserveOnly,
    )
    .unwrap();
    let (_stop, receiver) = tokio::sync::watch::channel(false);
    let task = tokio::spawn(async move { driver.run(receiver, Duration::from_millis(5)).await });
    tokio::time::timeout(Duration::from_secs(2), entered.notified())
        .await
        .unwrap();
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    assert!(!f.runtime.admission_ready());
    // Acknowledgement did not finish; only the already committed waterline is
    // authoritative. A fresh verified driver must retry that waterline.
    assert_eq!(f.store.load().unwrap().terminal_cursors, vec![cursor(1)]);
    assert!(f.store.load().unwrap().checkpoint.active.is_empty());
}

async fn wait_phase<T: TerminalTransport + 'static>(task: &TerminalTask<T>, phase: DriverPhase) {
    tokio::time::timeout(Duration::from_secs(2), async {
        while task.progress().phase != phase {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn supervised_task_confirms_startup_before_ready_and_settles_without_http() {
    let f = fixture();
    let record = sent(&f.runtime, "a", 1).await;
    let driver = TerminalDriver::new(
        f.runtime.clone(),
        Transport::new(f.store.clone(), vec![record]),
        cursor(0),
        Ownership::ExclusiveRouterV1,
    )
    .unwrap();
    let task = TerminalTask::spawn(driver, Duration::from_millis(20)).unwrap();
    assert_eq!(task.progress().phase, DriverPhase::Starting);
    assert!(!task
        .progress()
        .ready_at(std::time::Instant::now(), Duration::from_secs(1)));
    wait_phase(&task, DriverPhase::Idle).await;
    let progress = task.progress();
    assert!(progress.ready_at(std::time::Instant::now(), Duration::from_secs(1)));
    assert!(progress.settled());
    assert_eq!(progress.through, 1);
    assert_eq!(f.runtime.owner().active_len().unwrap(), 0);
    task.request_stop();
    assert!(!task
        .progress()
        .ready_at(std::time::Instant::now(), Duration::from_secs(1)));
    let (result, driver) = task.join().await.unwrap();
    assert_eq!(result, Ok(StopReason::Requested));
    assert!(!driver.pending_remote_confirmation());
    assert!(!f.runtime.admission_ready());
}

#[tokio::test]
async fn supervised_retry_does_not_claim_readiness_or_drain_when_debt_is_zero() {
    let f = fixture();
    let record = sent(&f.runtime, "a", 1).await;
    let transport = Transport::new(f.store.clone(), vec![record]);
    transport.calls.lock().unwrap().retire_losses = 1;
    let driver = TerminalDriver::new(
        f.runtime.clone(),
        transport,
        cursor(0),
        Ownership::ExclusiveRouterV1,
    )
    .unwrap();
    let task = TerminalTask::spawn(driver, Duration::from_millis(100)).unwrap();
    wait_phase(&task, DriverPhase::Retrying).await;
    let progress = task.progress();
    assert_eq!(f.runtime.owner().active_len().unwrap(), 0);
    assert_eq!(progress.through, 1);
    assert!(progress.pending_confirmation);
    assert!(!progress.settled());
    assert!(!progress.ready_at(std::time::Instant::now(), Duration::from_secs(1)));
    wait_phase(&task, DriverPhase::Idle).await;
    let (result, driver) = task.join().await.unwrap();
    assert_eq!(result, Ok(StopReason::Requested));
    assert!(!driver.pending_remote_confirmation());
    assert_eq!(f.store.load().unwrap().terminal_cursors, vec![cursor(1)]);
}

#[tokio::test]
async fn supervised_stop_with_unavailable_remote_reports_pending_for_restart() {
    let f = fixture();
    let transport = Transport::new(f.store.clone(), vec![]);
    // Keep the initial ACK unavailable, including when there is no local debt.
    let driver = TerminalDriver::new(
        f.runtime.clone(),
        UnavailableAck(transport),
        cursor(0),
        Ownership::ExclusiveRouterV1,
    )
    .unwrap();
    let task = TerminalTask::spawn(driver, Duration::from_millis(20)).unwrap();
    wait_phase(&task, DriverPhase::Retrying).await;
    let (result, driver) = task.join().await.unwrap();
    assert_eq!(result, Ok(StopReason::Requested));
    assert!(driver.pending_remote_confirmation());
    assert_eq!(driver.cursor(), &cursor(0));
    assert!(!f.runtime.admission_ready());
}

struct UnavailableAck(Transport);
impl TerminalTransport for UnavailableAck {
    fn poll<'a>(&'a mut self, cursor: &'a TerminalCursor) -> RpcFuture<'a, TerminalBatch> {
        self.0.poll(cursor)
    }
    fn acknowledge<'a>(&'a mut self, _: &'a TerminalCursor) -> RpcFuture<'a, ()> {
        Box::pin(async { Err(RpcFault::Unavailable) })
    }
    fn retire<'a>(&'a mut self, cursor: &'a TerminalCursor) -> RpcFuture<'a, ()> {
        self.0.retire(cursor)
    }
}

#[test]
fn progress_readiness_has_a_monotonic_deadline_and_busy_is_not_drain() {
    let now = std::time::Instant::now();
    let mut progress = DriverProgress {
        phase: DriverPhase::Idle,
        through: 0,
        pending_confirmation: false,
        last_success: Some(now),
    };
    assert!(progress.ready_at(now, Duration::from_millis(10)));
    assert!(!progress.ready_at(now + Duration::from_millis(10), Duration::from_millis(10)));
    assert!(!progress.ready_at(now - Duration::from_millis(1), Duration::from_millis(10)));
    assert!(!progress.ready_at(now, Duration::ZERO));
    progress.phase = DriverPhase::Working;
    assert!(progress.ready_at(now, Duration::from_millis(10)));
    assert!(!progress.settled());
    for phase in [
        DriverPhase::Starting,
        DriverPhase::Retrying,
        DriverPhase::Stopped,
        DriverPhase::Failed,
    ] {
        progress.phase = phase;
        assert!(!progress.ready_at(now, Duration::from_millis(10)));
        assert!(!progress.settled());
    }
}

#[tokio::test]
async fn dropping_supervisor_closes_admission_without_aborting_held_confirmation() {
    let f = fixture();
    let record = sent(&f.runtime, "a", 1).await;
    let entered = Arc::new(tokio::sync::Notify::new());
    let release = Arc::new(tokio::sync::Notify::new());
    let inner = Transport::new(f.store.clone(), vec![record]);
    let calls = inner.calls.clone();
    let transport = HoldAcknowledgement {
        inner,
        entered: entered.clone(),
        release: release.clone(),
    };
    let driver = TerminalDriver::new(
        f.runtime.clone(),
        transport,
        cursor(0),
        Ownership::ExclusiveRouterV1,
    )
    .unwrap();
    let task = TerminalTask::spawn(driver, Duration::from_millis(10)).unwrap();
    tokio::time::timeout(Duration::from_secs(2), entered.notified())
        .await
        .unwrap();
    assert!(!task.progress().settled());
    drop(task);
    assert!(!f.runtime.admission_ready());
    release.notify_one();
    tokio::time::timeout(Duration::from_secs(2), async {
        while !calls.lock().unwrap().values.contains(&("retire", 1)) {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(f.store.load().unwrap().terminal_cursors, vec![cursor(1)]);
}

#[tokio::test]
async fn cancelling_join_retains_stop_and_does_not_cancel_remote_confirmation() {
    let f = fixture();
    let record = sent(&f.runtime, "a", 1).await;
    let entered = Arc::new(tokio::sync::Notify::new());
    let release = Arc::new(tokio::sync::Notify::new());
    let inner = Transport::new(f.store.clone(), vec![record]);
    let calls = inner.calls.clone();
    let transport = HoldAcknowledgement {
        inner,
        entered: entered.clone(),
        release: release.clone(),
    };
    let driver = TerminalDriver::new(
        f.runtime.clone(),
        transport,
        cursor(0),
        Ownership::ExclusiveRouterV1,
    )
    .unwrap();
    let task = TerminalTask::spawn(driver, Duration::from_millis(10)).unwrap();
    tokio::time::timeout(Duration::from_secs(2), entered.notified())
        .await
        .unwrap();
    let joining = tokio::spawn(task.join());
    tokio::task::yield_now().await;
    joining.abort();
    assert!(matches!(joining.await, Err(error) if error.is_cancelled()));
    assert!(!f.runtime.admission_ready());
    release.notify_one();
    tokio::time::timeout(Duration::from_secs(2), async {
        while !calls.lock().unwrap().values.contains(&("retire", 1)) {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(f.store.load().unwrap().terminal_cursors, vec![cursor(1)]);
}

struct PanicPoll(Transport);
impl TerminalTransport for PanicPoll {
    fn poll<'a>(&'a mut self, _: &'a TerminalCursor) -> RpcFuture<'a, TerminalBatch> {
        Box::pin(async { panic!("fixture task panic") })
    }
    fn acknowledge<'a>(&'a mut self, cursor: &'a TerminalCursor) -> RpcFuture<'a, ()> {
        self.0.acknowledge(cursor)
    }
    fn retire<'a>(&'a mut self, cursor: &'a TerminalCursor) -> RpcFuture<'a, ()> {
        self.0.retire(cursor)
    }
}

#[tokio::test]
async fn task_panic_closes_admission_and_join_reports_failure_without_erasing_debt() {
    let f = fixture();
    let _record = sent(&f.runtime, "a", 1).await;
    let driver = TerminalDriver::new(
        f.runtime.clone(),
        PanicPoll(Transport::new(f.store.clone(), vec![])),
        cursor(0),
        Ownership::ObserveOnly,
    )
    .unwrap();
    let task = TerminalTask::spawn(driver, Duration::from_millis(10)).unwrap();
    wait_phase(&task, DriverPhase::Failed).await;
    assert!(matches!(
        task.join().await,
        Err("terminal_driver_task_failed")
    ));
    assert!(!f.runtime.admission_ready());
    assert_eq!(f.store.load().unwrap().checkpoint.active.len(), 1);
}

#[tokio::test]
async fn invalid_task_interval_and_unpolled_supervisor_drop_fail_closed() {
    for invalid in [true, false] {
        let f = fixture();
        let transport = Transport::new(f.store.clone(), vec![]);
        let calls = transport.calls.clone();
        let driver = TerminalDriver::new(
            f.runtime.clone(),
            transport,
            cursor(0),
            Ownership::ObserveOnly,
        )
        .unwrap();
        if invalid {
            assert!(TerminalTask::spawn(driver, Duration::ZERO).is_err());
        } else {
            drop(TerminalTask::spawn(driver, Duration::from_secs(1)).unwrap());
            tokio::task::yield_now().await;
        }
        assert!(!f.runtime.admission_ready());
        assert!(calls.lock().unwrap().values.is_empty());
    }
}

#[tokio::test]
async fn retry_after_a_success_invalidates_the_prior_readiness_proof() {
    let f = fixture();
    let mut transport = Transport::new(f.store.clone(), vec![]);
    transport.batches.push_back(Err(RpcFault::Unavailable));
    let driver = TerminalDriver::new(
        f.runtime.clone(),
        transport,
        cursor(0),
        Ownership::ObserveOnly,
    )
    .unwrap();
    let task = TerminalTask::spawn(driver, Duration::from_millis(100)).unwrap();
    wait_phase(&task, DriverPhase::Idle).await;
    assert!(task.progress().last_success.is_some());
    wait_phase(&task, DriverPhase::Retrying).await;
    let mut progress = task.progress();
    assert!(progress.last_success.is_none());
    // Even the next in-flight RPC cannot reuse the pre-failure success time.
    progress.phase = DriverPhase::Working;
    assert!(!progress.ready_at(std::time::Instant::now(), Duration::from_secs(60)));
    wait_phase(&task, DriverPhase::Idle).await;
    assert!(task.progress().last_success.is_some());
    assert!(task.join().await.unwrap().0.is_ok());
}
