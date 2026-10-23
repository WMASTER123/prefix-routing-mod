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
#[cfg(feature = "durable-candidate")]
#[path = "../src/routers/http/hust_terminal_transport.rs"]
pub mod hust_terminal_transport;
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
#[cfg(feature = "durable-candidate")]
#[path = "../src/routers/http/terminal_driver.rs"]
pub mod terminal_driver;

use directory::{Binding, Limits};
use hust_source_control::{HealthFault, HustSourceControl, SourceBinding, SourceIdentity};
use native_kv_owner::{NativeKvOwner, SourceControl};
use native_kv_zmq::ZmqNativeTransport;
use request_feature_port::{CompletionFeaturePort, TokenizerPort};
use runtime_cache_port::{InProcessCache, MonotonicClock, PartitionSpec};
use runtime_owner::control_gate::{ControlSet, SourceKey, WorkerGate};
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
    #[serde(default)]
    health_origin: Option<String>,
    #[serde(default)]
    retirement_origin: Option<String>,
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
    #[serde(default)]
    expected_retire_response_losses: usize,
    #[serde(default)]
    churn_families: usize,
    #[serde(default)]
    churn_children_per_family: usize,
    #[serde(default)]
    exercise_control_gate: bool,
    #[serde(default)]
    use_terminal_driver: bool,
    #[serde(default)]
    background_terminal_driver: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum RunMode {
    Qualify,
    CrashAfterSend,
    RecoverAfterCrash,
    RejectReplacedSource,
}

impl RunMode {
    fn parse(value: Option<std::ffi::OsString>) -> Self {
        match value.as_deref().and_then(std::ffi::OsStr::to_str) {
            None | Some("qualify") => Self::Qualify,
            Some("crash-after-send") => Self::CrashAfterSend,
            Some("recover-after-crash") => Self::RecoverAfterCrash,
            Some("reject-replaced-source") => Self::RejectReplacedSource,
            Some(_) => panic!("unsupported qualification mode"),
        }
    }
}

