// SPDX-License-Identifier: Apache-2.0
//! Candidate durable M3 wrapper: commit before permit, and terminal+cursor atomicity.
use super::{
    prefix_extension::{AdmissionFuture, CloseEvidence, Lease},
    runtime_owner::{CachePort, RuntimeOwner, TerminalResult, WorkerControl},
    runtime_provider::{AdmissionPort, Candidate, Decision, RequestView, SnapshotPort},
    runtime_store::{RuntimeStore, StoreError, StoredRuntime, TerminalCursor, RUNTIME_SCHEMA},
};
use std::{
    collections::BTreeMap,
    fmt::Debug,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};

const MAX_COMMIT_WAITERS: usize = 64;
const COMMIT_DEADLINE: Duration = Duration::from_secs(5);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TerminalRecord {
    pub sequence: u64,
    pub attempt_id: String,
    pub worker_url: String,
    pub instance_epoch: String,
}

#[derive(Debug, Clone)]
struct DurableMeta {
    stored_owner: String,
    generation: u64,
    cursors: BTreeMap<String, TerminalCursor>,
}

#[derive(Debug)]
pub struct DurableRuntime {
    owner: Arc<RuntimeOwner>,
    store: Arc<dyn RuntimeStore>,
    local_epoch: String,
    meta: Mutex<DurableMeta>,
    commit_gate: tokio::sync::Mutex<()>,
    commit_slots: Arc<tokio::sync::Semaphore>,
    admission_ready: AtomicBool,
    service_claimed: AtomicBool,
}

/// A durable transition mutates the in-memory owner before an awaited store
/// commit. Dropping that future must therefore retire this runtime instance:
/// the blocking store operation may still finish after task cancellation.
struct MutationFence<'a> {
    runtime: &'a DurableRuntime,
    armed: bool,
}

impl<'a> MutationFence<'a> {
    fn armed(runtime: &'a DurableRuntime) -> Self {
        Self {
            runtime,
            armed: true,
        }
    }

    fn disarm(&mut self) {
        self.armed = false;
    }
}

impl Drop for MutationFence<'_> {
    fn drop(&mut self) {
        if self.armed {
            self.runtime.fail_closed();
        }
    }
}

impl DurableRuntime {
    pub fn create(
        owner: Arc<RuntimeOwner>,
        store: Arc<dyn RuntimeStore>,
    ) -> Result<Self, StoreError> {
        Self::create_with_cursors(owner, store, Vec::new())
    }

    pub fn create_with_cursors(
        owner: Arc<RuntimeOwner>,
        store: Arc<dyn RuntimeStore>,
        terminal_cursors: Vec<TerminalCursor>,
    ) -> Result<Self, StoreError> {
        if terminal_cursors.iter().any(|cursor| cursor.through != 0) {
            return Err(StoreError::Invalid("initial_terminal_cursor_nonzero"));
        }
        let checkpoint = owner
            .checkpoint()
            .map_err(|_| StoreError::Invalid("owner_checkpoint_failed"))?;
        let initial = StoredRuntime {
            schema_version: RUNTIME_SCHEMA,
            owner_epoch: checkpoint.process_epoch.clone(),
            generation: 1,
            checkpoint,
            terminal_cursors,
        };
        initial.validate()?;
        store.initialize(&initial)?;
        let cursors = initial
            .terminal_cursors
            .iter()
            .cloned()
            .map(|cursor| (cursor.source_id.clone(), cursor))
            .collect();
        Ok(Self {
            owner,
            store,
            local_epoch: initial.owner_epoch.clone(),
            meta: Mutex::new(DurableMeta {
                stored_owner: initial.owner_epoch,
                generation: 1,
                cursors,
            }),
            commit_gate: tokio::sync::Mutex::new(()),
            commit_slots: Arc::new(tokio::sync::Semaphore::new(MAX_COMMIT_WAITERS)),
            admission_ready: AtomicBool::new(true),
            service_claimed: AtomicBool::new(false),
        })
    }

