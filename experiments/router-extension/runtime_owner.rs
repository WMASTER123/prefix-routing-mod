// SPDX-License-Identifier: Apache-2.0
//! In-process M3 owner and M2 snapshot adapter for the first runtime chain.
//!
//! This owns admission state, not the KV tree or durable storage medium. A
//! checkpoint is explicit so persistence can be selected and measured without
//! changing request-path semantics. Transport closure never releases sent work.
use super::{
    prefix_extension::{AdmissionFuture, CloseEvidence, Lease},
    runtime_provider::{AdmissionPort, Candidate, Decision, RequestView, SnapshotPort},
};
use serde::{Deserialize, Serialize};
#[path = "control_gate.rs"]
pub mod control_gate;
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    fmt::Debug,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex, OnceLock,
    },
};

fn label(value: &str, limit: usize) -> bool {
    !value.is_empty() && value.len() <= limit && value.bytes().all(|byte| byte.is_ascii_graphic())
}

pub trait CachePort: Debug + Send + Sync {
    /// None is unknown. A trusted miss is Some(0).
    fn lookup(
        &self,
        worker_url: &str,
        instance_epoch: &str,
        identity: &str,
        block_hashes: &[String],
    ) -> Option<u32>;
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkerControl {
    pub worker_url: String,
    pub instance_epoch: String,
    pub worker_version: u64,
    pub cache_identity: String,
    pub healthy: bool,
    pub draining: bool,
    pub capacity: u32,
}

impl WorkerControl {
    fn valid(&self) -> bool {
        label(&self.worker_url, 2048)
            && label(&self.instance_epoch, 128)
            && label(&self.cache_identity, 256)
            && self.worker_version > 0
            && self.capacity > 0
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AttemptPhase {
    Reserved,
    Ready,
    Sent,
    Uncertain,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ActiveAttempt {
    pub request_id: String,
    pub attempt_id: String,
    pub worker_url: String,
    pub instance_epoch: String,
    pub worker_version: u64,
    pub config_version: u64,
    pub phase: AttemptPhase,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OwnerCheckpoint {
    pub process_epoch: String,
    pub config_version: u64,
    pub workers: Vec<WorkerControl>,
    pub active: Vec<ActiveAttempt>,
    pub retired: Vec<String>,
    pub next_attempt: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TerminalResult {
    Applied,
    Duplicate,
    Unknown,
}

#[derive(Debug)]
struct OwnerState {
    config_version: u64,
    workers: BTreeMap<String, WorkerControl>,
    active: BTreeMap<String, ActiveAttempt>,
    retired: BTreeSet<String>,
    retired_order: VecDeque<String>,
    next_attempt: u64,
}

#[derive(Debug)]
struct Shared {
    state: Mutex<OwnerState>,
    failed: AtomicBool,
    max_active: usize,
    max_retired: usize,
    controls: OnceLock<Arc<control_gate::ControlSet>>,
}

impl Shared {
    fn fail(&self) {
        self.failed.store(true, Ordering::Release);
    }

    fn retire(state: &mut OwnerState, attempt_id: String, max_retired: usize) {
        if state.retired.insert(attempt_id.clone()) {
            state.retired_order.push_back(attempt_id);
        }
        while state.retired_order.len() > max_retired {
            if let Some(oldest) = state.retired_order.pop_front() {
                state.retired.remove(&oldest);
            }
        }
    }

    fn remove_active(&self, state: &mut OwnerState, attempt_id: &str) -> Option<ActiveAttempt> {
        let attempt = state.active.remove(attempt_id)?;
        Self::retire(state, attempt.attempt_id.clone(), self.max_retired);
        Some(attempt)
    }

    fn fence_matches(state: &OwnerState, attempt: &ActiveAttempt) -> bool {
        state.config_version == attempt.config_version
            && state
                .workers
                .get(&attempt.worker_url)
                .is_some_and(|worker| {
                    worker.instance_epoch == attempt.instance_epoch
                        && worker.worker_version == attempt.worker_version
                        && worker.healthy
                        && !worker.draining
                })
    }
}

#[derive(Debug)]
pub struct RuntimeOwner {
    cache: Arc<dyn CachePort>,
    process_epoch: String,
    shared: Arc<Shared>,
}

impl RuntimeOwner {
    pub fn new(
        cache: Arc<dyn CachePort>,
        process_epoch: String,
        config_version: u64,
        workers: Vec<WorkerControl>,
        max_active: usize,
        max_retired: usize,
    ) -> Result<Self, &'static str> {
        Self::from_parts(
            cache,
            process_epoch.clone(),
            OwnerCheckpoint {
                process_epoch,
                config_version,
                workers,
                active: vec![],
                retired: vec![],
                next_attempt: 0,
            },
            max_active,
            max_retired,
            false,
        )
    }

    pub fn restore(
        cache: Arc<dyn CachePort>,
        process_epoch: String,
        checkpoint: OwnerCheckpoint,
        max_active: usize,
        max_retired: usize,
    ) -> Result<Self, &'static str> {
        Self::from_parts(
            cache,
            process_epoch,
            checkpoint,
            max_active,
            max_retired,
            true,
        )
    }

    fn from_parts(
        cache: Arc<dyn CachePort>,
        process_epoch: String,
        checkpoint: OwnerCheckpoint,
        max_active: usize,
        max_retired: usize,
        restoring: bool,
    ) -> Result<Self, &'static str> {
        if !label(&process_epoch, 64)
            || !label(&checkpoint.process_epoch, 64)
            || checkpoint.config_version == 0
            || !(1..=1_048_576).contains(&max_active)
            || !(1..=1_048_576).contains(&max_retired)
            || checkpoint.active.len() > max_active
            || checkpoint.retired.len() > max_retired
            || (restoring && process_epoch == checkpoint.process_epoch)
        {
            return Err("invalid_owner_configuration");
        }
        let mut workers = BTreeMap::new();
        for worker in checkpoint.workers {
            if !worker.valid() || workers.insert(worker.worker_url.clone(), worker).is_some() {
                return Err("invalid_worker_configuration");
            }
        }
        if workers.is_empty() {
            return Err("empty_worker_configuration");
        }
        let mut active = BTreeMap::new();
        let mut requests = BTreeSet::new();
        for mut attempt in checkpoint.active {
            if !label(&attempt.request_id, 128)
                || !label(&attempt.attempt_id, 128)
                || !label(&attempt.worker_url, 2048)
                || !label(&attempt.instance_epoch, 128)
                || attempt.worker_version == 0
                || attempt.config_version == 0
                || !requests.insert(attempt.request_id.clone())
                || active.contains_key(&attempt.attempt_id)
            {
                return Err("invalid_active_checkpoint");
            }
            if restoring {
                // A process boundary destroys proof that a pre-send callback
                // had not crossed into the network. Preserve the debt and
                // require reliable terminal reconciliation.
                attempt.phase = AttemptPhase::Uncertain;
            }
            active.insert(attempt.attempt_id.clone(), attempt);
        }
        let mut retired = BTreeSet::new();
        let mut retired_order = VecDeque::new();
        for attempt_id in checkpoint.retired {
            if !label(&attempt_id, 128)
                || active.contains_key(&attempt_id)
                || !retired.insert(attempt_id.clone())
            {
                return Err("invalid_retired_checkpoint");
            }
            retired_order.push_back(attempt_id);
        }
        let next_attempt = if restoring {
            0
        } else {
            checkpoint.next_attempt
        };
        Ok(Self {
            cache,
            process_epoch,
            shared: Arc::new(Shared {
                state: Mutex::new(OwnerState {
                    config_version: checkpoint.config_version,
                    workers,
                    active,
                    retired,
                    retired_order,
                    next_attempt,
                }),
                failed: AtomicBool::new(false),
                max_active,
                max_retired,
                controls: OnceLock::new(),
            }),
        })
    }

    pub fn checkpoint(&self) -> Result<OwnerCheckpoint, &'static str> {
        let state = self.shared.state.lock().map_err(|_| "owner_poisoned")?;
        Ok(OwnerCheckpoint {
            process_epoch: self.process_epoch.clone(),
            config_version: state.config_version,
            workers: state.workers.values().cloned().collect(),
            active: state.active.values().cloned().collect(),
            retired: state.retired_order.iter().cloned().collect(),
            next_attempt: state.next_attempt,
        })
    }

    pub fn uses_controls(&self, controls: &Arc<control_gate::ControlSet>) -> bool {
        self.shared
            .controls
            .get()
            .is_some_and(|installed| Arc::ptr_eq(installed, controls))
    }

    /// Install exactly once, before exposing this owner to the HTTP host.
    /// Recovered uncertain debt is allowed; live reservations are not.
    pub fn install_controls(
        &self,
        controls: Arc<control_gate::ControlSet>,
    ) -> Result<(), &'static str> {
        let state = self.shared.state.lock().map_err(|_| "owner_poisoned")?;
        if state
            .active
            .values()
            .any(|attempt| attempt.phase != AttemptPhase::Uncertain)
            || controls.0.len() != state.workers.len()
            || state
                .workers
                .keys()
                .any(|worker| !controls.0.contains_key(worker))
        {
            return Err("control_install_conflict");
        }
        self.shared
            .controls
            .set(controls)
            .map_err(|_| "controls_already_installed")
    }

