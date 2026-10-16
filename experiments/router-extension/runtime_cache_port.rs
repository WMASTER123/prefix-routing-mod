// SPDX-License-Identifier: Apache-2.0
//! In-process M2 adapter over the reviewed original Router KV index.
//!
//! The adapter owns partition identity, recovery/control fences and the one
//! bounded `Pool`. It never performs network or device work on `lookup`.
use crate::{
    directory::{Batch, Binding, Block, Fault, Limits, Ticket},
    pool::Pool,
    runtime_owner::CachePort,
};
use std::{
    collections::BTreeMap,
    fmt,
    sync::{Arc, Mutex},
    time::Instant,
};

pub trait ClockPort: fmt::Debug + Send + Sync {
    fn now(&self) -> u64;
}

#[derive(Debug)]
pub struct MonotonicClock(Instant);

impl Default for MonotonicClock {
    fn default() -> Self {
        Self(Instant::now())
    }
}

impl ClockPort for MonotonicClock {
    fn now(&self) -> u64 {
        self.0.elapsed().as_nanos().min(u64::MAX as u128) as u64
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PartitionSpec {
    pub binding: Binding,
    /// Engine process/start identity used by M3. It is deliberately distinct
    /// from the indexer's numeric incarnation until the HUST mapping is frozen.
    pub instance_epoch: Arc<str>,
}

impl PartitionSpec {
    fn valid(&self) -> bool {
        !self.instance_epoch.is_empty()
            && self.instance_epoch.len() <= 128
            && self
                .instance_epoch
                .bytes()
                .all(|byte| byte.is_ascii_graphic())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CacheFault {
    Index(Fault),
    UnknownPartition,
    StalePartition,
    InvalidPartition,
    Unavailable,
}

impl From<Fault> for CacheFault {
    fn from(value: Fault) -> Self {
        Self::Index(value)
    }
}

#[derive(Debug, Clone)]
pub struct RecoveryToken {
    spec: PartitionSpec,
    ticket: Ticket,
    started: u64,
}

#[derive(Debug, Clone)]
pub struct ControlToken {
    spec: PartitionSpec,
    ticket: Ticket,
    started: u64,
}

#[derive(Debug)]
struct Session {
    spec: PartitionSpec,
    ticket: Ticket,
}

struct State {
    pool: Pool,
    sessions: BTreeMap<Arc<str>, Session>,
}

pub struct InProcessCache {
    state: Mutex<State>,
    clock: Arc<dyn ClockPort>,
}

impl fmt::Debug for InProcessCache {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("InProcessCache").finish_non_exhaustive()
    }
}

impl InProcessCache {
    pub fn new(
        max_partitions: usize,
        max_bytes: usize,
        clock: Arc<dyn ClockPort>,
    ) -> Result<Self, CacheFault> {
        Ok(Self {
            state: Mutex::new(State {
                pool: Pool::new(max_partitions, max_bytes)?,
                sessions: BTreeMap::new(),
            }),
            clock,
        })
    }

    fn state(&self) -> Result<std::sync::MutexGuard<'_, State>, CacheFault> {
        self.state.lock().map_err(|_| CacheFault::Unavailable)
    }

    fn exact<'a>(state: &'a State, spec: &PartitionSpec) -> Result<&'a Session, CacheFault> {
        let session = state
            .sessions
            .get(&spec.binding.worker)
            .ok_or(CacheFault::UnknownPartition)?;
        if &session.spec != spec {
            return Err(CacheFault::StalePartition);
        }
        Ok(session)
    }

    pub fn register(
        &self,
        spec: PartitionSpec,
        limits: Limits,
        lease: u64,
    ) -> Result<RecoveryToken, CacheFault> {
        if !spec.valid() {
            return Err(CacheFault::InvalidPartition);
        }
        let mut state = self.state()?;
        state.pool.register(spec.binding.clone(), limits, lease)?;
        let ticket = match state.pool.begin_recovery(&spec.binding) {
            Ok(ticket) => ticket,
            Err(error) => {
                let _ = state.pool.unregister(&spec.binding);
                return Err(error.into());
            }
        };
        state.sessions.insert(
            spec.binding.worker.clone(),
            Session {
                spec: spec.clone(),
                ticket: ticket.clone(),
            },
        );
        Ok(RecoveryToken {
            spec,
            ticket,
            started: self.clock.now(),
        })
    }

