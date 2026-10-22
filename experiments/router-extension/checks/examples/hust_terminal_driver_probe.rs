// SPDX-License-Identifier: Apache-2.0
//! Bounded CPU protocol harness. No engine imports or production admission claim.
use prefix_router_extension_checks::{
    durable_runtime::DurableRuntime,
    hust_terminal_transport::HustTerminalTransport,
    prefix_extension::CloseEvidence,
    runtime_owner::{CachePort, RuntimeOwner, WorkerControl},
    runtime_provider::{AdmissionPort, Decision, RequestView},
    runtime_store::{RuntimeStore, TerminalCursor},
    sqlite_row_runtime_store::SqliteRowRuntimeStore,
    sqlite_runtime_store::DEFAULT_MAX_SNAPSHOT_BYTES,
    terminal_driver::{Ownership, PumpError, Step, TerminalDriver},
};
use serde::Deserialize;
use serde_json::json;
use std::{
    io::{self, BufRead, Read, Write},
    path::PathBuf,
    sync::Arc,
    time::Duration,
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    cursor: TerminalCursor,
    token: String,
    frontend_pid: u32,
    core_pid: u32,
    ownership: String,
    store: PathBuf,
}
#[derive(Debug)]
struct EmptyCache;
impl CachePort for EmptyCache {
    fn lookup(&self, _: &str, _: &str, _: &str, _: &[String]) -> Option<u32> {
        None
    }
}
fn emit(value: serde_json::Value) {
    println!("{value}");
    io::stdout().flush().unwrap();
}
#[tokio::main(flavor = "current_thread")]
async fn main() {
    let path = std::env::args_os()
        .nth(1)
        .expect("private fixture config path");
    let mut input = Vec::new();
    std::fs::File::open(path)
        .unwrap()
        .take(8193)
        .read_to_end(&mut input)
        .unwrap();
    assert!(input.len() <= 8192);
    let config: Config = serde_json::from_slice(&input).expect("bounded fixture config");
    assert_eq!(config.cursor.through, 0);
    let ownership = match config.ownership.as_str() {
        "observe_only" => Ownership::ObserveOnly,
        "exclusive_router_v1" => Ownership::ExclusiveRouterV1,
        _ => panic!("invalid fixture mode"),
    };
    let store =
        Arc::new(SqliteRowRuntimeStore::create(&config.store, DEFAULT_MAX_SNAPSHOT_BYTES).unwrap());
    let owner = Arc::new(
        RuntimeOwner::new(
            Arc::new(EmptyCache),
            "cpu-driver".into(),
            1,
            vec![WorkerControl {
                worker_url: config.cursor.worker_url.clone(),
                instance_epoch: config.cursor.instance_epoch.clone(),
                worker_version: 1,
                cache_identity: "fixture".into(),
                healthy: true,
                draining: false,
                capacity: 8,
            }],
            16,
            16,
        )
        .unwrap(),
    );
    let runtime = Arc::new(
        DurableRuntime::create_with_cursors(owner, store.clone(), vec![config.cursor.clone()])
            .unwrap(),
    );
    let transport = HustTerminalTransport::new(
        config.cursor.clone(),
        config.token,
        config.frontend_pid,
        config.core_pid,
        ownership,
        Duration::from_secs(1),
    )
    .unwrap();
    let mut driver =
        TerminalDriver::new(runtime.clone(), transport, config.cursor.clone(), ownership).unwrap();
    emit(json!({"ready":true,"npu_validated":false}));
    // Commands are fixture-only and bounded. The caller also enforces a process
    // deadline; stdin is not part of the production terminal transport.
    let stdin = io::stdin();
    let mut input = stdin.lock();
    for number in 0..32 {
        let mut bytes = Vec::new();
        input
            .by_ref()
            .take(257)
            .read_until(b'\n', &mut bytes)
            .unwrap();
        if bytes.is_empty() {
            break;
        }
        assert!(bytes.len() <= 256);
        let command = std::str::from_utf8(&bytes).unwrap().trim();
        let result = match command {
            "reserve" => {
                let request_id = format!("request-{number}");
                let request = RequestView {
                    request_id: request_id.clone(),
                    identity: "fixture".into(),
                    block_hashes: Arc::from([]),
                    exact: false,
                };
                let decision = Decision {
                    request_id: request_id.clone(),
                    attempt_id: runtime.allocate_attempt(&request_id).unwrap(),
                    worker_url: config.cursor.worker_url.clone(),
                    instance_epoch: config.cursor.instance_epoch.clone(),
                    worker_version: 1,
                    config_version: 1,
                };
                let lease = runtime.reserve_async(&request, &decision).await.unwrap();
                let attempt = lease.request_id_header().unwrap().to_owned();
                lease.before_send().unwrap();
                lease.sent();
                lease.closed(true, CloseEvidence::TransportEof);
                json!({"attempt":attempt})
            }
            "step" => match driver.step().await {
                Ok(Step::Idle) => json!({"step":"idle"}),
                Ok(Step::Committed(count)) => json!({"step":"committed","count":count}),
                Err(PumpError::Retry) => json!({"step":"retry"}),
                Err(PumpError::Fatal(reason)) => json!({"step":"fatal","reason":reason}),
            },
            _ => panic!("unknown fixture command"),
        };
        let persisted = store.load().unwrap();
        emit(
            json!({"result":result,"active":runtime.owner().active_len().unwrap(),
            "through":driver.cursor().through,"stored_through":persisted.terminal_cursors[0].through,
            "generation":persisted.generation,"pending":driver.pending_remote_confirmation(),
            "failed":runtime.owner().is_failed()}),
        );
    }
    runtime.owner().fail_closed();
}
