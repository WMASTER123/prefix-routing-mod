// SPDX-License-Identifier: Apache-2.0
//! Single-source terminal pump; no second ledger or terminal history.
//! The host owns exactly one driver per source and must join, not abort, it.
use super::{
    durable_runtime::{DurableRuntime, TerminalRecord},
    runtime_store::TerminalCursor,
};
use std::{future::Future, pin::Pin, sync::Arc, time::Duration};

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

    pub fn pending_remote_confirmation(&self) -> bool {
        self.pending != Pending::None
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
        mut stop: tokio::sync::watch::Receiver<bool>,
        interval: Duration,
    ) -> Result<StopReason, PumpError> {
        let _supervision = SupervisionFence(self.runtime.clone());
        if interval < Duration::from_millis(1) || interval > Duration::from_secs(60) {
            return Err(self.fatal("terminal_driver_interval_invalid"));
        }
        loop {
            if *stop.borrow() {
                return Ok(StopReason::Requested);
            }
            match self.step().await {
                Ok(_) | Err(PumpError::Retry) => {}
                Err(error) => return Err(error),
            }
            // Observe a stop that arrived during an operation before sleeping.
            if *stop.borrow() {
                return Ok(StopReason::Requested);
            }
            tokio::select! {
                changed = stop.changed() => {
                    if changed.is_err() {
                        return Err(self.fatal("terminal_driver_supervisor_gone"));
                    }
                }
                _ = tokio::time::sleep(interval) => {}
            }
        }
    }
}
