// SPDX-License-Identifier: Apache-2.0
//! Fixed quota registration and control freshness over the SAME directory.
//! Quotas are logical allowances; not an allocator/RSS or production ABI.
use super::directory::{Batch, Binding, Block, Directory, Fault, Limits, Ticket};
use std::{collections::BTreeMap, sync::Arc};

struct Entry {
    directory: Directory,
    binding: Binding,
    ticket: Ticket,
    confirmed: Option<u64>,
    lease: u64,
    trusted: bool,
}

pub struct Pool {
    entries: BTreeMap<Arc<str>, Entry>,
    max_partitions: usize,
    max_bytes: usize,
    reserved: usize,
}

impl Pool {
    pub fn new(max_partitions: usize, max_bytes: usize) -> Result<Self, Fault> {
        if !(1..=32).contains(&max_partitions) || max_bytes == 0 {
            return Err(Fault::Invalid);
        }
        Ok(Self {
            entries: BTreeMap::new(),
            max_partitions,
            max_bytes,
            reserved: 0,
        })
    }

    pub fn register(&mut self, binding: Binding, limits: Limits, lease: u64) -> Result<(), Fault> {
        if lease == 0 || self.entries.contains_key(&binding.worker) {
            return Err(Fault::Invalid);
        }
        let next = self
            .reserved
            .checked_add(limits.total_bytes)
            .ok_or(Fault::Overflow)?;
        if self.entries.len() >= self.max_partitions || next > self.max_bytes {
            return Err(Fault::Budget);
        }
        let mut directory = Directory::new(limits)?;
        let ticket = directory.begin(binding.clone())?;
        self.entries.insert(
            binding.worker.clone(),
            Entry {
                directory,
                ticket,
                binding,
                confirmed: None,
                lease,
                trusted: false,
            },
        );
        self.reserved = next;
        Ok(())
    }

    fn entry(&mut self, binding: &Binding) -> Result<&mut Entry, Fault> {
        let entry = self
            .entries
            .get_mut(&binding.worker)
            .ok_or(Fault::Invalid)?;
        if &entry.binding != binding {
            return Err(Fault::Stale);
        }
        Ok(entry)
    }

    pub fn begin_recovery(&mut self, binding: &Binding) -> Result<Ticket, Fault> {
        let entry = self.entry(binding)?;
        entry.trusted = false;
        entry.confirmed = None;
        entry.ticket = entry.directory.begin(binding.clone())?;
        Ok(entry.ticket.clone())
    }

    pub fn install(
        &mut self,
        binding: &Binding,
        ticket: &Ticket,
        snapshot: Vec<Block>,
        watermark: u64,
        through: u64,
    ) -> Result<(), Fault> {
        let entry = self.entry(binding)?;
        if &entry.ticket != ticket {
            return Err(Fault::Stale);
        }
        entry.trusted = false;
        entry.confirmed = None;
        entry
            .directory
            .finish(&entry.ticket, snapshot, watermark, through)
    }

    pub fn queue(&mut self, binding: &Binding, ticket: &Ticket, batch: Batch) -> Result<(), Fault> {
        let entry = self.entry(binding)?;
        if &entry.ticket != ticket {
            return Err(Fault::Stale);
        }
        let result = entry.directory.queue(ticket, batch);
        if result.is_err() {
            entry.trusted = false;
        }
        result
    }

    pub fn live(&mut self, binding: &Binding, ticket: &Ticket, batch: Batch) -> Result<(), Fault> {
        let entry = self.entry(binding)?;
        if &entry.ticket != ticket {
            return Err(Fault::Stale);
        }
        let result = entry.directory.event(&entry.ticket, batch);
        if result.is_err() {
            entry.trusted = false;
        }
        // Data arrivals never extend control freshness.
        result
    }

    /// `started` is the Router time when this control request was sent, NOT
    /// its reply arrival or a backend clock. A late response cannot renew time.
    pub fn confirm(
        &mut self,
        binding: &Binding,
        ticket: &Ticket,
        through: u64,
        started: u64,
    ) -> Result<(), Fault> {
        let entry = self.entry(binding)?;
        if &entry.ticket != ticket {
            return Err(Fault::Stale);
        }
        if entry.directory.next_sequence() != Some(through)
            || entry.confirmed.is_some_and(|previous| started < previous)
        {
            entry.trusted = false;
            return Err(Fault::Gap);
        }
        entry.confirmed = Some(started);
        entry.trusted = true;
        Ok(())
    }

    pub fn lookup(&self, binding: &Binding, hashes: &[Arc<str>], now: u64) -> Option<u32> {
        let entry = self.entries.get(&binding.worker)?;
        let confirmed = entry.confirmed?;
        if &entry.binding != binding
            || !entry.trusted
            || now < confirmed
            || now - confirmed >= entry.lease
        {
            return None;
        }
        entry.directory.lookup(binding, hashes)
    }

    pub fn disconnect(&mut self, binding: &Binding) -> Result<(), Fault> {
        let entry = self.entry(binding)?;
        entry.trusted = false;
        entry.confirmed = None;
        // Complete recovery required; no ordinary frame can restore old trust.
        entry.ticket = entry.directory.begin(binding.clone())?;
        Ok(())
    }

