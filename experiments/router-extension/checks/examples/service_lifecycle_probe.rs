// SPDX-License-Identifier: Apache-2.0
//! CPU process/signal qualification. Backend transport is explicitly a substitute.
use axum::{routing::get, Router};
use prefix_router_extension_checks::{
    durable_runtime::DurableRuntime,
    prefix_extension::CloseEvidence,
    runtime_owner::{
        control_gate::{ControlSet, MonotonicClock, SourceKey, WorkerGate},
        CachePort, RuntimeOwner, WorkerControl,
    },
    runtime_provider::{AdmissionPort, Decision, RequestView},
    runtime_store::{RuntimeStore, TerminalCursor},
    service_lifecycle::{self, ServiceSupervisor},
    sqlite_row_runtime_store::SqliteRowRuntimeStore,
    sqlite_runtime_store::DEFAULT_MAX_SNAPSHOT_BYTES,
    terminal_driver::{Ownership, RpcFuture, TerminalBatch, TerminalDriver, TerminalTransport},
};
use std::{collections::BTreeMap, io::Write, sync::Arc, time::Duration};

#[derive(Debug)]
struct UnknownCache;
impl CachePort for UnknownCache {
    fn lookup(&self, _: &str, _: &str, _: &str, _: &[String]) -> Option<u32> {
        None
    }
}
struct EmptyTerminal;
impl TerminalTransport for EmptyTerminal {
    fn poll<'a>(&'a mut self, after: &'a TerminalCursor) -> RpcFuture<'a, TerminalBatch> {
        Box::pin(async {
            Ok(TerminalBatch {
                after: after.clone(),
                records: vec![],
            })
        })
    }
    fn acknowledge<'a>(&'a mut self, _: &'a TerminalCursor) -> RpcFuture<'a, ()> {
        Box::pin(async { Ok(()) })
    }
    fn retire<'a>(&'a mut self, _: &'a TerminalCursor) -> RpcFuture<'a, ()> {
        Box::pin(async { Ok(()) })
    }
}
fn emit(value: serde_json::Value) {
    println!("{value}");
    std::io::stdout().flush().unwrap();
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let args: Vec<_> = std::env::args_os().collect();
    assert_eq!(
        args.len(),
        4,
        "usage: probe <private-new-sqlite-path> fresh|debt|recover <drain-ms>"
    );
    let path = std::path::PathBuf::from(&args[1]);
    let mode = args[2].to_str().unwrap();
    let budget = Duration::from_millis(args[3].to_str().unwrap().parse::<u64>().unwrap());
    if mode == "recover" {
        let store =
            Arc::new(SqliteRowRuntimeStore::open(path, DEFAULT_MAX_SNAPSHOT_BYTES).unwrap());
        let stored = store.load().unwrap();
        let recovered = DurableRuntime::recover(
            Arc::new(UnknownCache),
            format!("reopened-{}", std::process::id()),
            store,
            16,
            16,
        )
        .unwrap();
        emit(
            serde_json::json!({"paused": !recovered.admission_ready(), "active": recovered.owner().active_len().unwrap(), "through": stored.terminal_cursors[0].through}),
        );
        return;
    }
    assert!(matches!(mode, "fresh" | "debt"));
    let controls = vec![WorkerControl {
        worker_url: "http://fixture-worker".into(),
        instance_epoch: "instance".into(),
        worker_version: 1,
        cache_identity: "cache".into(),
        healthy: true,
        draining: false,
        capacity: 8,
    }];
    let owner = Arc::new(
        RuntimeOwner::new(
            Arc::new(UnknownCache),
            format!("fixture-{}", std::process::id()),
            1,
            controls,
            16,
            16,
        )
        .unwrap(),
    );
    let cursor = TerminalCursor {
        source_id: "source".into(),
        worker_url: "http://fixture-worker".into(),
        instance_epoch: "instance".into(),
        observer_epoch: "observer".into(),
        through: 0,
    };
    let store = Arc::new(SqliteRowRuntimeStore::create(path, DEFAULT_MAX_SNAPSHOT_BYTES).unwrap());
    let runtime = Arc::new(
        DurableRuntime::create_with_cursors(owner.clone(), store, vec![cursor.clone()]).unwrap(),
    );
    if mode == "debt" {
        let request = RequestView {
            request_id: "fixture".into(),
            identity: "cache".into(),
            block_hashes: Arc::from([]),
            exact: false,
        };
        let decision = Decision {
            request_id: "fixture".into(),
            attempt_id: runtime.allocate_attempt("fixture").unwrap(),
            worker_url: "http://fixture-worker".into(),
            instance_epoch: "instance".into(),
            worker_version: 1,
            config_version: 1,
        };
        let lease = runtime.reserve_async(&request, &decision).await.unwrap();
        lease.before_send().unwrap();
        lease.sent();
        lease.closed(true, CloseEvidence::TransportDropped);
    }
    let identity = SourceKey {
        instance: "instance".into(),
        observer: "observer".into(),
        ownership: "exclusive_router_v1".into(),
    };
    let gate = Arc::new(
        WorkerGate::new(
            identity.clone(),
            Arc::new(MonotonicClock::default()),
            60_000_000_000,
        )
        .unwrap(),
    );
    gate.complete(gate.begin().unwrap(), &identity).unwrap();
    let gates = Arc::new(ControlSet(BTreeMap::from([(
        "http://fixture-worker".into(),
        gate,
    )])));
    owner.install_controls(gates.clone()).unwrap();
    let driver = TerminalDriver::new(
        runtime.clone(),
        EmptyTerminal,
        cursor,
        Ownership::ExclusiveRouterV1,
    )
    .unwrap();
    let supervisor = ServiceSupervisor::start(
        runtime,
        gates,
        vec![driver],
        Duration::from_millis(10),
        Duration::from_secs(5),
        4,
    )
    .unwrap();
    let state = supervisor.state.clone();
    let app = Router::new()
        .route("/work", get(|| async { "ok" }))
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            service_lifecycle::track_http,
        ))
        .route(
            "/readyz",
            get(service_lifecycle::readiness).with_state(state),
        );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    #[cfg(unix)]
    let stop = {
        use tokio::signal::unix::{signal, SignalKind};
        let mut term = signal(SignalKind::terminate()).unwrap();
        let mut int = signal(SignalKind::interrupt()).unwrap();
        let mut hup = signal(SignalKind::hangup()).unwrap();
        async move {
            tokio::select! { _ = term.recv() => (), _ = int.recv() => (), _ = hup.recv() => () }
        }
    };
    #[cfg(not(unix))]
    let stop = async {
        tokio::signal::ctrl_c().await.unwrap();
    };
    emit(
        serde_json::json!({"origin": format!("http://{}", listener.local_addr().unwrap()), "pid": std::process::id()}),
    );
    let result = service_lifecycle::serve_until(listener, app, supervisor, stop, budget).await;
    emit(
        serde_json::json!({"shutdown": format!("{result:?}"), "active": owner.active_len().unwrap(), "scope": "cpu-substitute"}),
    );
    std::process::exit(result.exit_code());
}