    pub fn recover(
        cache: Arc<dyn CachePort>,
        local_epoch: String,
        store: Arc<dyn RuntimeStore>,
        max_active: usize,
        max_retired: usize,
    ) -> Result<Self, StoreError> {
        let stored = store.load()?;
        let mut paused = stored.checkpoint.clone();
        for worker in &mut paused.workers {
            worker.healthy = false;
            worker.draining = true;
        }
        let owner = Arc::new(
            RuntimeOwner::restore(cache, local_epoch.clone(), paused, max_active, max_retired)
                .map_err(StoreError::Invalid)?,
        );
        let cursors = stored
            .terminal_cursors
            .into_iter()
            .map(|cursor| (cursor.source_id.clone(), cursor))
            .collect();
        Ok(Self {
            owner,
            store,
            local_epoch,
            meta: Mutex::new(DurableMeta {
                stored_owner: stored.owner_epoch,
                generation: stored.generation,
                cursors,
            }),
            commit_gate: tokio::sync::Mutex::new(()),
            commit_slots: Arc::new(tokio::sync::Semaphore::new(MAX_COMMIT_WAITERS)),
            admission_ready: AtomicBool::new(false),
            service_claimed: AtomicBool::new(false),
        })
    }

    pub fn owner(&self) -> &Arc<RuntimeOwner> {
        &self.owner
    }

    pub fn admission_ready(&self) -> bool {
        self.admission_ready.load(Ordering::Acquire) && !self.owner.is_failed()
    }

