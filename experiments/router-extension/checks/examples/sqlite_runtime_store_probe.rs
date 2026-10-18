// SPDX-License-Identifier: Apache-2.0
//! Explicit local-filesystem commit-latency probe; not a production benchmark.
use prefix_router_extension_checks::{
    runtime_owner::{ActiveAttempt, AttemptPhase, OwnerCheckpoint, WorkerControl},
    runtime_store::{RuntimeStore, StoredRuntime, RUNTIME_SCHEMA},
    sqlite_row_runtime_store::SqliteRowRuntimeStore,
    sqlite_runtime_store::{SqliteRuntimeStore, DEFAULT_MAX_SNAPSHOT_BYTES},
};
use std::{env, fs, path::PathBuf, time::Instant};

fn percentile(sorted: &[u128], numerator: usize, denominator: usize) -> u128 {
    let index = (sorted.len() * numerator)
        .div_ceil(denominator)
        .saturating_sub(1);
    sorted[index.min(sorted.len() - 1)]
}

fn checkpoint(active: usize) -> OwnerCheckpoint {
    OwnerCheckpoint {
        process_epoch: "probe-owner".into(),
        config_version: 1,
        workers: vec![WorkerControl {
            worker_url: "http://127.0.0.1:9000".into(),
            instance_epoch: "probe-instance".into(),
            worker_version: 1,
            cache_identity: "probe-identity".into(),
            healthy: true,
            draining: false,
            capacity: 8192,
        }],
        active: (0..active)
            .map(|index| ActiveAttempt {
                request_id: format!("request-{index}"),
                attempt_id: format!("prm-probe-owner-{index}"),
                worker_url: "http://127.0.0.1:9000".into(),
                instance_epoch: "probe-instance".into(),
                worker_version: 1,
                config_version: 1,
                phase: AttemptPhase::Uncertain,
            })
            .collect(),
        retired: vec![],
        next_attempt: active as u64,
    }
}

fn file_size(path: PathBuf) -> u64 {
    fs::metadata(path).map_or(0, |metadata| metadata.len())
}

fn main() {
    let arguments: Vec<String> = env::args().collect();
    assert!(
        (4..=5).contains(&arguments.len()),
        "usage: probe PATH ITERATIONS ACTIVE [snapshot|rows]"
    );
    let path = PathBuf::from(&arguments[1]);
    let iterations: usize = arguments[2].parse().expect("valid iterations");
    let active: usize = arguments[3].parse().expect("valid active count");
    assert!((10..=10_000).contains(&iterations));
    assert!(active <= 4096);
    assert!(!path.exists(), "probe path must not exist");
    assert!(path.parent().is_some_and(|parent| parent.is_dir()));

    let layout = arguments.get(4).map_or("snapshot", String::as_str);
    let store: Box<dyn RuntimeStore> = match layout {
        "snapshot" => {
            Box::new(SqliteRuntimeStore::create(&path, DEFAULT_MAX_SNAPSHOT_BYTES).unwrap())
        }
        "rows" => {
            Box::new(SqliteRowRuntimeStore::create(&path, DEFAULT_MAX_SNAPSHOT_BYTES).unwrap())
        }
        _ => panic!("unknown storage layout"),
    };
    let mut state = StoredRuntime {
        schema_version: RUNTIME_SCHEMA,
        owner_epoch: "probe-owner".into(),
        generation: 1,
        checkpoint: checkpoint(active),
        terminal_cursors: vec![],
    };
    store.initialize(&state).unwrap();
    let mut samples = Vec::with_capacity(iterations);
    let warmup = 50;
    for index in 0..iterations + warmup {
        let expected = state.generation;
        state.generation += 1;
        state.checkpoint.next_attempt += 1;
        // Change one existing record, rather than measuring a metadata-only
        // commit while the active rows never change. This is a store probe,
        // not an engine/request lifecycle simulation.
        if active > 0 {
            let record = &mut state.checkpoint.active[index % active];
            record.phase = if record.phase == AttemptPhase::Uncertain {
                AttemptPhase::Sent
            } else {
                AttemptPhase::Uncertain
            };
        }
        let started = Instant::now();
        store.commit("probe-owner", expected, &state).unwrap();
        let elapsed = started.elapsed().as_micros();
        if index >= warmup {
            samples.push(elapsed);
        }
    }
    assert_eq!(store.load().unwrap(), state);
    samples.sort_unstable();
    let report = serde_json::json!({
        "schema_version": 1,
        "scope": "sqlite-full-wal-single-commit",
        "layout": layout,
        "workload": "metadata-and-one-active-row-update",
        "warmup_commits": warmup,
        "architecture": env::consts::ARCH,
        "os": env::consts::OS,
        "iterations": iterations,
        "active_records": active,
        "p50_us": percentile(&samples, 50, 100),
        "p95_us": percentile(&samples, 95, 100),
        "p99_us": percentile(&samples, 99, 100),
        "max_us": samples[samples.len() - 1],
        "database_bytes": file_size(path.clone()),
        "wal_bytes": file_size(PathBuf::from(format!("{}-wal", path.display()))),
        "shm_bytes": file_size(PathBuf::from(format!("{}-shm", path.display()))),
        "production_performance_verified": false,
    });
    println!("{}", serde_json::to_string(&report).unwrap());
}
