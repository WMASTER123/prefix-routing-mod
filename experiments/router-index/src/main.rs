// SPDX-License-Identifier: Apache-2.0
//! Original index churn audit. Rebuild uses a synthetic complete snapshot.
use std::{fs, sync::Arc, time::Instant};
include!(concat!(env!("OUT_DIR"), "/upstream.rs"));
mod directory;
#[cfg(test)]
mod directory_tests;
mod online;
mod pool;
mod wire;
use kv_index::{
    indexer::{KvBlockIndexer, MatchQuery},
    types::{ResidencyOwner, StorageTier},
};

fn owner() -> ResidencyOwner {
    ResidencyOwner::Worker {
        source: Arc::from("fixture-worker"),
        dp_rank: 0,
        incarnation: 1,
    }
}

fn memory_kib(field: &str) -> usize {
    fs::read_to_string("/proc/self/status")
        .unwrap()
        .lines()
        .find(|line| line.starts_with(field))
        .unwrap()
        .split_whitespace()
        .nth(1)
        .unwrap()
        .parse()
        .unwrap()
}

fn snapshot() -> KvBlockIndexer {
    let index = KvBlockIndexer::new();
    for value in 0..64 {
        let hash: Arc<str> = Arc::from(format!("{value:064x}"));
        index.store(
            0,
            owner(),
            StorageTier::Device,
            None,
            &[(hash.clone(), hash)],
        );
    }
    index
}

fn check_live(index: &KvBlockIndexer) {
    for value in 0..64 {
        let results = index.find_matches(&MatchQuery {
            group_idx: 0,
            local_hashes: vec![Arc::from(format!("{value:064x}"))],
            tiers_of_interest: vec![StorageTier::Device],
        });
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].matched_depth, 1);
    }
}

#[cfg(test)]
mod resource_counterexamples {
    use super::*;

    #[test]
    fn incomplete_parent_is_not_a_safe_snapshot() {
        let index = KvBlockIndexer::new();
        let parent: Arc<str> = Arc::from("parent");
        let child: Arc<str> = Arc::from("child");
        // Upstream accepts an unknown parent and anchors the child at root.
        // A production adapter must reject this input before calling store.
        index.store(
            0,
            owner(),
            StorageTier::Device,
            Some(parent),
            &[(child.clone(), child.clone())],
        );
        let matches = index.find_matches(&MatchQuery {
            group_idx: 0,
            local_hashes: vec![child],
            tiers_of_interest: vec![StorageTier::Device],
        });
        assert_eq!(matches.len(), 1);
        assert_eq!(matches[0].matched_depth, 1);
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mode = args.get(1).map(String::as_str).unwrap_or("original");
    if mode == "online" {
        if let Err(error) = online::run() {
            eprintln!("{error}");
            std::process::exit(1);
        }
        return;
    }
    if mode == "wire" {
        if let Err(error) = wire::run(args.get(2).expect("wire requires explicit input path")) {
            eprintln!("{error}");
            std::process::exit(1);
        }
        return;
    }
    assert!(matches!(mode, "original" | "rebuild" | "adapter"));
    let count: usize = args.get(2).map(|v| v.parse().unwrap()).unwrap_or(200_000);
    let budget: usize = args.get(3).map(|v| v.parse().unwrap()).unwrap_or(4096);
    assert!((1..=1_000_000).contains(&count));
    assert!((64..=65_536).contains(&budget));
    if mode == "adapter" {
        directory_tests_support::churn(count, budget);
        return;
    }
    let mut index = snapshot();
    let before = memory_kib("VmRSS:");
    let started = Instant::now();
    let mut rebuilt = 0;
    let mut peak = before;
    for value in 64..count + 64 {
        let hash: Arc<str> = Arc::from(format!("{value:064x}"));
        index.store(
            0,
            owner(),
            StorageTier::Device,
            None,
            &[(hash.clone(), hash.clone())],
        );
        index.remove(0, &owner(), StorageTier::Device, &[hash]);
        if mode == "rebuild" && (value - 63) % budget == 0 {
            peak = peak.max(memory_kib("VmRSS:"));
            check_live(&index);
            // A production adapter must stage/validate the complete live view
            // with a common watermark before swapping. This fixture knows it.
            let staged = snapshot();
            check_live(&staged);
            peak = peak.max(memory_kib("VmRSS:"));
            index = staged;
            rebuilt += 1;
        }
        if (value - 63) % budget == 0 {
            peak = peak.max(memory_kib("VmRSS:"));
        }
    }
    check_live(&index);
    let after = memory_kib("VmRSS:");
    let high_water = memory_kib("VmHWM:");
    peak = peak.max(after);
    println!("{{\"mode\":\"{mode}\",\"churn_blocks\":{count},\"budget\":{budget},\"rebuilds\":{rebuilt},\"rss_before_kib\":{before},\"rss_after_kib\":{after},\"sampled_peak_kib\":{peak},\"vmhwm_kib\":{high_water},\"elapsed_ms\":{}}}", started.elapsed().as_millis());
}

mod directory_tests_support {
    use super::*;
    use directory::{Batch, Binding, Block, Directory, Limits, Op};

    pub fn block(value: usize) -> Block {
        Block {
            id: Arc::from(format!("{value:064x}")),
            parent: None,
            local: Arc::from(format!("{value:064x}")),
            resident: true,
        }
    }
    pub fn binding() -> Binding {
        Binding {
            worker: Arc::from("fixture-worker"),
            instance: 1,
            publisher: Arc::from("publisher-1"),
            identity: Arc::from(format!("{:064x}", 1)),
            block_size: 16,
        }
    }
    pub fn limits(records: usize) -> Limits {
        Limits {
            records,
            operations: 128,
            queued_batches: 128,
            queue_bytes: 1_048_576,
            total_bytes: 64_000_000,
            query_blocks: 4096,
            parent_depth: 1024,
        }
    }
    pub fn churn(count: usize, budget: usize) {
        let binding = binding();
        let mut dir = Directory::new(limits(budget)).unwrap();
        let ticket = dir.begin(binding.clone()).unwrap();
        dir.finish(&ticket, (0..64).map(block).collect(), 0, 0)
            .unwrap();
        let started = Instant::now();
        let before = memory_kib("VmRSS:");
        for (seq, value) in (64..64 + count).enumerate() {
            let b = block(value);
            dir.event(
                &ticket,
                Batch {
                    sequence: seq as u64,
                    ops: vec![Op::Store(b.clone()), Op::Remove(b.id)],
                },
            )
            .unwrap();
            if seq % 4096 == 0 {
                assert_eq!(dir.lookup(&binding, &[block(0).local]), Some(1));
                assert!(dir.retained_records() <= budget);
            }
        }
        println!("{{\"mode\":\"adapter\",\"churn_blocks\":{count},\"budget\":{budget},\"rebuilds\":{},\"retained_records\":{},\"rss_before_kib\":{before},\"rss_after_kib\":{},\"vmhwm_kib\":{},\"elapsed_ms\":{}}}", dir.rebuilds, dir.retained_records(), memory_kib("VmRSS:"), memory_kib("VmHWM:"), started.elapsed().as_millis());
    }
}