    /// A stopped service retires this runtime; consumers cannot be respawned
    /// under the same owner. This process-local claim is not recovery evidence.
    pub fn claim_service(&self) -> Result<(), &'static str> {
        self.service_claimed
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map(|_| ())
            .map_err(|_| "service_already_claimed")
    }

    /// Bounded source inventory; the stored cursors remain the recovery authority.
    pub fn terminal_cursors(&self) -> Result<Vec<TerminalCursor>, &'static str> {
        Ok(self
            .meta
            .lock()
            .map_err(|_| "durable_meta_poisoned")?
            .cursors
            .values()
            .cloned()
            .collect())
    }

    fn snapshot(
        &self,
        generation: u64,
        cursors: &BTreeMap<String, TerminalCursor>,
    ) -> Result<StoredRuntime, StoreError> {
        let checkpoint = self
            .owner
            .checkpoint()
            .map_err(|_| StoreError::Invalid("owner_checkpoint_failed"))?;
        Ok(StoredRuntime {
            schema_version: RUNTIME_SCHEMA,
            owner_epoch: self.local_epoch.clone(),
            generation,
            checkpoint,
            terminal_cursors: cursors.values().cloned().collect(),
        })
    }

    fn fail_closed(&self) {
        self.admission_ready.store(false, Ordering::Release);
        self.owner.fail_closed();
    }

    async fn persist(
        &self,
        expected_owner: String,
        expected_generation: u64,
        next: StoredRuntime,
        takeover: bool,
    ) -> Result<(), &'static str> {
        let store = self.store.clone();
        let task = tokio::task::spawn_blocking(move || {
            if takeover {
                store.takeover(&expected_owner, expected_generation, &next)
            } else {
                store.commit(&expected_owner, expected_generation, &next)
            }
        });
        match tokio::time::timeout(COMMIT_DEADLINE, task).await {
            Ok(Ok(Ok(()))) => Ok(()),
            Ok(Ok(Err(_))) | Ok(Err(_)) | Err(_) => Err("durable_store_failed"),
        }
    }

    pub async fn resume(
        &self,
        config_version: u64,
        workers: Vec<WorkerControl>,
        previous_dispatcher_stopped: bool,
    ) -> Result<(), &'static str> {
        let _slot = self
            .commit_slots
            .try_acquire()
            .map_err(|_| "durable_queue_full")?;
        let _gate = self.commit_gate.lock().await;
        if self.admission_ready() || !previous_dispatcher_stopped {
            return Err("runtime_takeover_not_allowed");
        }
        let previous = self.owner.checkpoint().map_err(|_| "owner_unavailable")?;
        if workers.len() != previous.workers.len()
            || workers.iter().any(|worker| {
                !worker.healthy
                    || worker.draining
                    || !previous.workers.iter().any(|old| {
                        old.worker_url == worker.worker_url
                            && old.instance_epoch == worker.instance_epoch
                            && old.cache_identity == worker.cache_identity
                    })
            })
        {
            return Err("runtime_takeover_identity");
        }
        self.owner
            .publish(config_version, workers)
            .map_err(|_| "runtime_takeover_control")?;
        let mut mutation = MutationFence::armed(self);
        let (stored_owner, generation, cursors) = match self.meta.lock() {
            Ok(meta) => (
                meta.stored_owner.clone(),
                meta.generation,
                meta.cursors.clone(),
            ),
            Err(_) => {
                self.fail_closed();
                return Err("owner_poisoned");
            }
        };
        let Some(next_generation) = generation.checked_add(1) else {
            self.fail_closed();
            return Err("runtime_generation_exhausted");
        };
        let next = match self.snapshot(next_generation, &cursors) {
            Ok(next) => next,
            Err(_) => {
                self.fail_closed();
                return Err("durable_store_failed");
            }
        };
        if self
            .persist(stored_owner, generation, next.clone(), true)
            .await
            .is_err()
        {
            self.fail_closed();
            return Err("durable_store_failed");
        }
        let Ok(mut meta) = self.meta.lock() else {
            self.fail_closed();
            return Err("owner_poisoned");
        };
        meta.stored_owner = self.local_epoch.clone();
        meta.generation = next.generation;
        self.admission_ready.store(true, Ordering::Release);
        mutation.disarm();
        Ok(())
    }

    pub fn terminal_cursor(&self, source_id: &str) -> Result<Option<TerminalCursor>, &'static str> {
        self.meta
            .lock()
            .map(|meta| meta.cursors.get(source_id).cloned())
            .map_err(|_| "owner_poisoned")
    }

    pub async fn apply_terminal_batch(
        &self,
        cursor: TerminalCursor,
        records: &[TerminalRecord],
    ) -> Result<u64, &'static str> {
        let _slot = self
            .commit_slots
            .try_acquire()
            .map_err(|_| "durable_queue_full")?;
        let _gate = self.commit_gate.lock().await;
        if !cursor.valid() || records.is_empty() || records.len() > 64 {
            return Err("invalid_terminal_batch");
        }
        let (stored_owner, generation, mut cursors) = {
            let meta = self.meta.lock().map_err(|_| "owner_poisoned")?;
            (
                meta.stored_owner.clone(),
                meta.generation,
                meta.cursors.clone(),
            )
        };
        let through = match cursors.get(&cursor.source_id) {
            Some(current)
                if current.worker_url == cursor.worker_url
                    && current.instance_epoch == cursor.instance_epoch
                    && current.observer_epoch == cursor.observer_epoch =>
            {
                current.through
            }
            Some(_) => {
                self.fail_closed();
                return Err("terminal_source_conflict");
            }
            None if cursors.values().any(|current| {
                current.worker_url == cursor.worker_url
                    && current.instance_epoch == cursor.instance_epoch
            }) =>
            {
                self.fail_closed();
                return Err("terminal_source_conflict");
            }
            None => 0,
        };
        if cursor.through != through {
            return Err("terminal_cursor_conflict");
        }
        // Validate the complete batch before changing any in-memory debt. This
        // keeps malformed later records from partially settling earlier ones.
        for (offset, record) in records.iter().enumerate() {
            let expected = through
                .checked_add(offset as u64 + 1)
                .ok_or("terminal_sequence_exhausted")?;
            if record.sequence != expected
                || record.worker_url != cursor.worker_url
                || record.instance_epoch != cursor.instance_epoch
            {
                self.fail_closed();
                return Err("invalid_terminal_sequence");
            }
        }
        let checkpoint = self.owner.checkpoint().map_err(|_| "owner_unavailable")?;
        for record in records {
            match checkpoint
                .active
                .iter()
                .find(|attempt| attempt.attempt_id == record.attempt_id)
            {
                Some(attempt)
                    if attempt.worker_url == record.worker_url
                        && attempt.instance_epoch == record.instance_epoch => {}
                None if checkpoint.retired.contains(&record.attempt_id) => {}
                _ => {
                    self.fail_closed();
                    return Err("unknown_or_conflicting_terminal_attempt");
                }
            }
        }
        let mut mutation = MutationFence::armed(self);
        for record in records {
            match self.owner.apply_terminal(
                &record.attempt_id,
                &record.worker_url,
                &record.instance_epoch,
            ) {
                Ok(TerminalResult::Applied | TerminalResult::Duplicate) => {}
                Ok(TerminalResult::Unknown) => {
                    self.fail_closed();
                    return Err("unknown_terminal_attempt");
                }
                Err(error) => {
                    self.fail_closed();
                    return Err(error);
                }
            }
        }
        let mut next_cursor = cursor;
        next_cursor.through = through + records.len() as u64;
        cursors.insert(next_cursor.source_id.clone(), next_cursor.clone());
        let Some(next_generation) = generation.checked_add(1) else {
            self.fail_closed();
            return Err("runtime_generation_exhausted");
        };
        let next = match self.snapshot(next_generation, &cursors) {
            Ok(next) => next,
            Err(_) => {
                self.fail_closed();
                return Err("durable_store_failed");
            }
        };
        if self
            .persist(stored_owner, generation, next.clone(), false)
            .await
            .is_err()
        {
            self.fail_closed();
            return Err("durable_store_failed");
        }
        let Ok(mut meta) = self.meta.lock() else {
            self.fail_closed();
            return Err("owner_poisoned");
        };
        meta.generation = next.generation;
        meta.cursors = cursors;
        mutation.disarm();
        Ok(next_cursor.through)
    }
}