    pub fn publish(
        &self,
        config_version: u64,
        workers: Vec<WorkerControl>,
    ) -> Result<(), &'static str> {
        if self.shared.failed.load(Ordering::Acquire) || config_version == 0 {
            return Err("owner_unavailable");
        }
        let mut replacement = BTreeMap::new();
        for worker in workers {
            if !worker.valid()
                || replacement
                    .insert(worker.worker_url.clone(), worker)
                    .is_some()
            {
                return Err("invalid_worker_configuration");
            }
        }
        if replacement.is_empty() {
            return Err("empty_worker_configuration");
        }
        let mut state = self.shared.state.lock().map_err(|_| "owner_poisoned")?;
        if config_version <= state.config_version {
            return Err("config_version_not_monotonic");
        }
        state.config_version = config_version;
        state.workers = replacement;
        Ok(())
    }

    pub fn apply_terminal(
        &self,
        attempt_id: &str,
        worker_url: &str,
        instance_epoch: &str,
    ) -> Result<TerminalResult, &'static str> {
        let mut state = self.shared.state.lock().map_err(|_| "owner_poisoned")?;
        if let Some(attempt) = state.active.get(attempt_id) {
            if attempt.worker_url != worker_url || attempt.instance_epoch != instance_epoch {
                self.shared.fail();
                return Err("terminal_identity_conflict");
            }
            if !matches!(attempt.phase, AttemptPhase::Sent | AttemptPhase::Uncertain) {
                self.shared.fail();
                return Err("terminal_before_send");
            }
            self.shared.remove_active(&mut state, attempt_id);
            return Ok(TerminalResult::Applied);
        }
        if state.retired.contains(attempt_id) {
            return Ok(TerminalResult::Duplicate);
        }
        Ok(TerminalResult::Unknown)
    }

    pub fn active_len(&self) -> Result<usize, &'static str> {
        self.shared
            .state
            .lock()
            .map(|state| state.active.len())
            .map_err(|_| "owner_poisoned")
    }

    /// Stop new reads/admissions after an uncertain durable transition.
    /// Reliable terminal processing may continue after a process restart from
    /// the last committed snapshot; this instance must not be reused.
    pub fn fail_closed(&self) {
        self.shared.fail();
    }

    pub fn is_failed(&self) -> bool {
        self.shared.failed.load(Ordering::Acquire)
    }
}