    pub fn replace(
        &self,
        previous: &PartitionSpec,
        current: PartitionSpec,
        limits: Limits,
        lease: u64,
    ) -> Result<RecoveryToken, CacheFault> {
        if !current.valid() || previous.binding.worker != current.binding.worker {
            return Err(CacheFault::InvalidPartition);
        }
        let mut state = self.state()?;
        Self::exact(&state, previous)?;
        state
            .pool
            .replace(&previous.binding, current.binding.clone(), limits, lease)?;
        let ticket = state.pool.begin_recovery(&current.binding)?;
        state.sessions.insert(
            current.binding.worker.clone(),
            Session {
                spec: current.clone(),
                ticket: ticket.clone(),
            },
        );
        Ok(RecoveryToken {
            spec: current,
            ticket,
            started: self.clock.now(),
        })
    }

    pub fn unregister(&self, spec: &PartitionSpec) -> Result<(), CacheFault> {
        let mut state = self.state()?;
        Self::exact(&state, spec)?;
        state.pool.unregister(&spec.binding)?;
        state.sessions.remove(&spec.binding.worker);
        Ok(())
    }

    pub fn begin_recovery(&self, spec: &PartitionSpec) -> Result<RecoveryToken, CacheFault> {
        let mut state = self.state()?;
        Self::exact(&state, spec)?;
        let ticket = state.pool.begin_recovery(&spec.binding)?;
        state.sessions.get_mut(&spec.binding.worker).unwrap().ticket = ticket.clone();
        Ok(RecoveryToken {
            spec: spec.clone(),
            ticket,
            started: self.clock.now(),
        })
    }

    pub fn install(
        &self,
        token: &RecoveryToken,
        snapshot: Vec<Block>,
        watermark: u64,
        through: u64,
    ) -> Result<(), CacheFault> {
        let mut state = self.state()?;
        let session = Self::exact(&state, &token.spec)?;
        if session.ticket != token.ticket {
            return Err(CacheFault::StalePartition);
        }
        state.pool.install(
            &token.spec.binding,
            &token.ticket,
            snapshot,
            watermark,
            through,
        )?;
        Ok(())
    }

    pub fn confirm_recovery(&self, token: &RecoveryToken, through: u64) -> Result<(), CacheFault> {
        let mut state = self.state()?;
        let session = Self::exact(&state, &token.spec)?;
        if session.ticket != token.ticket {
            return Err(CacheFault::StalePartition);
        }
        state
            .pool
            .confirm(&token.spec.binding, &token.ticket, through, token.started)?;
        Ok(())
    }

    pub fn begin_control(&self, spec: &PartitionSpec) -> Result<ControlToken, CacheFault> {
        let state = self.state()?;
        let session = Self::exact(&state, spec)?;
        Ok(ControlToken {
            spec: spec.clone(),
            ticket: session.ticket.clone(),
            started: self.clock.now(),
        })
    }

    pub fn confirm_control(&self, token: &ControlToken, through: u64) -> Result<(), CacheFault> {
        let mut state = self.state()?;
        let session = Self::exact(&state, &token.spec)?;
        if session.ticket != token.ticket {
            return Err(CacheFault::StalePartition);
        }
        state
            .pool
            .confirm(&token.spec.binding, &token.ticket, through, token.started)?;
        Ok(())
    }

    pub fn live(&self, spec: &PartitionSpec, batch: Batch) -> Result<(), CacheFault> {
        let mut state = self.state()?;
        let session = Self::exact(&state, spec)?;
        let ticket = session.ticket.clone();
        let result = state.pool.live(&spec.binding, &ticket, batch);
        if let Err(error) = result {
            let _ = state.pool.disconnect(&spec.binding);
            return Err(error.into());
        }
        Ok(())
    }

    pub fn disconnect(&self, spec: &PartitionSpec) -> Result<(), CacheFault> {
        let mut state = self.state()?;
        Self::exact(&state, spec)?;
        let ticket = state.pool.begin_recovery(&spec.binding)?;
        state.sessions.get_mut(&spec.binding.worker).unwrap().ticket = ticket;
        Ok(())
    }

    pub fn reserved_bytes(&self) -> Result<usize, CacheFault> {
        Ok(self.state()?.pool.reserved_bytes())
    }
}

