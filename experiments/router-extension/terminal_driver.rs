// SPDX-License-Identifier: Apache-2.0
//! Single-source terminal pump; no second ledger or terminal history.
//! The host owns exactly one driver per source and must join, not abort, it.
use super::{
    durable_runtime::{DurableRuntime, TerminalRecord},
    runtime_store::TerminalCursor,
};
use std::{
    future::Future,
    pin::Pin,
    sync::Arc,
    time::{Duration, Instant},
};

pub type RpcFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T, RpcFault>> + Send + 'a>>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RpcFault {
    /// No reliable reply. Retry the same idempotent operation/waterline.
    Unavailable,
    /// Wrong identity, malformed batch, gap, or rejected recovery cursor.
    InvalidSource,
}

#[derive(Debug)]
pub struct TerminalBatch {
    /// Full verified binding and the requested starting waterline, not the end.
    pub after: TerminalCursor,
    pub records: Vec<TerminalRecord>,
}

/// Adapter must bound payload, records and RPC duration, validate the actual
/// PID/source identity and complete family, and never treat EOF as ENDED.
/// `acknowledge` and `retire` must verify the exact requested waterline even
/// when newer terminal records are concurrently present in the source.
pub trait TerminalTransport: Send {
    fn poll<'a>(&'a mut self, after: &'a TerminalCursor) -> RpcFuture<'a, TerminalBatch>;
    fn acknowledge<'a>(&'a mut self, through: &'a TerminalCursor) -> RpcFuture<'a, ()>;
    fn retire<'a>(&'a mut self, through: &'a TerminalCursor) -> RpcFuture<'a, ()>;
    /// Optional bounded diagnostics for the last poll, called once only after
    /// its durable commit. Must not perform I/O or change admission/source state.
    fn committed(&mut self) {}
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ownership {
    ObserveOnly,
    ExclusiveRouterV1,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Pending {
    Acknowledge,
    Retire,
    None,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PumpError {
    Retry,
    Fatal(&'static str),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Step {
    Idle,
    Committed(usize),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StopReason {
    Requested,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DriverPhase {
    Starting,
    Working,
    Idle,
    Retrying,
    Stopped,
    Failed,
}

/// One replaceable snapshot per source, never an event queue or a second cursor
/// authority. Only DurableRuntime's stored cursor can be used for recovery.
#[derive(Debug, Clone)]
pub struct DriverProgress {
    pub phase: DriverPhase,
    pub through: u64,
    pub pending_confirmation: bool,
    pub last_success: Option<Instant>,
}

impl DriverProgress {
    pub fn ready_at(&self, now: Instant, max_age: Duration) -> bool {
        !max_age.is_zero()
            && matches!(self.phase, DriverPhase::Idle | DriverPhase::Working)
            && self.last_success.is_some_and(|last| {
                now.checked_duration_since(last)
                    .is_some_and(|age| age < max_age)
            })
    }

    /// Only a drain hint: close new admission and finish HTTP work first, then
    /// join all tasks and inspect their final pending flags before claiming drain.
    pub fn settled(&self) -> bool {
        self.phase == DriverPhase::Idle && !self.pending_confirmation
    }
}

/// Owns a single source consumer. Dropping the supervisor closes admission and
/// asks the task to stop between bounded operations; it never aborts a commit.
#[must_use = "keep the terminal task supervised and join it on shutdown"]
pub struct TerminalTask<T: TerminalTransport + 'static> {
    runtime: Arc<DurableRuntime>,
    stop: tokio::sync::watch::Sender<bool>,
    progress: tokio::sync::watch::Receiver<DriverProgress>,
    task: Option<tokio::task::JoinHandle<DriverExit<T>>>,
}

pub type DriverExit<T> = (Result<StopReason, PumpError>, TerminalDriver<T>);

/// Read-only, bounded view. Cloning this never creates another consumer.
#[derive(Clone)]
pub struct TerminalMonitor {
    runtime: Arc<DurableRuntime>,
    progress: tokio::sync::watch::Receiver<DriverProgress>,
    stop: tokio::sync::watch::Receiver<bool>,
}
impl TerminalMonitor {
    pub fn progress(&self) -> DriverProgress {
        let mut value = self.progress.borrow().clone();
        if self.runtime.owner().is_failed() || self.progress.has_changed().is_err() {
            value.phase = DriverPhase::Failed;
        } else if *self.stop.borrow() {
            value.phase = DriverPhase::Stopped;
        }
        value
    }
}

impl<T: TerminalTransport + 'static> TerminalTask<T> {
    /// The caller verifies one task per source and all source bindings before
    /// admitting HTTP work. This is not an engine or process supervisor.
    pub fn spawn(mut driver: TerminalDriver<T>, interval: Duration) -> Result<Self, &'static str> {
        if !(Duration::from_millis(1)..=Duration::from_secs(60)).contains(&interval) {
            driver.runtime.owner().fail_closed();
            return Err("terminal_driver_interval_invalid");
        }
        let runtime = driver.runtime.clone();
        let (stop, receiver) = tokio::sync::watch::channel(false);
        let (sender, progress) =
            tokio::sync::watch::channel(driver.progress(DriverPhase::Starting, None));
        let task = tokio::spawn(async move {
            let result = driver.run_inner(receiver, interval, Some(sender)).await;
            (result, driver)
        });
        Ok(Self {
            runtime,
            stop,
            progress,
            task: Some(task),
        })
    }

    pub fn progress(&self) -> DriverProgress {
        let mut progress = self.progress.borrow().clone();
        if self.runtime.owner().is_failed()
            || self.task.as_ref().is_none_or(|task| task.is_finished())
        {
            progress.phase = DriverPhase::Failed;
        } else if *self.stop.borrow() {
            progress.phase = DriverPhase::Stopped;
        }
        progress
    }

    pub fn monitor(&self) -> TerminalMonitor {
        TerminalMonitor {
            runtime: self.runtime.clone(),
            progress: self.progress.clone(),
            stop: self.stop.subscribe(),
        }
    }

    pub fn request_stop(&self) {
        self.stop.send_replace(true);
    }

    /// Await this method, not a timeout around a mutating step. Cancelling this
    /// join still leaves the task's stop flag set and admission closed.
    pub async fn join(
        mut self,
    ) -> Result<(Result<StopReason, PumpError>, TerminalDriver<T>), &'static str> {
        self.request_stop();
        let result = self.task.as_mut().expect("owned terminal task").await;
        self.task.take();
        self.runtime.owner().fail_closed();
        result.map_err(|_| "terminal_driver_task_failed")
    }
}

impl<T: TerminalTransport + 'static> Drop for TerminalTask<T> {
    fn drop(&mut self) {
        if self.task.is_some() {
            self.runtime.owner().fail_closed();
            self.request_stop();
        }
    }
}

// Losing a reconciliation task must never leave HTTP admission enabled, even
// if its caller aborts the future or drops its supervision channel by mistake.
struct SupervisionFence(Arc<DurableRuntime>);
impl Drop for SupervisionFence {
    fn drop(&mut self) {
        self.0.owner().fail_closed();
    }
}

pub struct TerminalDriver<T> {
    runtime: Arc<DurableRuntime>,
    transport: T,
    cursor: TerminalCursor,
    ownership: Ownership,
    pending: Pending,
    failed: bool,
}

impl<T: TerminalTransport> TerminalDriver<T> {
    pub fn ownership(&self) -> Ownership {
        self.ownership
    }

