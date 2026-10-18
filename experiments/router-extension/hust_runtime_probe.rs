// SPDX-License-Identifier: Apache-2.0
//! Finite public HTTP qualification with real M1, native KV and HUST terminals.
//! Run only against the two owned, initially cold HUST services in its config.
#![allow(unexpected_cfgs)] // Optional candidate feature is absent in the original host manifest.
use futures_util::StreamExt;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fmt,
    io::Read,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use vllm_router_rs::{
    config::types::{PolicyConfig, RouterConfig, RoutingMode},
    routers::http::router::Router,
    server::{build_app, AppContext, AppState},
    tokenizer::Tokenizer,
};

pub mod kv_index {
    pub use vllm_router_rs::kv_index::*;
}
mod prefix_extension {
    pub use vllm_router_rs::routers::http::prefix_extension::*;
}
#[path = "../src/routers/http/runtime_directory.rs"]
mod directory;
#[cfg(feature = "durable-candidate")]
#[path = "../src/routers/http/durable_runtime.rs"]
mod durable_runtime;
#[path = "../src/routers/http/hust_source_control.rs"]
mod hust_source_control;
#[path = "../src/routers/http/native_kv_owner.rs"]
mod native_kv_owner;
#[path = "../src/routers/http/native_kv_wire.rs"]
mod native_kv_wire;
#[path = "../src/routers/http/native_kv_zmq.rs"]
mod native_kv_zmq;
#[path = "../src/routers/http/runtime_pool.rs"]
mod pool;
#[path = "../src/routers/http/request_feature_port.rs"]
mod request_feature_port;
#[path = "../src/routers/http/runtime_cache_port.rs"]
mod runtime_cache_port;
#[path = "../src/routers/http/runtime_owner.rs"]
mod runtime_owner;
#[path = "../src/routers/http/runtime_provider.rs"]
mod runtime_provider;
#[cfg(feature = "durable-candidate")]
#[path = "../src/routers/http/runtime_store.rs"]
mod runtime_store;
#[cfg(feature = "durable-candidate")]
#[path = "../src/routers/http/sqlite_row_runtime_store.rs"]
mod sqlite_row_runtime_store;
#[cfg(feature = "durable-candidate")]
#[path = "../src/routers/http/sqlite_runtime_store.rs"]
mod sqlite_runtime_store;

use directory::{Binding, Limits};
use hust_source_control::{HustSourceControl, SourceBinding, SourceIdentity};
use native_kv_owner::NativeKvOwner;
use native_kv_zmq::ZmqNativeTransport;
use request_feature_port::{CompletionFeaturePort, TokenizerPort};
use runtime_cache_port::{InProcessCache, MonotonicClock, PartitionSpec};
use runtime_owner::{CachePort, RuntimeOwner, TerminalResult, WorkerControl};
use runtime_provider::{FeaturePort, RuntimeProvider};
#[cfg(feature = "durable-candidate")]
use {
    durable_runtime::{DurableRuntime, TerminalRecord},
    runtime_store::{RuntimeStore, TerminalCursor},
    sqlite_row_runtime_store::SqliteRowRuntimeStore,
    sqlite_runtime_store::DEFAULT_MAX_SNAPSHOT_BYTES,
};

#[cfg(feature = "durable-candidate")]
type DurableOption = Option<Arc<DurableRuntime>>;
#[cfg(not(feature = "durable-candidate"))]
type DurableOption = Option<()>;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct Worker {
    origin: String,
    token: String,
    worker_id: String,
    frontend_pid: u32,
    live_endpoint: String,
    replay_endpoint: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    workers: Vec<Worker>,
    tokenizer: PathBuf,
    tokenizer_sha256: String,
    identity: String,
    model: String,
    #[serde(default)]
    durable_store: Option<PathBuf>,
    #[serde(default = "default_terminal_ownership_mode")]
    terminal_ownership_mode: String,
}