impl CachePort for InProcessCache {
    fn lookup(
        &self,
        worker_url: &str,
        instance_epoch: &str,
        identity: &str,
        block_hashes: &[String],
    ) -> Option<u32> {
        let state = self.state.lock().ok()?;
        let session = state.sessions.get(worker_url)?;
        if session.spec.instance_epoch.as_ref() != instance_epoch
            || session.spec.binding.identity.as_ref() != identity
        {
            return None;
        }
        let hashes: Vec<Arc<str>> = block_hashes
            .iter()
            .map(|hash| Arc::<str>::from(hash.as_str()))
            .collect();
        state
            .pool
            .lookup(&session.spec.binding, &hashes, self.clock.now())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::directory::Op;
    use std::sync::atomic::{AtomicU64, Ordering};

    const BLOCK: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    const IDENTITY: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    #[derive(Debug, Default)]
    struct ManualClock(AtomicU64);

    impl ManualClock {
        fn set(&self, now: u64) {
            self.0.store(now, Ordering::Release);
        }
    }

    impl ClockPort for ManualClock {
        fn now(&self) -> u64 {
            self.0.load(Ordering::Acquire)
        }
    }

    fn limits() -> Limits {
        Limits {
            records: 64,
            operations: 64,
            queued_batches: 8,
            queue_bytes: 4096,
            total_bytes: 16384,
            query_blocks: 64,
            parent_depth: 64,
        }
    }

    fn spec(instance: u64, epoch: &str) -> PartitionSpec {
        PartitionSpec {
            binding: Binding {
                worker: Arc::from("http://worker-a"),
                instance,
                publisher: Arc::from(format!("publisher-{instance}")),
                identity: Arc::from(IDENTITY),
                block_size: 128,
            },
            instance_epoch: Arc::from(epoch),
        }
    }

    fn lookup(cache: &InProcessCache, epoch: &str) -> Option<u32> {
        CachePort::lookup(cache, "http://worker-a", epoch, IDENTITY, &[BLOCK.into()])
    }

    #[test]
    fn recovery_live_expiry_and_old_control_fail_closed() {
        let clock = Arc::new(ManualClock::default());
        let cache = InProcessCache::new(1, 16384, clock.clone()).unwrap();
        let spec = spec(1, "epoch-a");
        let recovery = cache.register(spec.clone(), limits(), 10).unwrap();
        cache.install(&recovery, vec![], 0, 0).unwrap();
        cache.confirm_recovery(&recovery, 0).unwrap();
        assert_eq!(lookup(&cache, "epoch-a"), Some(0));

        cache
            .live(
                &spec,
                Batch {
                    sequence: 0,
                    ops: vec![Op::Store(Block {
                        id: Arc::from(BLOCK),
                        parent: None,
                        local: Arc::from(BLOCK),
                        resident: true,
                    })],
                },
            )
            .unwrap();
        clock.set(2);
        let older = cache.begin_control(&spec).unwrap();
        clock.set(3);
        let current = cache.begin_control(&spec).unwrap();
        cache.confirm_control(&current, 1).unwrap();
        assert_eq!(lookup(&cache, "epoch-a"), Some(1));
        assert_eq!(
            cache.confirm_control(&older, 1),
            Err(CacheFault::Index(Fault::Gap))
        );
        assert_eq!(lookup(&cache, "epoch-a"), None);

        let recovery = cache.begin_recovery(&spec).unwrap();
        cache.install(&recovery, vec![], 1, 1).unwrap();
        cache.confirm_recovery(&recovery, 1).unwrap();
        clock.set(13);
        assert_eq!(lookup(&cache, "epoch-a"), None);
    }

    #[test]
    fn exact_replacement_and_unregister_preserve_identity_and_quota() {
        let clock = Arc::new(ManualClock::default());
        let cache = InProcessCache::new(1, 16384, clock).unwrap();
        let old = spec(1, "epoch-a");
        let recovery = cache.register(old.clone(), limits(), 10).unwrap();
        cache.install(&recovery, vec![], 0, 0).unwrap();
        cache.confirm_recovery(&recovery, 0).unwrap();

        let current = spec(2, "epoch-b");
        let recovery = cache.replace(&old, current.clone(), limits(), 10).unwrap();
        assert_eq!(lookup(&cache, "epoch-a"), None);
        assert_eq!(lookup(&cache, "epoch-b"), None);
        cache.install(&recovery, vec![], 0, 0).unwrap();
        cache.confirm_recovery(&recovery, 0).unwrap();
        assert_eq!(lookup(&cache, "epoch-b"), Some(0));
        assert_eq!(cache.unregister(&old), Err(CacheFault::StalePartition));
        assert_eq!(cache.reserved_bytes().unwrap(), 16384);
        cache.unregister(&current).unwrap();
        assert_eq!(cache.reserved_bytes().unwrap(), 0);
    }
}