    pub fn uses_runtime(&self, runtime: &Arc<DurableRuntime>) -> bool {
        Arc::ptr_eq(&self.runtime, runtime)
    }
    /// Construct only after source verification. A fresh source must already
    /// have its zero cursor persisted. Ownership handoff must precede new
    /// settlement; replaying an already committed ACK does not resume admission.
    pub fn new(
        runtime: Arc<DurableRuntime>,
        transport: T,
        cursor: TerminalCursor,
        ownership: Ownership,
    ) -> Result<Self, &'static str> {
        if !cursor.valid() || runtime.terminal_cursor(&cursor.source_id)?.as_ref() != Some(&cursor)
        {
            return Err("terminal_driver_binding_conflict");
        }
        Ok(Self {
            runtime,
            transport,
            cursor,
            ownership,
            // Reissue ACK/retire at the durable cursor on startup. There may
            // be no active debt after a crash between commit and remote ACK.
            pending: Pending::Acknowledge,
            failed: false,
        })
    }

    pub fn cursor(&self) -> &TerminalCursor {
        &self.cursor
    }

    /// Read-only qualification diagnostics; never expose a second mutable consumer.
    pub fn transport(&self) -> &T {
        &self.transport
    }

    pub fn pending_remote_confirmation(&self) -> bool {
        self.pending != Pending::None
    }

    fn progress(&self, phase: DriverPhase, last_success: Option<Instant>) -> DriverProgress {
        DriverProgress {
            phase,
            through: self.cursor.through,
            pending_confirmation: self.pending_remote_confirmation(),
            last_success,
        }
    }

    fn fatal(&mut self, reason: &'static str) -> PumpError {
        self.failed = true;
        self.runtime.owner().fail_closed();
        PumpError::Fatal(reason)
    }