fn default_terminal_ownership_mode() -> String {
    "observe_only".into()
}

struct LocalTokenizer(Tokenizer);
impl fmt::Debug for LocalTokenizer {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("LocalTokenizer")
    }
}
impl TokenizerPort for LocalTokenizer {
    fn encode(&self, text: &str) -> Result<Vec<u32>, &'static str> {
        self.0
            .encode(text)
            .map(|value| value.token_ids().to_vec())
            .map_err(|_| "tokenizer_failed")
    }
}

struct Session {
    spec: PartitionSpec,
    identity: SourceIdentity,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
    errors: Arc<Mutex<Vec<&'static str>>>,
}
impl Session {
    fn start(
        worker: Worker,
        cache: Arc<InProcessCache>,
        identity: String,
        incarnation: u64,
    ) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let errors = Arc::new(Mutex::new(Vec::new()));
        let (ready_tx, ready_rx) = mpsc::sync_channel(1);
        let task_stop = stop.clone();
        let task_errors = errors.clone();
        let thread = thread::spawn(move || {
            let result = (|| {
                let control = HustSourceControl::connect_cold(
                    &worker.origin,
                    worker.token,
                    &worker.worker_id,
                    worker.frontend_pid,
                )?;
                let source = control.identity().clone();
                let spec = PartitionSpec {
                    binding: Binding {
                        worker: Arc::from(worker.origin),
                        instance: incarnation,
                        publisher: Arc::from(format!(
                            "{}-connection-{incarnation}",
                            source.binding.instance_epoch
                        )),
                        identity: Arc::from(identity),
                        block_size: 128,
                    },
                    instance_epoch: Arc::from(source.binding.instance_epoch.clone()),
                };
                let transport = ZmqNativeTransport::connect(
                    Arc::new(zmq::Context::new()),
                    &worker.live_endpoint,
                    &worker.replay_endpoint,
                    128,
                    Duration::from_secs(2),
                )?;
                let mut owner = NativeKvOwner::bootstrap(
                    cache,
                    spec.clone(),
                    None,
                    Limits {
                        records: 512,
                        operations: 512,
                        queued_batches: 16,
                        queue_bytes: 65536,
                        total_bytes: 262144,
                        query_blocks: 64,
                        parent_depth: 64,
                    },
                    10_000_000_000,
                    128,
                    transport,
                    control,
                )?;
                ready_tx.send(Ok((spec, source))).unwrap();
                let mut refresh = Instant::now() + Duration::from_millis(500);
                while !task_stop.load(Ordering::Acquire) {
                    owner.poll_once(Duration::from_millis(25))?;
                    if Instant::now() >= refresh {
                        owner.refresh()?;
                        refresh = Instant::now() + Duration::from_millis(500);
                    }
                }
                owner.shutdown()?;
                Ok::<_, native_kv_owner::OwnerFault>(())
            })();
            if let Err(error) = result {
                task_errors.lock().unwrap().push("native_session_failed");
                eprintln!("native KV session failed closed: {error:?}");
                let _ = ready_tx.send(Err(error));
            }
        });
        let (spec, identity) = ready_rx
            .recv_timeout(Duration::from_secs(15))
            .expect("KV startup timed out")
            .unwrap_or_else(|error| panic!("KV startup failed: {error:?}"));
        Self {
            spec,
            identity,
            stop,
            thread: Some(thread),
            errors,
        }
    }
    fn stop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(handle) = self.thread.take() {
            handle.join().expect("KV owner panicked");
        }
        assert!(self.errors.lock().unwrap().is_empty(), "KV owner failed");
    }
}
impl Drop for Session {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(handle) = self.thread.take() {
            let _ = handle.join();
        }
    }
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
struct Ended {
    binding: SourceBinding,
    sequence: u64,
    attempt_id: String,
}
#[derive(Deserialize)]
struct Family {
    attempt_id: String,
    expected: usize,
    bound: usize,
    ended: usize,
    sealed: bool,
    abandoned: bool,
    core_statuses: BTreeMap<String, usize>,
}
#[derive(Deserialize)]
struct TerminalReply {
    schema_version: u32,
    binding: SourceBinding,
    frontend_pid: u32,
    core_pid: u32,
    boundary: u64,
    mode: String,
    records: Vec<Ended>,
    families: Vec<Family>,
    disabled: bool,
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct RetirementReply {
    schema_version: u32,
    binding: SourceBinding,
    retired: usize,
    retired_through: u64,
    family_count: usize,
    reserved_children: usize,
}

fn terminal_poll(
    worker: Worker,
    source: SourceIdentity,
    after: u64,
    acknowledge: u64,
) -> (TerminalReply, BTreeMap<String, usize>) {
    let client = reqwest::blocking::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap();
    let mut url =
        reqwest::Url::parse(&format!("{}/prefix-routing/terminal", worker.origin)).unwrap();
    url.query_pairs_mut()
        .append_pair("observer_epoch", &source.binding.observer_epoch)
        .append_pair("after", &after.to_string())
        .append_pair("acknowledge", &acknowledge.to_string());
    let response = client
        .get(url)
        .header("X-Prefix-Routing-Control", worker.token)
        .send()
        .expect("terminal source unavailable");
    assert_eq!(response.status().as_u16(), 200);
    let mut bytes = Vec::new();
    response.take(1_048_577).read_to_end(&mut bytes).unwrap();
    assert!(bytes.len() <= 1_048_576);
    let reply: TerminalReply = serde_json::from_slice(&bytes).expect("terminal reply malformed");
    assert!(reply.schema_version == 1 && !reply.disabled && reply.mode == "replay");
    assert_eq!(reply.binding, source.binding);
    assert_eq!(reply.frontend_pid, source.frontend_pid);
    assert_eq!(reply.core_pid, source.core_pid);
    assert!(reply.records.len() <= 64 && reply.families.len() <= 64);
    assert_eq!(
        reply.boundary,
        after.checked_add(reply.records.len() as u64).unwrap()
    );
    let families: BTreeMap<_, _> = reply
        .families
        .iter()
        .map(|value| (value.attempt_id.as_str(), value))
        .collect();
    assert_eq!(families.len(), reply.families.len());
    // Validate the complete batch before the first ledger update.
    for (offset, item) in reply.records.iter().enumerate() {
        assert_eq!(item.binding, source.binding);
        assert_eq!(item.sequence, after + offset as u64 + 1);
        let family = families
            .get(item.attempt_id.as_str())
            .expect("missing complete family");
        assert!(
            family.sealed && !family.abandoned && family.expected > 0 && family.expected <= 256
        );
        assert_eq!(family.expected, family.bound);
        assert_eq!(family.bound, family.ended);
        assert_eq!(
            family
                .core_statuses
                .values()
                .try_fold(0_usize, |total, count| total.checked_add(*count))
                .expect("status count overflow"),
            family.ended
        );
        for (status, count) in &family.core_statuses {
            assert!(
                *count > 0
                    && *count <= family.ended
                    && matches!(
                        status.as_str(),
                        "unknown"
                            | "FINISHED_STOPPED"
                            | "FINISHED_LENGTH_CAPPED"
                            | "FINISHED_ABORTED"
                            | "FINISHED_IGNORED"
                            | "FINISHED_ERROR"
                            | "FINISHED_REPETITION"
                    )
            );
        }
    }
    let mut statuses = BTreeMap::new();
    for item in &reply.records {
        for (status, count) in &families[item.attempt_id.as_str()].core_statuses {
            *statuses.entry(status.clone()).or_insert(0) += count;
        }
    }
    (reply, statuses)
}

fn terminal_retire(worker: Worker, source: SourceIdentity, through: u64) -> RetirementReply {
    assert_eq!(source.ownership_mode, "exclusive_router_v1");
    let client = reqwest::blocking::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap();
    let mut url =
        reqwest::Url::parse(&format!("{}/prefix-routing/terminal/retire", worker.origin)).unwrap();
    url.query_pairs_mut()
        .append_pair("observer_epoch", &source.binding.observer_epoch)
        .append_pair("through", &through.to_string())
        .append_pair("checkpointed_through", &through.to_string());
    let response = client
        .post(url)
        .header("X-Prefix-Routing-Control", worker.token)
        .json(&serde_json::json!({}))
        .send()
        .expect("terminal retirement unavailable");
    assert_eq!(response.status().as_u16(), 200);
    let mut bytes = Vec::new();
    response.take(4097).read_to_end(&mut bytes).unwrap();
    assert!(bytes.len() <= 4096);
    let reply: RetirementReply =
        serde_json::from_slice(&bytes).expect("terminal retirement reply malformed");
    assert_eq!(reply.schema_version, 1);
    assert_eq!(reply.binding, source.binding);
    assert_eq!(reply.retired_through, through);
    assert!(reply.family_count <= 64);
    assert!(reply.family_count <= reply.reserved_children);
    assert!(reply.reserved_children <= 256);
    reply
}

async fn reconcile(
    config: &Config,
    sessions: &[Session],
    cursors: &mut [u64],
    owner: &Arc<RuntimeOwner>,
    durable: &DurableOption,
    retired_families: &mut usize,
) -> BTreeMap<String, usize> {
    let deadline = Instant::now() + Duration::from_secs(15);
    let mut statuses = BTreeMap::new();
    while owner.active_len().unwrap() != 0 {
        for index in 0..config.workers.len() {
            let worker = config.workers[index].clone();
            let source = sessions[index].identity.clone();
            let after = cursors[index];
            let (reply, observed) =
                tokio::task::spawn_blocking(move || terminal_poll(worker, source, after, 0))
                    .await
                    .unwrap();
            #[cfg(feature = "durable-candidate")]
            if let Some(runtime) = durable {
                if !reply.records.is_empty() {
                    let source = &sessions[index].identity.binding;
                    let cursor = TerminalCursor {
                        source_id: config.workers[index].worker_id.clone(),
                        worker_url: config.workers[index].origin.clone(),
                        instance_epoch: source.instance_epoch.clone(),
                        observer_epoch: source.observer_epoch.clone(),
                        through: after,
                    };
                    let records: Vec<_> = reply
                        .records
                        .iter()
                        .map(|item| TerminalRecord {
                            sequence: item.sequence,
                            attempt_id: item.attempt_id.clone(),
                            worker_url: config.workers[index].origin.clone(),
                            instance_epoch: source.instance_epoch.clone(),
                        })
                        .collect();
                    let through = runtime
                        .apply_terminal_batch(cursor.clone(), &records)
                        .await
                        .unwrap();
                    assert_eq!(through, reply.boundary);
                    let worker = config.workers[index].clone();
                    let source = sessions[index].identity.clone();
                    let (replayed, _) = tokio::task::spawn_blocking(move || {
                        terminal_poll(worker, source, after, 0)
                    })
                    .await
                    .unwrap();
                    assert_eq!(replayed.records, reply.records);
                    // A lost ACK must not authorize applying the old batch again.
                    assert_eq!(
                        runtime.apply_terminal_batch(cursor, &records).await,
                        Err("terminal_cursor_conflict")
                    );
                    assert_eq!(
                        runtime
                            .terminal_cursor(&config.workers[index].worker_id)
                            .unwrap()
                            .unwrap()
                            .through,
                        through
                    );
                    let worker = config.workers[index].clone();
                    let source = sessions[index].identity.clone();
                    let (ack, _) = tokio::task::spawn_blocking(move || {
                        terminal_poll(worker, source, through, through)
                    })
                    .await
                    .unwrap();
                    assert_eq!(ack.boundary, through);
                    assert!(ack.records.is_empty());
                    if config.terminal_ownership_mode == "exclusive_router_v1" {
                        let worker = config.workers[index].clone();
                        let source = sessions[index].identity.clone();
                        let retired = tokio::task::spawn_blocking(move || {
                            terminal_retire(worker, source, through)
                        })
                        .await
                        .unwrap();
                        assert_eq!(retired.retired, reply.records.len());
                        *retired_families = retired_families
                            .checked_add(retired.retired)
                            .expect("retirement count overflow");

                        // A lost successful response is safe to retry at the same durable waterline.
                        let worker = config.workers[index].clone();
                        let source = sessions[index].identity.clone();
                        let repeated = tokio::task::spawn_blocking(move || {
                            terminal_retire(worker, source, through)
                        })
                        .await
                        .unwrap();
                        assert_eq!(repeated.retired, 0);
                        assert_eq!(
                            repeated,
                            RetirementReply {
                                retired: 0,
                                ..retired
                            }
                        );
                    }
                }
            } else {
                settle_volatile(
                    owner,
                    &config.workers[index],
                    &sessions[index].identity,
                    &reply,
                );
            }
            #[cfg(not(feature = "durable-candidate"))]
            {
                let _ = durable;
                settle_volatile(
                    owner,
                    &config.workers[index],
                    &sessions[index].identity,
                    &reply,
                );
            }
            cursors[index] = reply.boundary;
            for (status, count) in observed {
                *statuses.entry(status).or_insert(0) += count;
            }
        }
        assert!(
            Instant::now() < deadline,
            "real terminal reconciliation timed out"
        );
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    statuses
}

fn settle_volatile(
    owner: &RuntimeOwner,
    worker: &Worker,
    source: &SourceIdentity,
    reply: &TerminalReply,
) {
    for item in &reply.records {
        assert!(matches!(
            owner
                .apply_terminal(
                    &item.attempt_id,
                    &worker.origin,
                    &source.binding.instance_epoch
                )
                .unwrap(),
            TerminalResult::Applied | TerminalResult::Duplicate
        ));
        assert_eq!(
            owner
                .apply_terminal(
                    &item.attempt_id,
                    &worker.origin,
                    &source.binding.instance_epoch
                )
                .unwrap(),
            TerminalResult::Duplicate
        );
    }
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() {
    let path = PathBuf::from(std::env::args_os().nth(1).expect("private config required"));
    assert!(std::fs::metadata(&path).unwrap().len() <= 16384);
    let config: Config = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    assert_eq!(config.workers.len(), 2);
    assert!(matches!(
        config.terminal_ownership_mode.as_str(),
        "observe_only" | "exclusive_router_v1"
    ));
    assert!(
        config.terminal_ownership_mode == "observe_only" || config.durable_store.is_some(),
        "managed retirement requires the durable candidate"
    );
    assert_eq!(config.identity.len(), 64);
    assert!(std::fs::metadata(&config.tokenizer).unwrap().len() <= 256 * 1024 * 1024);
    let asset = std::fs::read(&config.tokenizer).unwrap();
    assert_eq!(
        format!("{:x}", Sha256::digest(&asset)),
        config.tokenizer_sha256
    );
    let tokenizer = Arc::new(LocalTokenizer(
        Tokenizer::from_file(config.tokenizer.to_str().unwrap()).unwrap(),
    ));
    assert_eq!(
        format!(
            "{:x}",
            Sha256::digest(std::fs::read(&config.tokenizer).unwrap())
        ),
        config.tokenizer_sha256
    );
    let features = Arc::new(
        CompletionFeaturePort::new(
            tokenizer,
            config.model.clone(),
            config.identity.clone(),
            uuid::Uuid::new_v4().simple().to_string(),
            128,
            65536,
            1024,
        )
        .unwrap(),
    );
    let cache =
        Arc::new(InProcessCache::new(2, 524288, Arc::new(MonotonicClock::default())).unwrap());
    let mut sessions = Vec::new();
    for (index, worker) in config.workers.iter().cloned().enumerate() {
        let cache = cache.clone();
        let identity = config.identity.clone();
        sessions.push(
            tokio::task::spawn_blocking(move || {
                Session::start(worker, cache, identity, index as u64 + 1)
            })
            .await
            .unwrap(),
        );
    }
    assert!(sessions
        .iter()
        .all(|session| session.identity.ownership_mode == config.terminal_ownership_mode));
    let owner = Arc::new(
        RuntimeOwner::new(
            cache.clone(),
            uuid::Uuid::new_v4().simple().to_string(),
            1,
            sessions
                .iter()
                .map(|s| WorkerControl {
                    worker_url: s.spec.binding.worker.to_string(),
                    instance_epoch: s.spec.instance_epoch.to_string(),
                    worker_version: 1,
                    cache_identity: config.identity.clone(),
                    healthy: true,
                    draining: false,
                    capacity: 2,
                })
                .collect(),
            16,
            16,
        )
        .unwrap(),
    );
    #[cfg(feature = "durable-candidate")]
    let durable = config.durable_store.as_ref().map(|path| {
        assert!(!path.exists(), "candidate store must be newly created");
        Arc::new(
            DurableRuntime::create(
                owner.clone(),
                Arc::new(SqliteRowRuntimeStore::create(path, DEFAULT_MAX_SNAPSHOT_BYTES).unwrap()),
            )
            .unwrap(),
        )
    });
    #[cfg(not(feature = "durable-candidate"))]
    let durable = {
        assert!(
            config.durable_store.is_none(),
            "binary lacks durable candidate feature"
        );
        None::<()>
    };
    let urls: Vec<_> = config.workers.iter().map(|w| w.origin.clone()).collect();
    assert!(
        urls[0] < urls[1],
        "stable tie-break qualification requires ordered origins"
    );
    let mut router_config = RouterConfig::new(
        RoutingMode::Regular {
            worker_urls: urls.clone(),
        },
        PolicyConfig::RoundRobin,
    );
    router_config.disable_retries = true;
    router_config.disable_circuit_breaker = true;
    router_config.worker_startup_timeout_secs = 10;
    router_config.worker_startup_check_interval_secs = 1;
    let client = reqwest::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(60))
        .build()
        .unwrap();
    let context =
        Arc::new(AppContext::new(router_config, client.clone(), 16, None, vec![]).unwrap());
    let snapshot: Arc<dyn runtime_provider::SnapshotPort>;
    let admission: Arc<dyn runtime_provider::AdmissionPort>;
    #[cfg(feature = "durable-candidate")]
    {
        if let Some(runtime) = &durable {
            snapshot = runtime.clone();
            admission = runtime.clone();
        } else {
            snapshot = owner.clone();
            admission = owner.clone();
        }
    }
    #[cfg(not(feature = "durable-candidate"))]
    {
        snapshot = owner.clone();
        admission = owner.clone();
    }
    context
        .policy_registry
        .install_http_extension(Arc::new(RuntimeProvider::new(
            features.clone(),
            snapshot,
            admission,
        )))
        .unwrap();
    let router = Arc::new(Router::new(urls, &context).await.unwrap());
    let app = build_app(
        Arc::new(AppState {
            router,
            context,
            concurrency_queue_tx: None,
            router_manager: None,
        }),
        65536,
        vec!["x-request-id".into()],
        vec![],
        false,
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let origin = format!("http://{}", listener.local_addr().unwrap());
    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel::<()>();
    let http = tokio::spawn(async move {
        axum::serve(listener, app)
            .with_graceful_shutdown(async {
                let _ = shutdown_rx.await;
            })
            .await
            .unwrap();
    });
    let prompts = [
        "A reusable prefix supports efficient inference. ".repeat(64),
        "Distributed directories require independent replica identities. ".repeat(64),
    ];
    let query = |index: usize| {
        features
            .extract(
                &serde_json::json!({"model": config.model, "prompt": prompts[index]}),
                "/v1/completions",
            )
            .unwrap()
            .block_hashes
    };
    let queries = [query(0), query(1)];
    let lookup = |index: usize, prompt: usize| {
        CachePort::lookup(
            &*cache,
            &config.workers[index].origin,
            &sessions[index].spec.instance_epoch,
            &config.identity,
            &queries[prompt],
        )
    };
    // Prewarm only B directly after both subscriptions established a cold origin.
    let prewarm: serde_json::Value = client.post(format!("{}/v1/completions", config.workers[1].origin)).json(&serde_json::json!({"model": config.model, "prompt": prompts[1], "max_tokens": 16, "temperature": 0})).send().await.unwrap().error_for_status().unwrap().json().await.unwrap();
    assert_eq!(
        prewarm["usage"]["prompt_tokens_details"]["cached_tokens"],
        0
    );
    let deadline = Instant::now() + Duration::from_secs(5);
    while lookup(1, 1) != Some(3) {
        assert!(Instant::now() < deadline);
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    assert_eq!(lookup(0, 1), Some(0));
    let mut cursors = vec![0; 2];
    let mut retired_families = 0_usize;
    let mut results = Vec::new();
    for (prompt, expected_worker, expected_cached) in [(0, 0, 0), (0, 0, 384), (1, 1, 384)] {
        let response = client.post(format!("{origin}/v1/completions")).json(&serde_json::json!({"model": config.model, "prompt": prompts[prompt], "max_tokens": 16, "temperature": 0})).send().await.unwrap();
        assert_eq!(response.status().as_u16(), 200);
        let reply: serde_json::Value = response.json().await.unwrap();
        assert_eq!(
            reply["usage"]["prompt_tokens_details"]["cached_tokens"],
            expected_cached
        );
        let active = owner.checkpoint().unwrap().active;
        assert_eq!(active.len(), 1);
        assert_eq!(active[0].worker_url, config.workers[expected_worker].origin);
        #[cfg(feature = "durable-candidate")]
        if let Some(path) = &config.durable_store {
            let reopened =
                Arc::new(SqliteRowRuntimeStore::open(path, DEFAULT_MAX_SNAPSHOT_BYTES).unwrap());
            let persisted = reopened.load().unwrap();
            assert_eq!(persisted.checkpoint.active.len(), 1);
            assert_eq!(
                persisted.checkpoint.active[0].attempt_id,
                active[0].attempt_id
            );
            let paused = DurableRuntime::recover(
                cache.clone(),
                uuid::Uuid::new_v4().simple().to_string(),
                reopened,
                16,
                16,
            )
            .unwrap();
            assert!(!paused.admission_ready());
            let debt = paused.owner().checkpoint().unwrap().active;
            assert_eq!(debt.len(), 1);
            assert_eq!(debt[0].attempt_id, active[0].attempt_id);
            assert_eq!(debt[0].phase, runtime_owner::AttemptPhase::Uncertain);
        }
        results
            .push(serde_json::json!({"worker": expected_worker, "cached_tokens": expected_cached}));
        reconcile(
            &config,
            &sessions,
            &mut cursors,
            &owner,
            &durable,
            &mut retired_families,
        )
        .await;
        assert_eq!(owner.active_len().unwrap(), 0);
    }
    let invalid = client.post(format!("{origin}/v1/completions"))
        .json(&serde_json::json!({"model": config.model, "prompt": ["unsupported-array"], "max_tokens": 1}))
        .send().await.unwrap();
    assert!(!invalid.status().is_success());
    assert_eq!(owner.active_len().unwrap(), 0);

    let response = client.post(format!("{origin}/v1/completions"))
        .json(&serde_json::json!({"model": config.model, "prompt": prompts[0], "max_tokens": 256, "temperature": 0, "ignore_eos": true, "stream": true}))
        .send().await.unwrap().error_for_status().unwrap();
    let mut stream = response.bytes_stream();
    let mut saw_text = false;
    let mut pending = Vec::new();
    for _ in 0..64 {
        let chunk = tokio::time::timeout(Duration::from_secs(10), stream.next())
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        pending.extend_from_slice(&chunk);
        assert!(pending.len() <= 65536);
        while let Some(boundary) = pending.iter().position(|byte| *byte == b'\n') {
            let line: Vec<u8> = pending.drain(..=boundary).collect();
            if let Some(data) = std::str::from_utf8(&line)
                .unwrap()
                .trim()
                .strip_prefix("data: ")
            {
                if data == "[DONE]" {
                    continue;
                }
                let value: serde_json::Value = serde_json::from_str(data).unwrap();
                if value["choices"][0]["text"]
                    .as_str()
                    .is_some_and(|text| !text.is_empty())
                {
                    saw_text = true;
                }
            }
        }
        if saw_text {
            break;
        }
    }
    assert!(saw_text, "cancel must follow an actual generated output");
    drop(stream);
    assert_eq!(owner.active_len().unwrap(), 1);
    let cancellation = reconcile(
        &config,
        &sessions,
        &mut cursors,
        &owner,
        &durable,
        &mut retired_families,
    )
    .await;
    assert_eq!(cancellation.get("FINISHED_ABORTED"), Some(&1));
    // Graceful HTTP shutdown precedes source teardown; source stop invalidates M2.
    shutdown_tx.send(()).unwrap();
    http.await.unwrap();
    let mut durable_verified = false;
    #[cfg(feature = "durable-candidate")]
    if let Some(path) = &config.durable_store {
        let reopened =
            Arc::new(SqliteRowRuntimeStore::open(path, DEFAULT_MAX_SNAPSHOT_BYTES).unwrap());
        let stored = reopened.load().unwrap();
        assert!(stored.checkpoint.active.is_empty());
        for (index, worker) in config.workers.iter().enumerate() {
            assert_eq!(
                stored
                    .terminal_cursors
                    .iter()
                    .find(|cursor| cursor.source_id == worker.worker_id)
                    .unwrap()
                    .through,
                cursors[index]
            );
        }
        let recovered = DurableRuntime::recover(
            cache.clone(),
            uuid::Uuid::new_v4().simple().to_string(),
            reopened,
            16,
            16,
        )
        .unwrap();
        assert!(!recovered.admission_ready());
        assert_eq!(recovered.owner().active_len().unwrap(), 0);
        durable_verified = true;
    }
    #[cfg(not(feature = "durable-candidate"))]
    {
        let _ = &mut durable_verified;
    }
    for session in &mut sessions {
        session.stop();
    }
    for session in &sessions {
        assert_eq!(
            cache.lookup(
                &session.spec.binding.worker,
                &session.spec.instance_epoch,
                &config.identity,
                &queries[0]
            ),
            None
        );
    }
    println!(
        "{}",
        serde_json::json!({"status": "pass", "scope": "finite-public-http-real-M1-M2-M3-M4-M5-M7", "requests": results, "cancel_core_statuses": cancellation, "active_after_terminal": owner.active_len().unwrap(), "real_native_subscription": true, "durable_commit_ack_and_paused_reopen_verified": durable_verified, "managed_terminal_retirement_verified": config.terminal_ownership_mode == "exclusive_router_v1" && durable_verified && retired_families == 4, "retired_families": retired_families, "durable_recovery_verified": false, "production_lifecycle_verified": false, "performance_verified": false})
    );
}