impl SnapshotPort for DurableRuntime {
    fn candidates(
        &self,
        features: &RequestView,
        available: &[String],
    ) -> Result<Vec<Candidate>, &'static str> {
        if !self.admission_ready() {
            return Err("runtime_paused");
        }
        self.owner.candidates(features, available)
    }
}

impl AdmissionPort for DurableRuntime {
    fn allocate_attempt(&self, request_id: &str) -> Result<String, &'static str> {
        if !self.admission_ready() {
            return Err("runtime_paused");
        }
        self.owner.allocate_attempt(request_id)
    }

    fn reserve_async<'a>(
        &'a self,
        features: &'a RequestView,
        decision: &'a Decision,
    ) -> AdmissionFuture<'a> {
        Box::pin(async move {
            let _slot = self
                .commit_slots
                .try_acquire()
                .map_err(|_| "durable_queue_full")?;
            let _gate = self.commit_gate.lock().await;
            if !self.admission_ready() {
                return Err("runtime_paused");
            }
            let mut mutation = MutationFence::armed(self);
            let lease = match self.owner.reserve_async(features, decision).await {
                Ok(lease) => lease,
                Err(error) => {
                    mutation.disarm();
                    return Err(error);
                }
            };
            let (stored_owner, generation, cursors) = match self.meta.lock() {
                Ok(meta) => (
                    meta.stored_owner.clone(),
                    meta.generation,
                    meta.cursors.clone(),
                ),
                Err(_) => {
                    lease.closed(false, CloseEvidence::NotSent);
                    self.fail_closed();
                    return Err("owner_poisoned");
                }
            };
            let Some(next_generation) = generation.checked_add(1) else {
                lease.closed(false, CloseEvidence::NotSent);
                self.fail_closed();
                return Err("runtime_generation_exhausted");
            };
            let next = match self.snapshot(next_generation, &cursors) {
                Ok(next) => next,
                Err(_) => {
                    lease.closed(false, CloseEvidence::NotSent);
                    self.fail_closed();
                    return Err("durable_store_failed");
                }
            };
            if self
                .persist(stored_owner, generation, next.clone(), false)
                .await
                .is_err()
            {
                lease.closed(false, CloseEvidence::NotSent);
                self.fail_closed();
                return Err("durable_store_failed");
            }
            let Ok(mut meta) = self.meta.lock() else {
                self.fail_closed();
                return Err("owner_poisoned");
            };
            meta.generation = next.generation;
            mutation.disarm();
            Ok(Box::new(DurableLease { inner: lease }) as Box<dyn Lease>)
        })
    }
}

#[derive(Debug)]
struct DurableLease {
    inner: Box<dyn Lease>,
}

impl Lease for DurableLease {
    fn request_id_header(&self) -> Option<&str> {
        self.inner.request_id_header()
    }
    fn before_send(&self) -> Result<(), &'static str> {
        self.inner.before_send()
    }
    fn sent(&self) {
        self.inner.sent();
    }
    fn headers(&self, status: u16) {
        self.inner.headers(status);
    }
    fn closed(&self, sent: bool, evidence: CloseEvidence) {
        self.inner.closed(sent, evidence);
    }
}