    fn rpc_error(&mut self, error: RpcFault) -> PumpError {
        match error {
            RpcFault::Unavailable => PumpError::Retry,
            RpcFault::InvalidSource => self.fatal("terminal_driver_source_invalid"),
        }
    }

    async fn confirm(&mut self) -> Result<(), PumpError> {
        if self.pending == Pending::Acknowledge {
            self.transport
                .acknowledge(&self.cursor)
                .await
                .map_err(|error| self.rpc_error(error))?;
            self.pending = match self.ownership {
                Ownership::ExclusiveRouterV1 => Pending::Retire,
                Ownership::ObserveOnly => Pending::None,
            };
        }
        if self.pending == Pending::Retire {
            self.transport
                .retire(&self.cursor)
                .await
                .map_err(|error| self.rpc_error(error))?;
            self.pending = Pending::None;
        }
        Ok(())
    }

    /// At most one bounded batch per call. Do not race another driver on this
    /// source or concurrently call the old finite-probe reconciler.
    pub async fn step(&mut self) -> Result<Step, PumpError> {
        if self.failed || self.runtime.owner().is_failed() {
            return Err(self.fatal("terminal_driver_failed"));
        }
        // Complete a lost remote confirmation even when active_len() == 0.
        self.confirm().await?;
        let batch = self
            .transport
            .poll(&self.cursor)
            .await
            .map_err(|error| self.rpc_error(error))?;
        if batch.after != self.cursor || batch.records.len() > 64 {
            return Err(self.fatal("terminal_driver_batch_invalid"));
        }
        if batch.records.is_empty() {
            return Ok(Step::Idle);
        }
        let count = batch.records.len();
        let through = match self
            .runtime
            .apply_terminal_batch(self.cursor.clone(), &batch.records)
            .await
        {
            Ok(through) => through,
            // A full bounded commit queue has not mutated anything. Source
            // replay is safe; ambiguous commits must instead fail closed.
            Err("durable_queue_full") => return Err(PumpError::Retry),
            Err(_) => return Err(self.fatal("terminal_driver_commit_failed")),
        };
        self.cursor.through = through;
        self.transport.committed();
        self.pending = Pending::Acknowledge;
        self.confirm().await?;
        Ok(Step::Committed(count))
    }

    /// Independent of HTTP completion and debt count. No task/queue per event,
    /// no burst catch-up, no retained record history. Stop is checked between
    /// bounded operations: never cancel an in-progress durable mutation.
    /// Host still owns admission drain, HTTP count, signal budget and exit code.
    pub async fn run(
        &mut self,
        stop: tokio::sync::watch::Receiver<bool>,
        interval: Duration,
    ) -> Result<StopReason, PumpError> {
        self.run_inner(stop, interval, None).await
    }

    async fn run_inner(
        &mut self,
        mut stop: tokio::sync::watch::Receiver<bool>,
        interval: Duration,
        progress: Option<tokio::sync::watch::Sender<DriverProgress>>,
    ) -> Result<StopReason, PumpError> {
        let _supervision = SupervisionFence(self.runtime.clone());
        if interval < Duration::from_millis(1) || interval > Duration::from_secs(60) {
            return Err(self.fatal("terminal_driver_interval_invalid"));
        }
        let mut last_success = None;
        let result = loop {
            if *stop.borrow() {
                break Ok(StopReason::Requested);
            }
            if let Some(sender) = &progress {
                sender.send_replace(self.progress(DriverPhase::Working, last_success));
            }
            let phase = match self.step().await {
                Ok(_) => {
                    last_success = Some(Instant::now());
                    DriverPhase::Idle
                }
                Err(PumpError::Retry) => {
                    // A retry invalidates the last readiness proof. Starting
                    // the next RPC must not reopen readiness before it succeeds.
                    last_success = None;
                    DriverPhase::Retrying
                }
                Err(error) => break Err(error),
            };
            if let Some(sender) = &progress {
                sender.send_replace(self.progress(phase, last_success));
            }
            // Observe a stop that arrived during an operation before sleeping.
            if *stop.borrow() {
                break Ok(StopReason::Requested);
            }
            tokio::select! {
                changed = stop.changed() => {
                    if changed.is_err() {
                        break Err(self.fatal("terminal_driver_supervisor_gone"));
                    }
                }
                _ = tokio::time::sleep(interval) => {}
            }
        };
        if let Some(sender) = &progress {
            sender.send_replace(self.progress(
                if result.is_ok() {
                    DriverPhase::Stopped
                } else {
                    DriverPhase::Failed
                },
                last_success,
            ));
        }
        result
    }
}
