// SPDX-License-Identifier: Apache-2.0
//! Process-local control freshness. Never serialize a monotonic deadline.
use std::{
    collections::BTreeMap,
    fmt::Debug,
    sync::{Arc, Mutex},
    time::Instant,
};

pub trait Clock: Debug + Send + Sync {
    fn now_ns(&self) -> u64;
}

#[derive(Debug)]
pub struct MonotonicClock(Instant);
impl Default for MonotonicClock {
    fn default() -> Self {
        Self(Instant::now())
    }
}
impl Clock for MonotonicClock {
    fn now_ns(&self) -> u64 {
        self.0.elapsed().as_nanos().min(u64::MAX as u128) as u64
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceKey {
    pub instance: String,
    pub observer: String,
    pub ownership: String,
}

#[derive(Debug)]
struct State {
    revision: u64,
    probe: u64,
    deadline: Option<u64>,
    last_now: u64,
    draining: bool,
    quarantined: bool,
}
impl State {
    fn close(&mut self) {
        self.deadline = None;
        match self.revision.checked_add(1) {
            Some(next) => self.revision = next,
            None => self.quarantined = true,
        }
    }
    fn expire(&mut self, now: u64) -> Result<(), &'static str> {
        if now < self.last_now {
            self.close();
            self.quarantined = true;
            return Err("control_clock_regressed");
        }
        self.last_now = now;
        if self.deadline.is_some_and(|deadline| now >= deadline) {
            self.close();
        }
        Ok(())
    }
}

#[derive(Debug)]
pub struct Probe {
    nonce: Arc<()>,
    revision: u64,
    sequence: u64,
    started: u64,
}

#[derive(Debug)]
pub struct WorkerGate {
    expected: SourceKey,
    clock: Arc<dyn Clock>,
    ttl_ns: u64,
    nonce: Arc<()>,
    state: Mutex<State>,
}
impl WorkerGate {
    /// Startup binding check; does not renew freshness or grant a send fence.
    pub fn matches_source(&self, source: &SourceKey) -> bool {
        &self.expected == source
    }

    pub fn new(
        expected: SourceKey,
        clock: Arc<dyn Clock>,
        ttl_ns: u64,
    ) -> Result<Self, &'static str> {
        if ttl_ns == 0
            || ttl_ns > 300_000_000_000
            || [&expected.instance, &expected.observer, &expected.ownership]
                .iter()
                .any(|value| {
                    value.is_empty()
                        || value.len() > 128
                        || !value.bytes().all(|byte| byte.is_ascii_graphic())
                })
            || !matches!(
                expected.ownership.as_str(),
                "observe_only" | "exclusive_router_v1"
            )
        {
            return Err("invalid_control_gate");
        }
        let now = clock.now_ns();
        Ok(Self {
            expected,
            clock,
            ttl_ns,
            nonce: Arc::new(()),
            state: Mutex::new(State {
                revision: 1,
                probe: 0,
                deadline: None,
                last_now: now,
                draining: false,
                quarantined: false,
            }),
        })
    }

    /// Begin before network I/O, so a slow response cannot extend its own lease.
    pub fn begin(&self) -> Result<Probe, &'static str> {
        let mut state = self.state.lock().map_err(|_| "control_poisoned")?;
        let now = self.clock.now_ns();
        state.expire(now)?;
        if state.draining || state.quarantined {
            return Err("control_closed");
        }
        state.probe = state
            .probe
            .checked_add(1)
            .ok_or("control_probe_exhausted")?;
        Ok(Probe {
            nonce: self.nonce.clone(),
            revision: state.revision,
            sequence: state.probe,
            started: now,
        })
    }

    /// Call only after the version adapter has validated the full source reply.
    pub fn complete(&self, probe: Probe, observed: &SourceKey) -> Result<(), &'static str> {
        let mut state = self.state.lock().map_err(|_| "control_poisoned")?;
        let now = self.clock.now_ns();
        state.expire(now)?;
        if !Arc::ptr_eq(&probe.nonce, &self.nonce)
            || probe.revision != state.revision
            || probe.sequence != state.probe
            || state.draining
            || state.quarantined
        {
            return Err("control_stale_probe");
        }
        if observed != &self.expected {
            state.close();
            state.quarantined = true;
            return Err("control_identity_changed");
        }
        let deadline = probe
            .started
            .checked_add(self.ttl_ns)
            .ok_or("control_clock_exhausted")?;
        if now >= deadline {
            state.close();
            return Err("control_probe_expired");
        }
        state.deadline = Some(deadline);
        Ok(())
    }

    pub fn reject(&self, probe: Probe) -> Result<(), &'static str> {
        let mut state = self.state.lock().map_err(|_| "control_poisoned")?;
        if Arc::ptr_eq(&probe.nonce, &self.nonce)
            && probe.revision == state.revision
            && probe.sequence == state.probe
        {
            state.close();
        }
        Ok(())
    }

    /// Returns a send fence; renewing healthy observations preserves this fence.
    pub fn revision(&self, instance: &str) -> Option<u64> {
        let mut state = self.state.lock().ok()?;
        let now = self.clock.now_ns();
        state.expire(now).ok()?;
        (instance == self.expected.instance
            && state.deadline.is_some()
            && !state.draining
            && !state.quarantined)
            .then_some(state.revision)
    }

    pub fn drain(&self) -> Result<(), &'static str> {
        let mut state = self.state.lock().map_err(|_| "control_poisoned")?;
        state.draining = true;
        state.close();
        Ok(())
    }

    pub fn quarantine(&self) -> Result<(), &'static str> {
        let mut state = self.state.lock().map_err(|_| "control_poisoned")?;
        state.quarantined = true;
        state.close();
        Ok(())
    }
}

#[derive(Debug)]
pub struct ControlSet(pub BTreeMap<String, Arc<WorkerGate>>);
impl ControlSet {
    pub fn revision(&self, worker: &str, instance: &str) -> Option<u64> {
        self.0.get(worker)?.revision(instance)
    }
    pub fn drain(&self) -> Result<(), &'static str> {
        let mut failure = None;
        for gate in self.0.values() {
            if let Err(error) = gate.drain() {
                failure = Some(error);
            }
        }
        failure.map_or(Ok(()), Err)
    }
}