impl SnapshotPort for RuntimeOwner {
    fn candidates(
        &self,
        features: &RequestView,
        available: &[String],
    ) -> Result<Vec<Candidate>, &'static str> {
        if self.shared.failed.load(Ordering::Acquire) {
            return Err("owner_unavailable");
        }
        let available: BTreeSet<&str> = available.iter().map(String::as_str).collect();
        let (config_version, workers, active) = {
            let state = self.shared.state.lock().map_err(|_| "owner_poisoned")?;
            (
                state.config_version,
                state.workers.values().cloned().collect::<Vec<_>>(),
                state.active.values().cloned().collect::<Vec<_>>(),
            )
        };
        let mut result = Vec::new();
        for worker in workers {
            if !available.contains(worker.worker_url.as_str()) {
                continue;
            }
            let local_outstanding = active
                .iter()
                .filter(|attempt| {
                    attempt.worker_url == worker.worker_url
                        && attempt.instance_epoch == worker.instance_epoch
                })
                .count()
                .try_into()
                .map_err(|_| "outstanding_overflow")?;
            let matched_blocks = if features.exact && features.identity == worker.cache_identity {
                self.cache.lookup(
                    &worker.worker_url,
                    &worker.instance_epoch,
                    &features.identity,
                    &features.block_hashes,
                )
            } else {
                None
            };
            let healthy = worker.healthy
                && self.shared.controls.get().is_none_or(|controls| {
                    controls
                        .revision(&worker.worker_url, &worker.instance_epoch)
                        .is_some()
                });
            result.push(Candidate {
                worker_url: worker.worker_url,
                instance_epoch: worker.instance_epoch,
                worker_version: worker.worker_version,
                config_version,
                healthy,
                draining: worker.draining,
                local_outstanding,
                matched_blocks,
            });
        }
        Ok(result)
    }
}