    pub fn reserved_bytes(&self) -> usize {
        self.reserved
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn binding(worker: &str) -> Binding {
        Binding {
            worker: Arc::from(worker),
            instance: 1,
            publisher: Arc::from("publisher"),
            identity: Arc::from("a".repeat(64)),
            block_size: 2,
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
    fn restore(pool: &mut Pool, binding: &Binding, now: u64) -> Ticket {
        let ticket = pool.begin_recovery(binding).unwrap();
        pool.install(binding, &ticket, vec![], 0, 0).unwrap();
        pool.confirm(binding, &ticket, 0, now).unwrap();
        ticket
    }
    #[test]
    fn total_quota_rejects_third_partition() {
        let mut pool = Pool::new(3, 32768).unwrap();
        pool.register(binding("a"), limits(), 10).unwrap();
        pool.register(binding("b"), limits(), 10).unwrap();
        assert_eq!(
            pool.register(binding("c"), limits(), 10),
            Err(Fault::Budget)
        );
        assert_eq!(pool.reserved_bytes(), 32768);
    }
    #[test]
    fn expiry_is_unknown_not_miss_and_live_does_not_renew() {
        let b = binding("a");
        let mut pool = Pool::new(1, 16384).unwrap();
        pool.register(b.clone(), limits(), 10).unwrap();
        let ticket = pool.begin_recovery(&b).unwrap();
        pool.install(&b, &ticket, vec![], 0, 0).unwrap();
        assert_eq!(pool.lookup(&b, &[], 0), None);
        pool.confirm(&b, &ticket, 0, 0).unwrap();
        assert_eq!(pool.lookup(&b, &[], 9), Some(0));
        pool.live(
            &b,
            &ticket,
            Batch {
                sequence: 0,
                ops: vec![super::super::directory::Op::Clear],
            },
        )
        .unwrap();
        assert_eq!(pool.lookup(&b, &[], 10), None);
    }
    #[test]
    fn disconnect_and_gap_require_recovery() {
        let b = binding("a");
        let mut pool = Pool::new(1, 16384).unwrap();
        pool.register(b.clone(), limits(), 10).unwrap();
        let ticket = restore(&mut pool, &b, 0);
        pool.disconnect(&b).unwrap();
        assert_eq!(pool.confirm(&b, &ticket, 0, 1), Err(Fault::Stale));
        assert_eq!(pool.lookup(&b, &[], 1), None);
        let ticket = restore(&mut pool, &b, 2);
        assert_eq!(
            pool.live(
                &b,
                &ticket,
                Batch {
                    sequence: 1,
                    ops: vec![super::super::directory::Op::Clear]
                }
            ),
            Err(Fault::Gap)
        );
        assert_eq!(pool.lookup(&b, &[], 3), None);
    }
    #[test]
    fn foreign_binding_cannot_invalidate_current_view() {
        let b = binding("a");
        let mut pool = Pool::new(1, 16384).unwrap();
        pool.register(b.clone(), limits(), 10).unwrap();
        restore(&mut pool, &b, 0);
        let mut old = b.clone();
        old.instance = 0;
        assert_eq!(pool.disconnect(&old), Err(Fault::Stale));
        assert_eq!(pool.lookup(&b, &[], 1), Some(0));
    }

    #[test]
    fn late_same_publisher_restore_cannot_replace_new_view() {
        let b = binding("a");
        let mut pool = Pool::new(1, 16384).unwrap();
        pool.register(b.clone(), limits(), 10).unwrap();
        let old = pool.begin_recovery(&b).unwrap();
        let current = restore(&mut pool, &b, 2);
        assert_eq!(pool.install(&b, &old, vec![], 8, 8), Err(Fault::Stale));
        assert_eq!(pool.confirm(&b, &old, 8, 3), Err(Fault::Stale));
        assert_eq!(
            pool.live(
                &b,
                &old,
                Batch {
                    sequence: 0,
                    ops: vec![super::super::directory::Op::Clear]
                }
            ),
            Err(Fault::Stale)
        );
        assert_eq!(pool.lookup(&b, &[], 3), Some(0));
        pool.confirm(&b, &current, 0, 3).unwrap();
    }

    #[test]
    fn delayed_confirmation_does_not_extend_lease_from_receipt_time() {
        let b = binding("a");
        let mut pool = Pool::new(1, 16384).unwrap();
        pool.register(b.clone(), limits(), 10).unwrap();
        let ticket = restore(&mut pool, &b, 0);
        pool.confirm(&b, &ticket, 0, 0).unwrap(); // Received late, request began at 0.
        assert_eq!(pool.lookup(&b, &[], 100), None);
        pool.confirm(&b, &ticket, 0, 100).unwrap();
        assert_eq!(pool.lookup(&b, &[], 101), Some(0));
    }

    #[test]
    fn queue_and_snapshot_install_at_one_cut() {
        let b = binding("a");
        let mut pool = Pool::new(1, 16384).unwrap();
        pool.register(b.clone(), limits(), 10).unwrap();
        let ticket = pool.begin_recovery(&b).unwrap();
        pool.queue(
            &b,
            &ticket,
            Batch {
                sequence: 0,
                ops: vec![super::super::directory::Op::Clear],
            },
        )
        .unwrap();
        pool.install(&b, &ticket, vec![], 0, 1).unwrap();
        pool.confirm(&b, &ticket, 1, 0).unwrap();
        assert_eq!(pool.lookup(&b, &[], 1), Some(0));
    }

    #[test]
    fn unexpected_queue_after_install_withdraws_trust() {
        let b = binding("a");
        let mut pool = Pool::new(1, 16384).unwrap();
        pool.register(b.clone(), limits(), 10).unwrap();
        let ticket = restore(&mut pool, &b, 0);
        assert_eq!(
            pool.queue(
                &b,
                &ticket,
                Batch {
                    sequence: 0,
                    ops: vec![super::super::directory::Op::Clear]
                }
            ),
            Err(Fault::Invalid)
        );
        assert_eq!(pool.lookup(&b, &[], 1), None);
    }
}