#[derive(Clone)]
struct ExistingSource {
    binding: SourceBinding,
    ownership_mode: String,
    through: u64,
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
    health_control: Option<HustSourceControl>,
}
impl Session {
    fn start(
        worker: Worker,
        cache: Arc<InProcessCache>,
        identity: String,
        incarnation: u64,
        existing: Option<ExistingSource>,
    ) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let errors = Arc::new(Mutex::new(Vec::new()));
        let (ready_tx, ready_rx) = mpsc::sync_channel(1);
        let task_stop = stop.clone();
        let task_errors = errors.clone();
        let thread = thread::spawn(move || {
            let result = (|| {
                let (mut control, recovering) = if let Some(existing) = existing {
                    (
                        HustSourceControl::connect_existing(
                            &worker.origin,
                            worker.token,
                            &worker.worker_id,
                            worker.frontend_pid,
                            &existing.binding.instance_epoch,
                            &existing.binding.observer_epoch,
                            &existing.ownership_mode,
                            existing.through,
                        )?,
                        true,
                    )
                } else {
                    (
                        HustSourceControl::connect_cold(
                            &worker.origin,
                            worker.token,
                            &worker.worker_id,
                            worker.frontend_pid,
                        )?,
                        false,
                    )
                };
                let source = control.identity().clone();
                let health_control = control.fork_for_health_at(worker.health_origin.as_deref())?;
                let spec = PartitionSpec {
                    binding: Binding {
                        worker: Arc::from(worker.origin.clone()),
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
                if recovering {
                    // Native HUST replay END has no authoritative current
                    // sequence. A zero-event response therefore cannot prove
                    // that a warm directory is complete. Recover terminal
                    // identity/liveness for M3, but deliberately leave M2
                    // unknown until a future snapshot-capable protocol exists.
                    control.verify(&spec)?;
                    ready_tx
                        .send(Ok((spec.clone(), source, health_control)))
                        .unwrap();
                    let mut refresh = Instant::now() + Duration::from_millis(500);
                    while !task_stop.load(Ordering::Acquire) {
                        if Instant::now() >= refresh {
                            control.verify(&spec)?;
                            refresh = Instant::now() + Duration::from_millis(500);
                        }
                        thread::sleep(Duration::from_millis(25));
                    }
                    return Ok::<_, native_kv_owner::OwnerFault>(());
                }
                let transport = ZmqNativeTransport::connect(
                    Arc::new(zmq::Context::new()),
                    &worker.live_endpoint,
                    &worker.replay_endpoint,
                    128,
                    Duration::from_secs(2),
                )?;
                let limits = Limits {
                    records: 512,
                    operations: 512,
                    queued_batches: 16,
                    queue_bytes: 65536,
                    total_bytes: 262144,
                    query_blocks: 64,
                    parent_depth: 64,
                };
                let mut owner = NativeKvOwner::bootstrap(
                    cache,
                    spec.clone(),
                    None,
                    limits,
                    10_000_000_000,
                    128,
                    transport,
                    control,
                )?;
                ready_tx.send(Ok((spec, source, health_control))).unwrap();
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
        let (spec, identity, health_control) = ready_rx
            .recv_timeout(Duration::from_secs(15))
            .expect("KV startup timed out")
            .unwrap_or_else(|error| panic!("KV startup failed: {error:?}"));
        Self {
            spec,
            identity,
            stop,
            thread: Some(thread),
            errors,
            health_control: Some(health_control),
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

struct ControlSession {
    gate: Arc<WorkerGate>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
    pause: Arc<AtomicBool>,
}
impl ControlSession {
    fn start(session: &mut Session) -> Self {
        let mut control = session
            .health_control
            .take()
            .expect("health client consumed once");
        let spec = session.spec.clone();
        let key = SourceKey {
            instance: session.identity.binding.instance_epoch.clone(),
            observer: session.identity.binding.observer_epoch.clone(),
            ownership: session.identity.ownership_mode.clone(),
        };
        let gate = Arc::new(
            WorkerGate::new(
                key.clone(),
                Arc::new(runtime_owner::control_gate::MonotonicClock::default()),
                3_000_000_000,
            )
            .unwrap(),
        );
        let stop = Arc::new(AtomicBool::new(false));
        let task_stop = stop.clone();
        let task_gate = gate.clone();
        let pause = Arc::new(AtomicBool::new(false));
        let task_pause = pause.clone();
        let (ready_tx, ready_rx) = mpsc::sync_channel(1);
        let thread = thread::spawn(move || {
            let mut first = true;
            while !task_stop.load(Ordering::Acquire) {
                if task_pause.load(Ordering::Acquire) {
                    thread::sleep(Duration::from_millis(25));
                    continue;
                }
                let Ok(probe) = task_gate.begin() else {
                    break;
                };
                let result = match control.refresh_health(&spec) {
                    Ok(()) => task_gate.complete(probe, &key),
                    Err(HealthFault::Unavailable) => {
                        let _ = task_gate.reject(probe);
                        Err("control_unavailable")
                    }
                    Err(HealthFault::IdentityChanged) => {
                        let _ = task_gate.quarantine();
                        Err("control_identity_changed")
                    }
                };
                if first {
                    let _ = ready_tx.send(result);
                    first = false;
                }
                for _ in 0..20 {
                    if task_stop.load(Ordering::Acquire) {
                        break;
                    }
                    thread::sleep(Duration::from_millis(25));
                }
            }
            let _ = task_gate.drain();
        });
        ready_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("control startup timed out")
            .expect("initial control verification failed");
        Self {
            gate,
            stop,
            thread: Some(thread),
            pause,
        }
    }
}
impl Drop for ControlSession {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        let _ = self.gate.drain();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
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

struct RetirementOutcome {
    reply: RetirementReply,
    response_loss_retried: bool,
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
        .header("X-Prefix-Routing-Control", &worker.token)
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

fn terminal_identity(worker: Worker) -> SourceIdentity {
    let client = reqwest::blocking::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap();
    let response = client
        .get(format!("{}/prefix-routing/identity", worker.origin))
        .header("X-Prefix-Routing-Control", &worker.token)
        .send()
        .expect("terminal identity unavailable");
    assert_eq!(response.status().as_u16(), 200);
    let mut bytes = Vec::new();
    response.take(4097).read_to_end(&mut bytes).unwrap();
    assert!(bytes.len() <= 4096);
    let identity: SourceIdentity =
        serde_json::from_slice(&bytes).expect("terminal identity malformed");
    assert!(identity.valid());
    identity
}

async fn set_health_fault(client: &reqwest::Client, worker: &Worker, mode: &str) {
    client
        .post(format!(
            "{}/__fault/{mode}",
            worker.health_origin.as_ref().unwrap()
        ))
        .header("X-Prefix-Routing-Control", &worker.token)
        .body(Vec::new())
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap();
}

async fn wait_health(session: &ControlSession, epoch: &str, ready: bool) {
    let deadline = Instant::now() + Duration::from_secs(6);
    while session.gate.revision(epoch).is_some() != ready {
        assert!(
            Instant::now() < deadline,
            "control gate did not reach expected state"
        );
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
}

async fn control_request(
    client: &reqwest::Client,
    origin: &str,
    config: &Config,
    prompt: &str,
    owner: &RuntimeOwner,
    expected: usize,
) -> String {
    let before = owner.checkpoint().unwrap().active;
    let response = client.post(format!("{origin}/v1/completions"))
        .json(&serde_json::json!({"model": config.model, "prompt": prompt, "max_tokens": 1, "temperature": 0}))
        .send().await.unwrap();
    assert_eq!(response.status().as_u16(), 200);
    let _: serde_json::Value = response.json().await.unwrap();
    let after = owner.checkpoint().unwrap().active;
    let added: Vec<_> = after
        .iter()
        .filter(|attempt| {
            !before
                .iter()
                .any(|old| old.attempt_id == attempt.attempt_id)
        })
        .collect();
    assert_eq!(added.len(), 1);
    assert_eq!(added[0].worker_url, config.workers[expected].origin);
    added[0].attempt_id.clone()
}

fn assert_old_cursor_rejected(worker: Worker, source: SourceIdentity, after: u64) {
    assert!(after < source.retired_through);
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
        .append_pair("acknowledge", "0");
    let response = client
        .get(url)
        .header("X-Prefix-Routing-Control", worker.token)
        .send()
        .expect("terminal source unavailable");
    // A cursor below the retired waterline is no longer recoverable. HUST
    // exposes this as source-unavailable (503), not a caller conflict (409).
    assert_eq!(response.status().as_u16(), 503);
}

fn rss_kib(pid: u32) -> u64 {
    let path = format!("/proc/{pid}/status");
    let bytes = std::fs::read(&path).unwrap();
    assert!(bytes.len() <= 1024 * 1024);
    let status = std::str::from_utf8(&bytes).unwrap();
    let line = status
        .lines()
        .find(|line| line.starts_with("VmRSS:"))
        .expect("process RSS unavailable");
    let fields: Vec<_> = line.split_ascii_whitespace().collect();
    assert_eq!(fields.len(), 3);
    assert_eq!(fields[0], "VmRSS:");
    assert_eq!(fields[2], "kB");
    fields[1].parse().unwrap()
}

fn store_bytes(path: &std::path::Path) -> u64 {
    [
        path.to_path_buf(),
        PathBuf::from(format!("{}-wal", path.display())),
        PathBuf::from(format!("{}-shm", path.display())),
    ]
    .iter()
    .map(|item| {
        std::fs::metadata(item)
            .map(|value| value.len())
            .unwrap_or(0)
    })
    .sum()
}

fn terminal_retire_once(
    worker: &Worker,
    source: &SourceIdentity,
    through: u64,
) -> Result<RetirementReply, &'static str> {
    assert_eq!(source.ownership_mode, "exclusive_router_v1");
    let client = reqwest::blocking::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap();
    let origin = worker
        .retirement_origin
        .as_deref()
        .unwrap_or(worker.origin.as_str());
    let mut url = reqwest::Url::parse(&format!("{origin}/prefix-routing/terminal/retire"))
        .map_err(|_| "retirement_origin_invalid")?;
    url.query_pairs_mut()
        .append_pair("observer_epoch", &source.binding.observer_epoch)
        .append_pair("through", &through.to_string())
        .append_pair("checkpointed_through", &through.to_string());
    let response = client
        .post(url)
        .header("X-Prefix-Routing-Control", &worker.token)
        .json(&serde_json::json!({}))
        .send()
        .map_err(|_| "retirement_response_lost")?;
    if response.status().as_u16() != 200 {
        return Err("retirement_status_invalid");
    }
    let mut bytes = Vec::new();
    response
        .take(4097)
        .read_to_end(&mut bytes)
        .map_err(|_| "retirement_response_lost")?;
    if bytes.len() > 4096 {
        return Err("retirement_reply_oversized");
    }
    let reply: RetirementReply =
        serde_json::from_slice(&bytes).map_err(|_| "retirement_reply_malformed")?;
    if reply.schema_version != 1
        || reply.binding != source.binding
        || reply.retired_through != through
        || reply.family_count > 64
        || reply.family_count > reply.reserved_children
        || reply.reserved_children > 256
    {
        return Err("retirement_reply_invalid");
    }
    Ok(reply)
}

fn terminal_retire(worker: Worker, source: SourceIdentity, through: u64) -> RetirementOutcome {
    match terminal_retire_once(&worker, &source, through) {
        Ok(reply) => RetirementOutcome {
            reply,
            response_loss_retried: false,
        },
        Err("retirement_response_lost") => RetirementOutcome {
            reply: terminal_retire_once(&worker, &source, through)
                .expect("terminal retirement retry failed"),
            response_loss_retried: true,
        },
        Err(error) => panic!("terminal retirement rejected: {error}"),
    }
}

async fn reconcile(
    config: &Config,
    sessions: &[Session],
    cursors: &mut [u64],
    owner: &Arc<RuntimeOwner>,
    durable: &DurableOption,
    retired_families: &mut usize,
    retire_response_losses: &mut usize,
) -> BTreeMap<String, usize> {
    if config.use_terminal_driver {
        #[cfg(feature = "durable-candidate")]
        return reconcile_driver(config, sessions, cursors, durable, retired_families).await;
        #[cfg(not(feature = "durable-candidate"))]
        panic!("terminal driver requires durable candidate");
    }
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
                        let outcome = tokio::task::spawn_blocking(move || {
                            terminal_retire(worker, source, through)
                        })
                        .await
                        .unwrap();
                        if outcome.response_loss_retried {
                            assert_eq!(outcome.reply.retired, 0);
                            *retire_response_losses = retire_response_losses
                                .checked_add(1)
                                .expect("retirement response-loss count overflow");
                        } else {
                            assert_eq!(outcome.reply.retired, reply.records.len());
                        }
                        *retired_families = retired_families
                            .checked_add(reply.records.len())
                            .expect("retirement count overflow");

                        // A lost successful response is safe to retry at the same durable waterline.
                        let worker = config.workers[index].clone();
                        let source = sessions[index].identity.clone();
                        let repeated = tokio::task::spawn_blocking(move || {
                            terminal_retire(worker, source, through)
                        })
                        .await
                        .unwrap();
                        assert!(!repeated.response_loss_retried);
                        assert_eq!(repeated.reply.retired, 0);
                        assert_eq!(
                            repeated.reply,
                            RetirementReply {
                                retired: 0,
                                ..outcome.reply
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

#[cfg(feature = "durable-candidate")]
fn make_terminal_drivers(
    config: &Config,
    sessions: &[Session],
    durable: &DurableOption,
) -> Vec<terminal_driver::TerminalDriver<hust_terminal_transport::HustTerminalTransport>> {
    use hust_terminal_transport::HustTerminalTransport;
    use terminal_driver::{Ownership, TerminalDriver};
    let runtime = durable
        .as_ref()
        .expect("terminal driver requires durable runtime");
    assert_eq!(config.terminal_ownership_mode, "exclusive_router_v1");
    assert_eq!(config.expected_retire_response_losses, 0);
    config
        .workers
        .iter()
        .zip(sessions)
        .map(|(worker, session)| {
            assert!(worker.retirement_origin.is_none());
            let source = &session.identity;
            assert_eq!(source.binding.worker_id, worker.worker_id);
            let cursor = runtime.terminal_cursor(&worker.worker_id).unwrap().unwrap();
            assert_eq!(cursor.worker_url, worker.origin);
            assert_eq!(cursor.instance_epoch, source.binding.instance_epoch);
            assert_eq!(cursor.observer_epoch, source.binding.observer_epoch);
            let transport = HustTerminalTransport::new(
                cursor.clone(),
                worker.token.clone(),
                source.frontend_pid,
                source.core_pid,
                Ownership::ExclusiveRouterV1,
                Duration::from_secs(2),
            )
            .unwrap();
            TerminalDriver::new(
                runtime.clone(),
                transport,
                cursor,
                Ownership::ExclusiveRouterV1,
            )
            .unwrap()
        })
        .collect()
}

#[cfg(feature = "durable-candidate")]
async fn reconcile_driver(
    config: &Config,
    sessions: &[Session],
    cursors: &mut [u64],
    durable: &DurableOption,
    retired_families: &mut usize,
) -> BTreeMap<String, usize> {
    use terminal_driver::{PumpError, Step};
    assert!(
        !config.background_terminal_driver,
        "one consumer per source"
    );
    let runtime = durable.as_ref().unwrap();
    let mut drivers = make_terminal_drivers(config, sessions, durable);
    for (index, driver) in drivers.iter().enumerate() {
        assert_eq!(driver.cursor().through, cursors[index]);
    }
    let deadline = Instant::now() + Duration::from_secs(15);
    let mut statuses = BTreeMap::new();
    loop {
        for (index, driver) in drivers.iter_mut().enumerate() {
            let before = driver.cursor().through;
            match driver.step().await {
                Ok(Step::Committed(_)) | Ok(Step::Idle) | Err(PumpError::Retry) => {}
                Err(error) => panic!("terminal driver failed: {error:?}"),
            }
            // A successful local commit may be followed by a lost ACK/retire
            // response and return Retry. Count that cursor advance only once;
            // the loop still waits for pending confirmation before returning.
            let advanced = driver.cursor().through.checked_sub(before).unwrap();
            if advanced != 0 {
                *retired_families = retired_families
                    .checked_add(advanced.try_into().unwrap())
                    .unwrap();
                for (status, count) in driver.transport().last_poll_statuses() {
                    *statuses.entry(status.clone()).or_insert(0) += count;
                }
            }
            cursors[index] = driver.cursor().through;
        }
        if runtime.owner().active_len().unwrap() == 0
            && drivers
                .iter()
                .all(|driver| !driver.pending_remote_confirmation())
        {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "terminal driver finite reconciliation timed out"
        );
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    statuses
}

#[cfg(feature = "durable-candidate")]
type BackgroundTasks =
    Vec<terminal_driver::TerminalTask<hust_terminal_transport::HustTerminalTransport>>;

/// Qualification waits only on task progress and the original owner. No poll,
/// ACK or retire occurs in this HTTP-side waiter.
#[cfg(feature = "durable-candidate")]
async fn wait_background(tasks: &BackgroundTasks, owner: &RuntimeOwner, expected: &[u64]) {
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        assert!(!owner.is_failed(), "background terminal task failed");
        let progress: Vec<_> = tasks.iter().map(|task| task.progress()).collect();
        for (state, through) in progress.iter().zip(expected) {
            assert!(state.through <= *through, "unexpected source waterline");
        }
        if owner.active_len().unwrap() == 0
            && progress
                .iter()
                .zip(expected)
                .all(|(state, through)| state.settled() && state.through == *through)
        {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "background terminal drain timed out"
        );
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}

#[cfg(feature = "durable-candidate")]
async fn qualify_background(
    config: &Config,
    sessions: &[Session],
    durable: &DurableOption,
    controls: &ControlSet,
    origin: &str,
    client: &reqwest::Client,
    prompts: &[String; 2],
) -> serde_json::Value {
    use terminal_driver::{StopReason, TerminalTask};
    let runtime = durable.as_ref().unwrap();
    let owner = runtime.owner();
    // Each source is moved into exactly one task before the first request. The
    // finite reconciler is never called while these tasks exist.
    let tasks: BackgroundTasks = make_terminal_drivers(config, sessions, durable)
        .into_iter()
        .map(|driver| TerminalTask::spawn(driver, Duration::from_millis(25)).unwrap())
        .collect();
    let mut expected = vec![0; tasks.len()];
    wait_background(&tasks, owner, &expected).await;
    assert!(tasks.iter().all(|task| task
        .progress()
        .ready_at(Instant::now(), Duration::from_secs(5))));
    let mut results = Vec::new();
    for (prompt, target, cached) in [(0, 0, 0), (0, 0, 384), (1, 1, 384)] {
        let response = client.post(format!("{origin}/v1/completions"))
            .json(&serde_json::json!({"model": config.model, "prompt": prompts[prompt], "max_tokens": 16, "temperature": 0}))
            .send().await.unwrap().error_for_status().unwrap();
        let reply: serde_json::Value = response.json().await.unwrap();
        assert_eq!(
            reply["usage"]["prompt_tokens_details"]["cached_tokens"],
            cached
        );
        expected[target] += 1;
        wait_background(&tasks, owner, &expected).await;
        // Actual per-source committed cursor, not a prediction from M4.
        results.push(serde_json::json!({"worker": target, "cached_tokens": cached}));
    }
    let response = client.post(format!("{origin}/v1/completions"))
        .json(&serde_json::json!({"model": config.model, "prompt": prompts[0], "max_tokens": 256, "ignore_eos": true, "temperature": 0, "stream": true}))
        .send().await.unwrap().error_for_status().unwrap();
    let mut stream = response.bytes_stream();
    wait_generated(&mut stream).await;
    // Closing admission does not stop reconciliation or falsely end the stream.
    controls.drain().unwrap();
    let rejected = client
        .post(format!("{origin}/v1/completions"))
        .json(&serde_json::json!({"model": config.model, "prompt": prompts[0], "max_tokens": 1}))
        .send()
        .await
        .unwrap();
    assert_eq!(rejected.status().as_u16(), 503);
    drop(stream);
    expected[0] += 1;
    wait_background(&tasks, owner, &expected).await;
    // Stop ALL sources before joining the first: each task's exit fence closes
    // the shared owner. No task is aborted during a commit/ACK/retire.
    for task in &tasks {
        task.request_stop();
    }
    let mut statuses = BTreeMap::<String, usize>::new();
    for (index, task) in tasks.into_iter().enumerate() {
        let (result, driver) = task.join().await.unwrap();
        assert_eq!(result, Ok(StopReason::Requested));
        assert!(!driver.pending_remote_confirmation());
        assert_eq!(driver.cursor().through, expected[index]);
        for (status, count) in driver.transport().committed_statuses() {
            *statuses.entry(status.clone()).or_default() += count;
        }
    }
    assert_eq!(statuses.get("FINISHED_ABORTED"), Some(&1));
    assert_eq!(owner.active_len().unwrap(), 0);
    let store = Arc::new(
        SqliteRowRuntimeStore::open(
            config.durable_store.as_ref().unwrap(),
            DEFAULT_MAX_SNAPSHOT_BYTES,
        )
        .unwrap(),
    );
    let stored = store.load().unwrap();
    assert!(stored.checkpoint.active.is_empty());
    for (index, worker) in config.workers.iter().enumerate() {
        assert_eq!(
            stored
                .terminal_cursors
                .iter()
                .find(|cursor| cursor.source_id == worker.worker_id)
                .unwrap()
                .through,
            expected[index]
        );
    }
    let recovered = DurableRuntime::recover(
        Arc::new(UnknownCache),
        uuid::Uuid::new_v4().simple().to_string(),
        store,
        16,
        16,
    )
    .unwrap();
    assert!(!recovered.admission_ready());
    serde_json::json!({
        "status": "pass", "scope": "finite-public-http-real-M1-M2-M3-M4-M5-M7",
        "requests": results, "cancel_core_statuses": statuses, "active_after_terminal": 0,
        "real_native_subscription": true, "durable_commit_ack_and_paused_reopen_verified": true,
        "managed_terminal_retirement_verified": true, "terminal_driver_transport_verified": true,
        "background_terminal_driver_verified": true, "startup_confirmation_verified": true,
        "admission_drain_rejected": true, "background_tasks_joined": true,
        "retire_response_loss_recovered": true, "retire_response_losses": 0,
        "retired_families": expected.iter().sum::<u64>(), "churn_families": 0, "churn_children": 0,
        "durable_recovery_verified": false, "production_lifecycle_verified": false,
        "performance_verified": false
    })
}

#[cfg(feature = "durable-candidate")]
#[derive(Debug)]
struct UnknownCache;
#[cfg(feature = "durable-candidate")]
impl CachePort for UnknownCache {
    fn lookup(&self, _: &str, _: &str, _: &str, _: &[String]) -> Option<u32> {
        None
    }
}

async fn wait_generated(
    stream: &mut (impl futures_util::Stream<Item = Result<bytes::Bytes, reqwest::Error>> + Unpin),
) {
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
            let line: Vec<_> = pending.drain(..=boundary).collect();
            if let Some(data) = std::str::from_utf8(&line)
                .unwrap()
                .trim()
                .strip_prefix("data: ")
            {
                if data != "[DONE]" {
                    let value: serde_json::Value = serde_json::from_str(data).unwrap();
                    if value["choices"][0]["text"]
                        .as_str()
                        .is_some_and(|text| !text.is_empty())
                    {
                        return;
                    }
                }
            }
        }
    }
    panic!("cancel must follow an actual generated output");
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
    let mode = RunMode::parse(std::env::args_os().nth(2));
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
    assert!(config.expected_retire_response_losses <= config.workers.len());
    assert!(!config.background_terminal_driver || config.use_terminal_driver);
    assert!(
        !config.use_terminal_driver
            || (config.durable_store.is_some()
                && config.terminal_ownership_mode == "exclusive_router_v1"
                && config.expected_retire_response_losses == 0
                && config.churn_families == 0
                && !config.exercise_control_gate
                && mode == RunMode::Qualify),
        "driver transport qualification requires isolated managed durable mode"
    );
    assert!(
        config.expected_retire_response_losses == 0
            || config.terminal_ownership_mode == "exclusive_router_v1"
    );
    assert!(
        config.churn_families == 0
            || (config.terminal_ownership_mode == "exclusive_router_v1"
                && config.churn_families > 512
                && config.churn_families <= 1024
                && (1..=8).contains(&config.churn_children_per_family))
    );
    assert!(
        mode == RunMode::Qualify
            || (config.terminal_ownership_mode == "exclusive_router_v1"
                && config.durable_store.is_some()
                && config.churn_families == 0),
        "crash recovery qualification requires managed durable mode without churn"
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
    #[cfg(feature = "durable-candidate")]
    let existing_sources: BTreeMap<String, ExistingSource> = if matches!(
        mode,
        RunMode::RecoverAfterCrash | RunMode::RejectReplacedSource
    ) {
        let store_path = config
            .durable_store
            .as_ref()
            .expect("durable store required");
        let stored = SqliteRowRuntimeStore::open(store_path, DEFAULT_MAX_SNAPSHOT_BYTES)
            .unwrap()
            .load()
            .unwrap();
        stored
            .terminal_cursors
            .into_iter()
            .map(|cursor| {
                (
                    cursor.source_id.clone(),
                    ExistingSource {
                        binding: SourceBinding {
                            worker_id: cursor.source_id,
                            instance_epoch: cursor.instance_epoch,
                            observer_epoch: cursor.observer_epoch,
                        },
                        ownership_mode: config.terminal_ownership_mode.clone(),
                        through: cursor.through,
                    },
                )
            })
            .collect()
    } else {
        BTreeMap::new()
    };
    #[cfg(not(feature = "durable-candidate"))]
    let existing_sources = BTreeMap::<String, ExistingSource>::new();

    if mode == RunMode::RejectReplacedSource {
        assert_eq!(existing_sources.len(), config.workers.len());
        let worker = &config.workers[0];
        let expected = existing_sources.get(&worker.worker_id).unwrap();
        assert!(HustSourceControl::connect_existing(
            &worker.origin,
            worker.token.clone(),
            &worker.worker_id,
            worker.frontend_pid,
            &expected.binding.instance_epoch,
            &expected.binding.observer_epoch,
            &expected.ownership_mode,
            expected.through,
        )
        .is_err());
        let actual = terminal_identity(worker.clone());
        assert_ne!(actual.binding, expected.binding);
        println!(
            "{}",
            serde_json::json!({
                "status": "pass",
                "scope": "replaced-terminal-source-rejected",
                "source_id": worker.worker_id,
                "stored_instance_epoch": expected.binding.instance_epoch,
                "actual_instance_epoch": actual.binding.instance_epoch,
                "durable_recovery_verified": false
            })
        );
        return;
    }

    let mut sessions = Vec::new();
    for (index, worker) in config.workers.iter().cloned().enumerate() {
        let cache = cache.clone();
        let identity = config.identity.clone();
        let existing = existing_sources.get(&worker.worker_id).cloned();
        sessions.push(
            tokio::task::spawn_blocking(move || {
                Session::start(worker, cache, identity, index as u64 + 1, existing)
            })
            .await
            .unwrap(),
        );
    }
    assert!(sessions
        .iter()
        .all(|session| session.identity.ownership_mode == config.terminal_ownership_mode));
    let control_sessions: Vec<_> = sessions.iter_mut().map(ControlSession::start).collect();
    let control_set = Arc::new(ControlSet(
        config
            .workers
            .iter()
            .zip(&control_sessions)
            .map(|(worker, session)| (worker.origin.clone(), session.gate.clone()))
            .collect(),
    ));
    let controls: Vec<_> = sessions
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
        .collect();
    let fresh_owner = || {
        Arc::new(
            RuntimeOwner::new(
                cache.clone(),
                uuid::Uuid::new_v4().simple().to_string(),
                1,
                controls.clone(),
                16,
                16,
            )
            .unwrap(),
        )
    };
    #[cfg(feature = "durable-candidate")]
    let (owner, durable) = if mode == RunMode::RecoverAfterCrash {
        let path = config.durable_store.as_ref().unwrap();
        let runtime = Arc::new(
            DurableRuntime::recover(
                cache.clone(),
                uuid::Uuid::new_v4().simple().to_string(),
                Arc::new(SqliteRowRuntimeStore::open(path, DEFAULT_MAX_SNAPSHOT_BYTES).unwrap()),
                16,
                16,
            )
            .unwrap(),
        );
        (runtime.owner().clone(), Some(runtime))
    } else {
        let owner = fresh_owner();
        let durable = config.durable_store.as_ref().map(|path| {
            assert!(!path.exists(), "candidate store must be newly created");
            Arc::new(
                DurableRuntime::create_with_cursors(
                    owner.clone(),
                    Arc::new(
                        SqliteRowRuntimeStore::create(path, DEFAULT_MAX_SNAPSHOT_BYTES).unwrap(),
                    ),
                    config
                        .workers
                        .iter()
                        .zip(&sessions)
                        .map(|(worker, session)| TerminalCursor {
                            source_id: worker.worker_id.clone(),
                            worker_url: worker.origin.clone(),
                            instance_epoch: session.identity.binding.instance_epoch.clone(),
                            observer_epoch: session.identity.binding.observer_epoch.clone(),
                            through: 0,
                        })
                        .collect(),
                )
                .unwrap(),
            )
        });
        (owner, durable)
    };
    #[cfg(not(feature = "durable-candidate"))]
    let (owner, durable) = {
        assert!(
            config.durable_store.is_none(),
            "binary lacks durable candidate feature"
        );
        assert_eq!(mode, RunMode::Qualify);
        (fresh_owner(), None::<()>)
    };
    owner.install_controls(control_set.clone()).unwrap();
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
    let mut cursors: Vec<u64> = config
        .workers
        .iter()
        .map(|worker| {
            existing_sources
                .get(&worker.worker_id)
                .map(|source| source.through)
                .unwrap_or(0)
        })
        .collect();
    let mut retired_families = 0_usize;
    let mut retire_response_losses = 0_usize;

    if mode == RunMode::CrashAfterSend {
        let response = client
            .post(format!("{origin}/v1/completions"))
            .json(&serde_json::json!({
                "model": config.model,
                "prompt": prompts[0],
                "max_tokens": 8,
                "temperature": 0
            }))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status().as_u16(), 200);
        assert_eq!(owner.active_len().unwrap(), 1);
        #[cfg(feature = "durable-candidate")]
        {
            let stored = SqliteRowRuntimeStore::open(
                config.durable_store.as_ref().unwrap(),
                DEFAULT_MAX_SNAPSHOT_BYTES,
            )
            .unwrap()
            .load()
            .unwrap();
            assert_eq!(stored.checkpoint.active.len(), 1);
            assert_eq!(stored.terminal_cursors.len(), 2);
            assert!(stored
                .terminal_cursors
                .iter()
                .all(|cursor| cursor.through == 0));
        }
        eprintln!("intentional_router_crash_after_durable_send");
        std::process::exit(86);
    }

    if mode == RunMode::RecoverAfterCrash {
        for worker in 0..config.workers.len() {
            for prompt in 0..queries.len() {
                assert_eq!(
                    lookup(worker, prompt),
                    None,
                    "warm native directory must remain unknown after Router recovery"
                );
            }
        }
        let active_before = owner.active_len().unwrap();
        assert_eq!(active_before, 1);
        let paused = client
            .post(format!("{origin}/v1/completions"))
            .json(&serde_json::json!({
                "model": config.model,
                "prompt": prompts[1],
                "max_tokens": 1,
                "temperature": 0
            }))
            .send()
            .await
            .unwrap();
        assert!(!paused.status().is_success());
        assert_eq!(owner.active_len().unwrap(), active_before);
        let runtime = durable.as_ref().expect("recovery runtime required");
        assert!(!runtime.admission_ready());
        runtime.resume(2, controls.clone(), true).await.unwrap();
        assert!(runtime.admission_ready());

        reconcile(
            &config,
            &sessions,
            &mut cursors,
            &owner,
            &durable,
            &mut retired_families,
            &mut retire_response_losses,
        )
        .await;
        assert_eq!(owner.active_len().unwrap(), 0);
        let retired_index = cursors
            .iter()
            .position(|cursor| *cursor > 0)
            .expect("recovered request terminal was not observed");
        let worker = config.workers[retired_index].clone();
        let source = terminal_identity(worker.clone());
        assert_eq!(source.retired_through, cursors[retired_index]);
        let old_after = source.retired_through.checked_sub(1).unwrap();
        tokio::task::spawn_blocking(move || assert_old_cursor_rejected(worker, source, old_after))
            .await
            .unwrap();

        let stale = existing_sources.get(&config.workers[0].worker_id).unwrap();
        assert!(HustSourceControl::connect_existing(
            &config.workers[0].origin,
            config.workers[0].token.clone(),
            &config.workers[0].worker_id,
            config.workers[0].frontend_pid,
            "replacement-instance-must-not-match",
            &stale.binding.observer_epoch,
            &stale.ownership_mode,
            stale.through,
        )
        .is_err());

        let response = client
            .post(format!("{origin}/v1/completions"))
            .json(&serde_json::json!({
                "model": config.model,
                "prompt": prompts[1],
                "max_tokens": 8,
                "temperature": 0
            }))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status().as_u16(), 200);
        assert_eq!(owner.active_len().unwrap(), 1);
        reconcile(
            &config,
            &sessions,
            &mut cursors,
            &owner,
            &durable,
            &mut retired_families,
            &mut retire_response_losses,
        )
        .await;
        assert_eq!(owner.active_len().unwrap(), 0);

        shutdown_tx.send(()).unwrap();
        http.await.unwrap();
        for session in &mut sessions {
            session.stop();
        }
        println!(
            "{}",
            serde_json::json!({
                "status": "pass",
                "scope": "router-crash-paused-recovery",
                "paused_admission_rejected": true,
                "old_cursor_rejected": true,
                "synthetic_replacement_binding_rejected": true,
                "recovered_debt": active_before,
                "post_resume_request_completed": true,
                "retired_families": retired_families,
                "terminal_cursors": cursors,
                "m2_recovery_mode": "unknown_until_fresh_source",
                "durable_recovery_verified": true,
                "actual_source_replacement_verified": false,
                "performance_verified": false
            })
        );
        return;
    }
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
    #[cfg(feature = "durable-candidate")]
    if config.background_terminal_driver {
        let report = qualify_background(
            &config,
            &sessions,
            &durable,
            &control_set,
            &origin,
            &client,
            &prompts,
        )
        .await;
        shutdown_tx.send(()).unwrap();
        http.await.unwrap();
        for session in &mut sessions {
            session.stop();
        }
        println!("{report}");
        return;
    }
    let mut resource_samples = Vec::new();
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
            &mut retire_response_losses,
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
    wait_generated(&mut stream).await;
    drop(stream);
    assert_eq!(owner.active_len().unwrap(), 1);
    let cancellation = reconcile(
        &config,
        &sessions,
        &mut cursors,
        &owner,
        &durable,
        &mut retired_families,
        &mut retire_response_losses,
    )
    .await;
    assert_eq!(cancellation.get("FINISHED_ABORTED"), Some(&1));
    let mut control_qualification = serde_json::Value::Null;
    if config.exercise_control_gate {
        assert!(config.churn_families == 0 && config.durable_store.is_some());
        let epoch = &sessions[0].identity.binding.instance_epoch;
        let debt = control_request(&client, &origin, &config, &prompts[0], &owner, 0).await;
        set_health_fault(&client, &config.workers[0], "fail").await;
        wait_health(&control_sessions[0], epoch, false).await;
        control_request(&client, &origin, &config, &prompts[0], &owner, 1).await;
        assert!(owner
            .checkpoint()
            .unwrap()
            .active
            .iter()
            .any(|attempt| attempt.attempt_id == debt));
        assert_eq!(owner.active_len().unwrap(), 2);
        reconcile(
            &config,
            &sessions,
            &mut cursors,
            &owner,
            &durable,
            &mut retired_families,
            &mut retire_response_losses,
        )
        .await;
        assert_eq!(owner.active_len().unwrap(), 0);
        set_health_fault(&client, &config.workers[0], "pass").await;
        wait_health(&control_sessions[0], epoch, true).await;
        control_request(&client, &origin, &config, &prompts[0], &owner, 0).await;
        reconcile(
            &config,
            &sessions,
            &mut cursors,
            &owner,
            &durable,
            &mut retired_families,
            &mut retire_response_losses,
        )
        .await;
        control_sessions[0].pause.store(true, Ordering::Release);
        wait_health(&control_sessions[0], epoch, false).await;
        control_request(&client, &origin, &config, &prompts[0], &owner, 1).await;
        reconcile(
            &config,
            &sessions,
            &mut cursors,
            &owner,
            &durable,
            &mut retired_families,
            &mut retire_response_losses,
        )
        .await;
        control_sessions[0].pause.store(false, Ordering::Release);
        wait_health(&control_sessions[0], epoch, true).await;
        set_health_fault(&client, &config.workers[0], "identity").await;
        wait_health(&control_sessions[0], epoch, false).await;
        set_health_fault(&client, &config.workers[0], "pass").await;
        tokio::time::sleep(Duration::from_millis(700)).await;
        assert_eq!(control_sessions[0].gate.revision(epoch), None);
        control_request(&client, &origin, &config, &prompts[0], &owner, 1).await;
        reconcile(
            &config,
            &sessions,
            &mut cursors,
            &owner,
            &durable,
            &mut retired_families,
            &mut retire_response_losses,
        )
        .await;
        assert_eq!(owner.active_len().unwrap(), 0);
        control_set.drain().unwrap();
        let rejected = client
            .post(format!("{origin}/v1/completions"))
            .json(
                &serde_json::json!({"model": config.model, "prompt": prompts[0], "max_tokens": 1}),
            )
            .send()
            .await
            .unwrap();
        assert!(!rejected.status().is_success());
        assert_eq!(owner.active_len().unwrap(), 0);
        control_qualification = serde_json::json!({"http_control_failure": true, "same_source_revalidated": true, "debt_preserved": true, "health_expiry": true, "identity_quarantined": true, "identity_fault": "injected-observer-field", "other_replica_serves": true, "drain_rejected": true});
    }
    let churn_started = Instant::now();
    for iteration in 1..=config.churn_families {
        let response = client
            .post(format!("{origin}/v1/completions"))
            .json(&serde_json::json!({
                "model": config.model,
                "prompt": "bounded terminal-retirement churn",
                "max_tokens": 1,
                "n": config.churn_children_per_family,
                "temperature": 0.7
            }))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status().as_u16(), 200);
        let reply: serde_json::Value = response.json().await.unwrap();
        assert_eq!(
            reply["choices"].as_array().unwrap().len(),
            config.churn_children_per_family
        );
        assert_eq!(owner.active_len().unwrap(), 1);
        reconcile(
            &config,
            &sessions,
            &mut cursors,
            &owner,
            &durable,
            &mut retired_families,
            &mut retire_response_losses,
        )
        .await;
        assert_eq!(owner.active_len().unwrap(), 0);
        if iteration % 64 == 0 || iteration == config.churn_families {
            let mut sources = Vec::new();
            for (index, worker) in config.workers.iter().cloned().enumerate() {
                let current = tokio::task::spawn_blocking(move || terminal_identity(worker))
                    .await
                    .unwrap();
                assert_eq!(current.binding, sessions[index].identity.binding);
                assert_eq!(current.family_count, 0);
                assert_eq!(current.reserved_children, 0);
                assert_eq!(current.frontend_event_count, 0);
                assert_eq!(current.child_status_count, 0);
                assert_eq!(current.core_retained_records, 0);
                assert_eq!(current.core_duplicate_guards, 0);
                sources.push(serde_json::json!({
                    "worker": current.binding.worker_id,
                    "frontend_rss_kib": rss_kib(current.frontend_pid),
                    "core_rss_kib": rss_kib(current.core_pid),
                    "family_count": current.family_count,
                    "reserved_children": current.reserved_children,
                    "frontend_event_count": current.frontend_event_count,
                    "child_status_count": current.child_status_count,
                    "core_retained_records": current.core_retained_records,
                    "core_duplicate_guards": current.core_duplicate_guards,
                    "retired_through": current.retired_through,
                    "durable_cursor": cursors[index]
                }));
            }
            let checkpoint = owner.checkpoint().unwrap();
            assert!(checkpoint.active.is_empty());
            assert!(checkpoint.retired.len() <= 16);
            resource_samples.push(serde_json::json!({
                "completed_families": iteration,
                "completed_children": iteration * config.churn_children_per_family,
                "elapsed_millis": churn_started.elapsed().as_millis(),
                "router_rss_kib": rss_kib(std::process::id()),
                "retired_tombstones": checkpoint.retired.len(),
                "store_bytes": config.durable_store.as_ref().map(|path| store_bytes(path)).unwrap_or(0),
                "sources": sources
            }));
        }
    }
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
        serde_json::json!({"status": "pass", "scope": "finite-public-http-real-M1-M2-M3-M4-M5-M7", "requests": results, "cancel_core_statuses": cancellation, "active_after_terminal": owner.active_len().unwrap(), "real_native_subscription": true, "durable_commit_ack_and_paused_reopen_verified": durable_verified, "managed_terminal_retirement_verified": config.terminal_ownership_mode == "exclusive_router_v1" && durable_verified && retired_families == 4 + config.churn_families + if config.exercise_control_gate { 5 } else { 0 }, "terminal_driver_transport_verified": config.use_terminal_driver, "control_gate_qualification": control_qualification, "retire_response_loss_recovered": retire_response_losses == config.expected_retire_response_losses, "retire_response_losses": retire_response_losses, "retired_families": retired_families, "churn_families": config.churn_families, "churn_children": config.churn_families * config.churn_children_per_family, "resource_samples": resource_samples, "durable_recovery_verified": false, "production_lifecycle_verified": false, "performance_verified": false})
    );
}