impl AdmissionPort for RuntimeOwner {
    fn allocate_attempt(&self, request_id: &str) -> Result<String, &'static str> {
        if self.shared.failed.load(Ordering::Acquire) || !label(request_id, 128) {
            return Err("owner_unavailable");
        }
        let mut state = self.shared.state.lock().map_err(|_| "owner_poisoned")?;
        state.next_attempt = state
            .next_attempt
            .checked_add(1)
            .ok_or("attempt_id_exhausted")?;
        Ok(format!("prm-{}-{}", self.process_epoch, state.next_attempt))
    }

    fn reserve_async<'a>(
        &'a self,
        features: &'a RequestView,
        decision: &'a Decision,
    ) -> AdmissionFuture<'a> {
        Box::pin(async move {
            if self.shared.failed.load(Ordering::Acquire) {
                return Err("owner_unavailable");
            }
            let mut state = self.shared.state.lock().map_err(|_| "owner_poisoned")?;
            if features.request_id != decision.request_id
                || state.active.len() >= self.shared.max_active
                || state
                    .active
                    .values()
                    .any(|attempt| attempt.request_id == decision.request_id)
                || state.active.contains_key(&decision.attempt_id)
                || state.retired.contains(&decision.attempt_id)
            {
                return Err("admission_conflict");
            }
            let worker = state
                .workers
                .get(&decision.worker_url)
                .ok_or("worker_not_current")?;
            if state.config_version != decision.config_version
                || worker.instance_epoch != decision.instance_epoch
                || worker.worker_version != decision.worker_version
                || !worker.healthy
                || worker.draining
            {
                return Err("decision_fence_changed");
            }
            let local = state
                .active
                .values()
                .filter(|attempt| {
                    attempt.worker_url == decision.worker_url
                        && attempt.instance_epoch == decision.instance_epoch
                })
                .count();
            if local >= worker.capacity as usize {
                return Err("worker_capacity");
            }
            let control_revision = match self.shared.controls.get() {
                Some(controls) => Some(
                    controls
                        .revision(&decision.worker_url, &decision.instance_epoch)
                        .ok_or("control_unavailable")?,
                ),
                None => None,
            };
            let attempt = ActiveAttempt {
                request_id: decision.request_id.clone(),
                attempt_id: decision.attempt_id.clone(),
                worker_url: decision.worker_url.clone(),
                instance_epoch: decision.instance_epoch.clone(),
                worker_version: decision.worker_version,
                config_version: decision.config_version,
                phase: AttemptPhase::Reserved,
            };
            state
                .active
                .insert(decision.attempt_id.clone(), attempt.clone());
            Ok(Box::new(OwnerLease {
                attempt,
                shared: self.shared.clone(),
                control_revision,
            }) as Box<dyn Lease>)
        })
    }
}

#[derive(Debug)]
struct OwnerLease {
    attempt: ActiveAttempt,
    shared: Arc<Shared>,
    control_revision: Option<u64>,
}

impl Lease for OwnerLease {
    fn request_id_header(&self) -> Option<&str> {
        Some(&self.attempt.attempt_id)
    }

    fn before_send(&self) -> Result<(), &'static str> {
        if self.shared.failed.load(Ordering::Acquire) {
            return Err("owner_unavailable");
        }
        let mut state = self.shared.state.lock().map_err(|_| "owner_poisoned")?;
        let current = state
            .active
            .get(&self.attempt.attempt_id)
            .cloned()
            .ok_or("attempt_not_active")?;
        if current != self.attempt || current.phase != AttemptPhase::Reserved {
            return Err("attempt_phase");
        }
        let control_matches = match (self.shared.controls.get(), self.control_revision) {
            (None, None) => true,
            (Some(controls), Some(revision)) => {
                controls.revision(&current.worker_url, &current.instance_epoch) == Some(revision)
            }
            _ => false,
        };
        if !Shared::fence_matches(&state, &current) || !control_matches {
            self.shared
                .remove_active(&mut state, &self.attempt.attempt_id);
            return Err("decision_fence_changed");
        }
        state
            .active
            .get_mut(&self.attempt.attempt_id)
            .expect("checked active attempt")
            .phase = AttemptPhase::Ready;
        Ok(())
    }

    fn sent(&self) {
        let Ok(mut state) = self.shared.state.lock() else {
            self.shared.fail();
            return;
        };
        let Some(current) = state.active.get_mut(&self.attempt.attempt_id) else {
            self.shared.fail();
            return;
        };
        if current.phase != AttemptPhase::Ready {
            self.shared.fail();
            return;
        }
        current.phase = AttemptPhase::Sent;
    }

    fn headers(&self, _: u16) {}

    fn closed(&self, sent: bool, evidence: CloseEvidence) {
        let Ok(mut state) = self.shared.state.lock() else {
            self.shared.fail();
            return;
        };
        let Some(current) = state.active.get(&self.attempt.attempt_id).cloned() else {
            return;
        };
        if !sent && evidence == CloseEvidence::NotSent {
            if matches!(current.phase, AttemptPhase::Reserved | AttemptPhase::Ready) {
                self.shared
                    .remove_active(&mut state, &self.attempt.attempt_id);
            } else {
                self.shared.fail();
            }
            return;
        }
        if sent && matches!(current.phase, AttemptPhase::Sent | AttemptPhase::Uncertain) {
            state
                .active
                .get_mut(&self.attempt.attempt_id)
                .expect("checked active attempt")
                .phase = AttemptPhase::Uncertain;
        } else {
            self.shared.fail();
        }
    }
}
