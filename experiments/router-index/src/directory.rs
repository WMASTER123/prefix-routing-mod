// SPDX-License-Identifier: Apache-2.0
//! CPU feasibility adapter over the unchanged upstream index. One partition,
//! normalized verified hashes only; not a wire decoder or a production M2.
use super::kv_index::{
    indexer::{KvBlockIndexer, MatchQuery},
    types::{ResidencyOwner, StorageTier},
};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    sync::Arc,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Binding {
    pub worker: Arc<str>,
    pub instance: u64,
    pub publisher: Arc<str>,
    /// Digest of independently verified model/template/tokenizer/KV identity.
    pub identity: Arc<str>,
    pub block_size: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Block {
    pub id: Arc<str>,
    pub parent: Option<Arc<str>>,
    pub local: Arc<str>,
    pub resident: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Op {
    Store(Block),
    Remove(Arc<str>),
    Clear,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Batch {
    pub sequence: u64,
    pub ops: Vec<Op>,
}

#[derive(Debug, Clone, Copy)]
pub struct Limits {
    pub records: usize,
    pub operations: usize,
    pub queued_batches: usize,
    pub queue_bytes: usize,
    /// Logical charges, not an allocator or RSS guarantee.
    pub total_bytes: usize,
    pub query_blocks: usize,
    /// Bound upstream tree destruction depth as well as recovery work.
    pub parent_depth: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fault {
    Stale,
    Invalid,
    Gap,
    Conflict,
    MissingParent,
    Cycle,
    Budget,
    Overflow,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Ticket {
    generation: u64,
    binding: Binding,
}

type Catalog = BTreeMap<Arc<str>, Block>;
struct View {
    catalog: Catalog,
    depths: BTreeMap<Arc<str>, usize>,
    paths: BTreeMap<(Option<Arc<str>>, Arc<str>), Arc<str>>,
    bytes: usize,
    index: KvBlockIndexer,
    next: u64,
}
struct Recovery {
    ticket: Ticket,
    queue: BTreeMap<u64, Batch>,
    bytes: usize,
}

pub struct Directory {
    limits: Limits,
    generation: u64,
    binding: Option<Binding>,
    active: Option<View>,
    recovery: Option<Recovery>,
    pub rebuilds: usize,
    pub fault: Option<Fault>,
}

fn label(s: &str) -> bool {
    !s.is_empty() && s.len() <= 128 && !s.chars().any(char::is_control)
}
fn digest(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}
fn valid_block(b: &Block) -> bool {
    label(&b.id) && b.parent.as_deref().is_none_or(label) && digest(&b.local)
}
fn charge(b: &Block) -> usize {
    512 + b.id.len() + b.local.len() + b.parent.as_ref().map_or(0, |p| p.len())
}
fn catalog_bytes(c: &Catalog) -> usize {
    c.values().map(charge).sum()
}

fn batch_bytes(batch: &Batch, limits: Limits) -> Result<usize, Fault> {
    if batch.ops.is_empty() || batch.ops.len() > limits.operations {
        return Err(Fault::Budget);
    }
    let mut bytes = 64usize;
    for op in &batch.ops {
        let size = match op {
            Op::Store(b) if valid_block(b) && b.resident => charge(b),
            Op::Remove(id) if label(id) => 64 + id.len(),
            Op::Clear => 64,
            _ => return Err(Fault::Invalid),
        };
        bytes = bytes.checked_add(size).ok_or(Fault::Overflow)?;
    }
    Ok(bytes)
}

/// Iterative parent-before-child order. No recursion on attacker-shaped chains.
fn order(catalog: &Catalog) -> Result<Vec<Arc<str>>, Fault> {
    let mut children: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    let mut paths = BTreeSet::new();
    let mut ready = VecDeque::new();
    for block in catalog.values() {
        if !paths.insert((block.parent.as_deref(), block.local.as_ref())) {
            // Upstream anchors cannot safely distinguish aliases at one path.
            return Err(Fault::Conflict);
        }
        if let Some(parent) = &block.parent {
            if !catalog.contains_key(parent) {
                return Err(Fault::MissingParent);
            }
            children.entry(parent).or_default().push(&block.id);
        } else {
            ready.push_back(block.id.as_ref());
        }
    }
    let mut result = Vec::with_capacity(catalog.len());
    while let Some(id) = ready.pop_front() {
        result.push(catalog.get(id).unwrap().id.clone());
        if let Some(next) = children.get(id) {
            ready.extend(next.iter().copied());
        }
    }
    if result.len() != catalog.len() {
        return Err(Fault::Cycle);
    }
    Ok(result)
}

fn compact(catalog: &mut Catalog) -> Result<(), Fault> {
    order(catalog)?;
    let mut needed = BTreeSet::new();
    for block in catalog.values().filter(|b| b.resident) {
        let mut next = Some(block.id.clone());
        while let Some(id) = next {
            if !needed.insert(id.clone()) {
                break;
            }
            next = catalog[&id].parent.clone();
        }
    }
    catalog.retain(|id, _| needed.contains(id));
    Ok(())
}

fn apply(catalog: &mut Catalog, ops: &[Op]) -> Result<(), Fault> {
    for op in ops {
        match op {
            Op::Store(b) => {
                if let Some(previous) = catalog.get(&b.id) {
                    if previous.parent != b.parent || previous.local != b.local {
                        return Err(Fault::Conflict);
                    }
                }
                // A store batch is normalized in parent-before-child order.
                if b.parent.as_ref().is_some_and(|p| !catalog.contains_key(p)) {
                    return Err(Fault::MissingParent);
                }
                catalog.insert(b.id.clone(), b.clone());
            }
            Op::Remove(id) => {
                // Removing already absent residency is idempotent.
                if let Some(b) = catalog.get_mut(id) {
                    b.resident = false;
                }
            }
            Op::Clear => catalog.clear(),
        }
    }
    order(catalog)?;
    Ok(())
}

impl Directory {
    pub fn new(limits: Limits) -> Result<Self, Fault> {
        if !(1..=65_536).contains(&limits.records)
            || !(1..=4096).contains(&limits.operations)
            || !(1..=4096).contains(&limits.queued_batches)
            || !(1..=65_536).contains(&limits.query_blocks)
            || !(1..=1024).contains(&limits.parent_depth)
            || limits.total_bytes > 1_073_741_824
            || limits.total_bytes < 1024
            || limits.queue_bytes == 0
            || limits.queue_bytes > limits.total_bytes
        {
            return Err(Fault::Invalid);
        }
        Ok(Self {
            limits,
            generation: 0,
            binding: None,
            active: None,
            recovery: None,
            rebuilds: 0,
            fault: None,
        })
    }

    fn fail<T>(&mut self, fault: Fault) -> Result<T, Fault> {
        self.active = None;
        self.recovery = None;
        self.fault = Some(fault);
        Err(fault)
    }

    fn budget(&self, catalog: &Catalog, extra: usize) -> Result<(), Fault> {
        if catalog.len() > self.limits.records
            || catalog_bytes(catalog)
                .checked_add(extra)
                .ok_or(Fault::Overflow)?
                > self.limits.total_bytes
        {
            return Err(Fault::Budget);
        }
        Ok(())
    }

    pub fn begin(&mut self, binding: Binding) -> Result<Ticket, Fault> {
        if !label(&binding.worker)
            || !label(&binding.publisher)
            || !digest(&binding.identity)
            || binding.block_size == 0
            || binding.block_size > 65_536
        {
            return self.fail(Fault::Invalid);
        }
        self.generation = match self.generation.checked_add(1) {
            Some(g) => g,
            None => return self.fail(Fault::Overflow),
        };
        let ticket = Ticket {
            generation: self.generation,
            binding: binding.clone(),
        };
        self.active = None;
        self.binding = Some(binding);
        self.recovery = Some(Recovery {
            ticket: ticket.clone(),
            queue: BTreeMap::new(),
            bytes: 0,
        });
        self.fault = None;
        Ok(ticket)
    }

    fn current_ticket(&self, ticket: &Ticket) -> bool {
        ticket.generation == self.generation && self.binding.as_ref() == Some(&ticket.binding)
    }

    /// Buffer verified frames during a snapshot handshake. Duplicate frames
    /// compare their complete normalized payload, not just sequence numbers.
    pub fn queue(&mut self, ticket: &Ticket, batch: Batch) -> Result<(), Fault> {
        if !self.current_ticket(ticket) {
            return Err(Fault::Stale);
        }
        let bytes = match batch_bytes(&batch, self.limits) {
            Ok(b) => b,
            Err(e) => return self.fail(e),
        };
        let Some(recovery) = self.recovery.as_mut() else {
            return Err(Fault::Invalid);
        };
        if recovery.ticket != *ticket {
            return Err(Fault::Stale);
        }
        if let Some(previous) = recovery.queue.get(&batch.sequence) {
            return if previous == &batch {
                Ok(())
            } else {
                self.fail(Fault::Conflict)
            };
        }
        let total = recovery.bytes.checked_add(bytes).ok_or(Fault::Overflow)?;
        if recovery.queue.len() >= self.limits.queued_batches || total > self.limits.queue_bytes {
            return self.fail(Fault::Budget);
        }
        recovery.queue.insert(batch.sequence, batch);
        recovery.bytes = total;
        Ok(())
    }

    fn build(&self, catalog: Catalog, next: u64) -> Result<View, Fault> {
        let sorted = order(&catalog)?;
        let mut depths = BTreeMap::new();
        for id in &sorted {
            let depth = catalog[id].parent.as_ref().map_or(1, |p| depths[p] + 1);
            if depth > self.limits.parent_depth {
                return Err(Fault::Budget);
            }
            depths.insert(id.clone(), depth);
        }
        let index = KvBlockIndexer::new();
        let binding = self.binding.as_ref().ok_or(Fault::Invalid)?;
        let owner = ResidencyOwner::Worker {
            source: binding.worker.clone(),
            dp_rank: 0,
            incarnation: binding.instance,
        };
        for id in sorted {
            let b = &catalog[&id];
            index.store(
                0,
                owner.clone(),
                StorageTier::Device,
                b.parent.clone(),
                &[(b.id.clone(), b.local.clone())],
            );
        }
        // Ancestors are topology, not residency. Remove their temporary claim
        // only after the complete topology has been installed, before readers.
        let absent: Vec<_> = catalog
            .values()
            .filter(|b| !b.resident)
            .map(|b| b.id.clone())
            .collect();
        index.remove(0, &owner, StorageTier::Device, &absent);
        Ok(View {
            depths,
            bytes: catalog_bytes(&catalog),
            paths: catalog
                .values()
                .map(|b| ((b.parent.clone(), b.local.clone()), b.id.clone()))
                .collect(),
            catalog,
            index,
            next,
        })
    }

    /// `watermark` is the snapshot's exclusive next sequence at its common
    /// publisher boundary. `through` is a confirmed exclusive replay boundary.
    /// This adapter trusts only the opt-in protocol's independently validated cut.
    pub fn finish(
        &mut self,
        ticket: &Ticket,
        snapshot: Vec<Block>,
        watermark: u64,
        through: u64,
    ) -> Result<(), Fault> {
        if !self.current_ticket(ticket) {
            return Err(Fault::Stale);
        }
        let Some(recovery) = self.recovery.take() else {
            return Err(Fault::Invalid);
        };
        let result = (|| {
            if watermark > through || snapshot.len() > self.limits.records {
                return Err(Fault::Invalid);
            }
            let input_bytes: usize = snapshot.iter().map(charge).sum();
            if input_bytes
                .checked_mul(2)
                .and_then(|n| n.checked_add(recovery.bytes))
                .ok_or(Fault::Overflow)?
                > self.limits.total_bytes
            {
                return Err(Fault::Budget);
            }
            let mut catalog = Catalog::new();
            for block in snapshot {
                if !valid_block(&block) {
                    return Err(Fault::Invalid);
                }
                if catalog.insert(block.id.clone(), block).is_some() {
                    return Err(Fault::Conflict);
                }
            }
            order(&catalog)?;
            let mut next = watermark;
            // Pre-watermark queued frames are represented by the snapshot.
            // Frames arriving beyond `through` also require continuous coverage.
            for (seq, batch) in recovery.queue.range(watermark..) {
                if *seq != next {
                    return Err(Fault::Gap);
                }
                self.budget(&catalog, recovery.bytes + batch_bytes(batch, self.limits)?)?;
                apply(&mut catalog, &batch.ops)?;
                self.budget(&catalog, recovery.bytes)?;
                next = next.checked_add(1).ok_or(Fault::Overflow)?;
            }
            if next < through {
                return Err(Fault::Gap);
            }
            compact(&mut catalog)?;
            self.budget(&catalog, catalog_bytes(&catalog) + recovery.bytes)?;
            self.build(catalog, next)
        })();
        match result {
            Ok(view) => {
                self.active = Some(view);
                self.fault = None;
                Ok(())
            }
            Err(e) => self.fail(e),
        }
    }

    /// Owner calls under a write lock. Partial batches are never queryable;
    /// an invalid operation withdraws the entire view. No whole-catalog clone
    /// or topology walk on each event; full scans happen on recovery/rebuild.
    pub fn event(&mut self, ticket: &Ticket, batch: Batch) -> Result<(), Fault> {
        if !self.current_ticket(ticket) {
            return Err(Fault::Stale);
        }
        let Some(view) = self.active.as_ref() else {
            return Err(Fault::Invalid);
        };
        // Old deliveries do not mutate installed state. Full conflicting replay
        // detection belongs to the bounded wire recovery/dedup window.
        if batch.sequence < view.next {
            return Err(Fault::Stale);
        }
        if batch.sequence != view.next {
            return self.fail(Fault::Gap);
        }
        let bytes = match batch_bytes(&batch, self.limits) {
            Ok(b) => b,
            Err(e) => return self.fail(e),
        };
        let mut view = self.active.take().unwrap();
        let binding = self.binding.as_ref().unwrap();
        let owner = ResidencyOwner::Worker {
            source: binding.worker.clone(),
            dp_rank: 0,
            incarnation: binding.instance,
        };
        let result = (|| {
            let mut rebuild = false;
            let mut old_index_bytes = view.bytes;
            if view.bytes + bytes > self.limits.total_bytes {
                return Err(Fault::Budget);
            }
            for op in &batch.ops {
                match op {
                    Op::Store(b) => {
                        let path = (b.parent.clone(), b.local.clone());
                        if view.paths.get(&path).is_some_and(|id| id != &b.id) {
                            return Err(Fault::Conflict);
                        }
                        if let Some(previous) = view.catalog.get(&b.id) {
                            if previous.parent != b.parent || previous.local != b.local {
                                return Err(Fault::Conflict);
                            }
                        } else {
                            if b.parent
                                .as_ref()
                                .is_some_and(|p| !view.catalog.contains_key(p))
                            {
                                return Err(Fault::MissingParent);
                            }
                            let depth = b.parent.as_ref().map_or(1, |p| view.depths[p] + 1);
                            if depth > self.limits.parent_depth {
                                return Err(Fault::Budget);
                            }
                            view.depths.insert(b.id.clone(), depth);
                            view.bytes += charge(b);
                        }
                        if view.bytes + bytes > self.limits.total_bytes {
                            return Err(Fault::Budget);
                        }
                        view.paths.insert(path, b.id.clone());
                        view.catalog.insert(b.id.clone(), b.clone());
                        if !rebuild {
                            view.index.store(
                                0,
                                owner.clone(),
                                StorageTier::Device,
                                b.parent.clone(),
                                &[(b.id.clone(), b.local.clone())],
                            );
                            old_index_bytes = old_index_bytes.max(view.bytes);
                        }
                    }
                    Op::Remove(id) => {
                        if let Some(b) = view.catalog.get_mut(id) {
                            b.resident = false;
                        }
                        if !rebuild {
                            view.index.remove(
                                0,
                                &owner,
                                StorageTier::Device,
                                std::slice::from_ref(id),
                            );
                        }
                    }
                    Op::Clear => {
                        view.catalog.clear();
                        view.paths.clear();
                        view.depths.clear();
                        view.bytes = 0;
                        rebuild = true;
                    }
                }
            }
            view.next = view.next.checked_add(1).ok_or(Fault::Overflow)?;
            if view.catalog.len() > self.limits.records || rebuild {
                compact(&mut view.catalog)?;
                // Two bounded index structures can overlap only during rebuild;
                // metadata ownership transfers, and no reader Arc can escape.
                self.budget(&view.catalog, old_index_bytes + bytes)?;
                let catalog = std::mem::take(&mut view.catalog);
                view = self.build(catalog, view.next)?;
                self.rebuilds += 1;
            }
            Ok(())
        })();
        if let Err(e) = result {
            return self.fail(e);
        }
        self.active = Some(view);
        Ok(())
    }

    /// None = untrusted/incompatible. Some(0) = trusted observed miss.
    pub fn lookup(&self, binding: &Binding, hashes: &[Arc<str>]) -> Option<u32> {
        if self.binding.as_ref() != Some(binding)
            || hashes.len() > self.limits.query_blocks
            || hashes.iter().any(|h| !digest(h))
        {
            return None;
        }
        let view = self.active.as_ref()?;
        let matches = view.index.find_matches(&MatchQuery {
            group_idx: 0,
            local_hashes: hashes.to_vec(),
            tiers_of_interest: vec![StorageTier::Device],
        });
        Some(matches.iter().map(|m| m.matched_depth).max().unwrap_or(0))
    }

    pub fn retained_records(&self) -> usize {
        self.active.as_ref().map_or(0, |v| v.catalog.len())
    }

    pub fn next_sequence(&self) -> Option<u64> {
        self.active.as_ref().map(|v| v.next)
    }
}
