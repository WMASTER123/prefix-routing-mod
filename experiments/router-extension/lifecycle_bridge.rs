// SPDX-License-Identifier: Apache-2.0
//! Bounded CPU transcript of real Lease callbacks. This is not a durable event
//! delivery mechanism, backend confirmation source or production ledger.
use super::prefix_extension::{CloseEvidence, Lease};
use serde::Serialize;
use std::{
    sync::{Arc, Mutex},
    time::Instant,
};

#[derive(Debug)]
pub struct RouterClock {
    id: String,
    origin: Instant,
}

impl RouterClock {
    pub fn new(id: String) -> Result<Self, &'static str> {
        identifier(&id)?;
        Ok(Self {
            id,
            origin: Instant::now(),
        })
    }
    pub fn now_ns(&self) -> u64 {
        // No backend timestamp is compared with this process-local clock.
        self.origin.elapsed().as_nanos().min(i64::MAX as u128) as u64
    }
}

fn identifier(value: &str) -> Result<(), &'static str> {
    // This prototype supports the ASCII subset of component-v0.1 IDs.
    if value.is_empty() || value.len() > 128 || !value.bytes().all(|b| b.is_ascii_graphic()) {
        return Err("invalid_lifecycle_identifier");
    }
    Ok(())
}

#[derive(Debug, Clone, Serialize)]
pub struct WorkerRef {
    worker_id: String,
    instance_epoch: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct LifecycleEvent {
    pub event_id: String,
    request_id: String,
    pub attempt_id: String,
    worker: WorkerRef,
    pub sequence: u64,
    kind: &'static str,
    confirmation: &'static str,
    pub clock_id: String,
    at_ns: u64,
    error_class: Option<&'static str>,
}

#[derive(Debug)]
pub struct LifecycleTrace {
    request_id: String,
    attempt_id: String,
    worker: WorkerRef,
    clock: Arc<RouterClock>,
    events: Vec<LifecycleEvent>,
    started: bool,
    closed: bool,
    fault: bool,
}

impl LifecycleTrace {
    pub fn new(
        request_id: String,
        attempt_id: String,
        worker_id: String,
        instance_epoch: String,
        clock: Arc<RouterClock>,
    ) -> Result<Self, &'static str> {
        for value in [&request_id, &attempt_id, &worker_id, &instance_epoch] {
            identifier(value)?;
        }
        if attempt_id.len() > 120 {
            return Err("event_identifier_budget");
        }
        Ok(Self {
            request_id,
            attempt_id,
            worker: WorkerRef {
                worker_id,
                instance_epoch,
            },
            clock,
            events: Vec::with_capacity(2),
            started: false,
            closed: false,
            fault: false,
        })
    }

    fn emit(
        &mut self,
        kind: &'static str,
        confirmation: &'static str,
        error: Option<&'static str>,
    ) {
        if self.events.len() == 2 {
            self.fault = true;
            return;
        }
        let sequence = self.events.len() as u64 + 1;
        self.events.push(LifecycleEvent {
            event_id: format!("{}:{sequence}", self.attempt_id),
            request_id: self.request_id.clone(),
            attempt_id: self.attempt_id.clone(),
            worker: self.worker.clone(),
            sequence,
            kind,
            confirmation,
            clock_id: self.clock.id.clone(),
            at_ns: self.clock.now_ns(),
            error_class: error,
        });
    }

    fn sent(&mut self) {
        if self.started || self.closed {
            self.fault = true;
            return;
        }
        self.started = true;
        self.emit("send_started", "none", None);
    }

    fn closed(&mut self, sent: bool, evidence: CloseEvidence) {
        if self.closed {
            return; // Duplicate transport close must not consume a sequence.
        }
        self.closed = true;
        if sent != self.started || (!sent && evidence != CloseEvidence::NotSent) {
            self.fault = true;
            return;
        }
        if !sent {
            self.emit("not_sent", "not_accepted", None);
        } else {
            let reason = match evidence {
                CloseEvidence::TransportEof => "transport_eof",
                CloseEvidence::TransportError => "transport_error",
                CloseEvidence::TransportDropped => "transport_dropped",
                CloseEvidence::NotSent => {
                    self.fault = true;
                    return;
                }
            };
            self.emit("outcome_unknown", "none", Some(reason));
        }
    }

    pub fn snapshot(&self) -> Result<Vec<LifecycleEvent>, &'static str> {
        if self.fault || !self.closed {
            Err("incomplete_or_invalid_lifecycle_trace")
        } else {
            Ok(self.events.clone())
        }
    }

    /// Copy at most two immutable observations while holding only the trace
    /// lock. The delivery controller performs I/O after releasing this lock.
    pub fn pending(&self, acknowledged: usize) -> Result<Vec<LifecycleEvent>, &'static str> {
        if self.fault || acknowledged > self.events.len() {
            return Err("invalid_lifecycle_cursor");
        }
        Ok(self.events[acknowledged..].to_vec())
    }

    pub fn identity(&self) -> &str {
        &self.attempt_id
    }

    pub fn delivered(&self, acknowledged: usize) -> bool {
        !self.fault && self.closed && acknowledged == self.events.len()
    }
}

#[derive(Debug)]
pub struct TracedLease {
    pub inner: Box<dyn Lease>,
    pub trace: Arc<Mutex<LifecycleTrace>>,
}

impl Lease for TracedLease {
    fn request_id_header(&self) -> Option<&str> {
        self.inner.request_id_header()
    }
    fn before_send(&self) -> Result<(), &'static str> {
        self.inner.before_send()
    }
    fn sent(&self) {
        self.trace.lock().unwrap().sent();
        self.inner.sent();
    }
    fn headers(&self, status: u16) {
        // HTTP status is transport evidence; it does not prove acceptance/end.
        self.inner.headers(status);
    }
    fn closed(&self, sent: bool, evidence: CloseEvidence) {
        self.trace.lock().unwrap().closed(sent, evidence);
        self.inner.closed(sent, evidence);
    }
}
