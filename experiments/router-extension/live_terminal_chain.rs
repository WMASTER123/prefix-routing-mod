// SPDX-License-Identifier: Apache-2.0
//! Finite single-backend integration using the original patched Router library.
//! No tokenizer/cache policy, new proxy, synthetic backend end or performance claim.
use axum::body::to_bytes;
use futures_util::StreamExt;
#[path = "admission_bridge.rs"]
mod admission_bridge;
#[path = "lifecycle_bridge.rs"]
mod lifecycle_bridge;
#[path = "online_bridge.rs"]
mod online_bridge;
use lifecycle_bridge::{LifecycleTrace, RouterClock, TracedLease};
use online_bridge::{Controller, OnlineLease, Outbox};
use std::{
    any::Any,
    io,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use vllm_router_rs::{
    config::types::{PolicyConfig, RouterConfig, RoutingMode},
    protocols::spec::CompletionRequest,
    routers::http::{
        prefix_extension::{self, *},
        router::Router,
    },
    server::AppContext,
};

#[derive(Debug)]
struct Identity {
    id: usize,
}
impl Features for Identity {
    fn as_any(&self) -> &dyn Any {
        self
    }
}

#[derive(Debug)]
struct Provider {
    clock: Arc<RouterClock>,
    admission: admission_bridge::AdmissionClient,
    outbox: Arc<Outbox>,
    next: AtomicUsize,
    namespace: String,
    worker: String,
    instance: String,
}
impl Provider {
    fn attempt(&self, id: usize) -> String {
        format!("prm-{}-{id}", self.namespace)
    }
    fn request(&self, id: usize) -> String {
        format!("request-{}-{id}", self.namespace)
    }
}
#[derive(Debug)]
struct Carrier(String);
impl Lease for Carrier {
    fn request_id_header(&self) -> Option<&str> {
        Some(&self.0)
    }
    fn before_send(&self) -> Result<(), &'static str> {
        Ok(())
    }
    fn sent(&self) {}
    fn headers(&self, _: u16) {}
    fn closed(&self, _: bool, _: CloseEvidence) {}
}
impl HttpExtension for Provider {
    fn prepare(
        &self,
        request: &serde_json::Value,
        route: &str,
    ) -> Result<Arc<dyn Features>, &'static str> {
        if route != "/v1/completions" || request["model"] != "prefix-qwen14b-probe" {
            return Err("controlled_scope");
        }
        let id = self.next.fetch_add(1, Ordering::SeqCst) + 1;
        if id > 16 {
            return Err("experiment_capacity");
        }
        Ok(Arc::new(Identity { id }))
    }
    fn select(
        &self,
        features: &dyn Features,
        available: &[String],
        revision: u64,
    ) -> Result<Option<Proposal>, &'static str> {
        if available.len() != 1 {
            return Err("one_controlled_backend_required");
        }
        let id = features
            .as_any()
            .downcast_ref::<Identity>()
            .ok_or("identity")?
            .id;
        Ok(Some(Proposal {
            worker_url: available[0].clone(),
            registry_revision: revision,
            decision: Arc::new(Identity { id }),
        }))
    }
    fn reserve(&self, _: &dyn Features, _: &Proposal) -> Result<Box<dyn Lease>, &'static str> {
        Err("async_admission_required")
    }
    fn reserve_async<'a>(
        &'a self,
        features: &'a dyn Features,
        proposal: &'a Proposal,
    ) -> std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<Box<dyn Lease>, &'static str>> + Send + 'a>,
    > {
        Box::pin(async move {
            if !self.outbox.admissible() {
                return Err("observation_unavailable");
            }
            let id = features
                .as_any()
                .downcast_ref::<Identity>()
                .ok_or("identity")?
                .id;
            if proposal
                .decision
                .as_any()
                .downcast_ref::<Identity>()
                .ok_or("decision")?
                .id
                != id
            {
                return Err("decision");
            }
            self.admission
                .reserve(self.request(id), self.attempt(id), self.clock.now_ns())
                .await?;
            let trace = Arc::new(Mutex::new(LifecycleTrace::new(
                self.request(id),
                self.attempt(id),
                self.worker.clone(),
                self.instance.clone(),
                self.clock.clone(),
            )?));
            self.outbox.register(trace.clone())?;
            Ok(Box::new(OnlineLease {
                inner: TracedLease {
                    inner: Box::new(Carrier(self.attempt(id))),
                    trace,
                },
                outbox: self.outbox.clone(),
            }) as Box<dyn Lease>)
        })
    }
    fn stream_limits(&self) -> Option<StreamLimits> {
        Some(StreamLimits {
            chunk_bytes: 65536,
            response_bytes: 1_048_576,
            idle: Duration::from_secs(30),
        })
    }
}

async fn route(router: &Router, stream: bool) -> axum::response::Response {
    let request: CompletionRequest = serde_json::from_value(serde_json::json!({
        "model":"prefix-qwen14b-probe", "prompt":"Please count positive integers in order.",
        "max_tokens":if stream {512} else {8}, "ignore_eos":true,
        "temperature":0.7,"n":if stream {1} else {2},"stream":stream
    }))
    .unwrap();
    let mut headers = axum::http::HeaderMap::new();
    for value in ["untrusted-client-A", "untrusted-client-B"] {
        headers.append("x-request-id", axum::http::HeaderValue::from_static(value));
    }
    router
        .route_typed_request(Some(&headers), &request, "/v1/completions", None)
        .await
}

async fn barrier(stage: serde_json::Value) {
    println!("{stage}");
    tokio::task::spawn_blocking(|| {
        let mut line = String::new();
        io::stdin().read_line(&mut line).unwrap();
        assert_eq!(line, "continue\n", "owner acknowledgement required");
    })
    .await
    .unwrap();
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() {
    let worker = std::env::args()
        .nth(1)
        .expect("one controlled backend origin");
    let url = reqwest::Url::parse(&worker).unwrap();
    assert_eq!(url.scheme(), "http");
    assert_eq!(url.host_str(), Some("127.0.0.1"));
    let get = |key| std::env::var(key).expect("explicit experiment binding");
    let clock = Arc::new(RouterClock::new(get("PREFIX_PROBE_CLOCK")).unwrap());
    let outbox = Arc::new(Outbox::new(16).unwrap());
    let address = format!("127.0.0.1:{}", get("PREFIX_PROBE_PORT"))
        .parse()
        .unwrap();
    let token = get("PREFIX_PROBE_TOKEN");
    let controller = Controller::start(outbox.clone(), address, token.clone()).unwrap();
    let namespace = get("PREFIX_CHAIN_NAMESPACE");
    assert!(namespace.len() == 32 && namespace.bytes().all(|b| b.is_ascii_hexdigit()));
    let provider = Arc::new(Provider {
        admission: admission_bridge::AdmissionClient::with_clock(
            address,
            token,
            get("PREFIX_PROBE_CLOCK"),
        )
        .unwrap(),
        clock,
        outbox,
        next: AtomicUsize::new(0),
        namespace,
        worker: get("PREFIX_CHAIN_WORKER"),
        instance: get("PREFIX_CHAIN_INSTANCE"),
    });
    let mut config = RouterConfig::new(
        RoutingMode::Regular {
            worker_urls: vec![worker.clone()],
        },
        PolicyConfig::RoundRobin,
    );
    config.disable_retries = true;
    config.disable_circuit_breaker = true;
    config.worker_startup_timeout_secs = 10;
    config.worker_startup_check_interval_secs = 1;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(60))
        .build()
        .unwrap();
    let ctx = Arc::new(AppContext::new(config, client, 16, None, vec![]).unwrap());
    ctx.policy_registry
        .install_http_extension(provider.clone())
        .unwrap();
    let router = Router::new(vec![worker], &ctx).await.unwrap();
    for (stream, name) in [(false, "completion"), (true, "cancel")] {
        let id = provider.next.load(Ordering::SeqCst) + 1;
        let response = route(&router, stream).await;
        assert_eq!(response.status().as_u16(), 200);
        if stream {
            let mut body = response.into_body().into_data_stream();
            let mut payload = String::new();
            let mut seen = false;
            while let Some(chunk) = tokio::time::timeout(Duration::from_secs(30), body.next())
                .await
                .unwrap()
            {
                let chunk = chunk.unwrap();
                payload.push_str(&String::from_utf8_lossy(&chunk));
                assert!(payload.len() <= 65536);
                for line in payload.lines() {
                    if let Some(data) = line.strip_prefix("data: ") {
                        if let Ok(value) = serde_json::from_str::<serde_json::Value>(data) {
                            seen |= value["choices"].as_array().is_some_and(|choices| {
                                choices
                                    .iter()
                                    .any(|c| c["text"].as_str().is_some_and(|t| !t.is_empty()))
                            });
                        }
                    }
                }
                if seen {
                    break;
                }
            }
            assert!(
                seen,
                "receive actual nonempty generated output before disconnect"
            );
            drop(body);
        } else {
            let body = to_bytes(response.into_body(), 1_048_576).await.unwrap();
            let value: serde_json::Value = serde_json::from_slice(&body).unwrap();
            assert_eq!(value["choices"].as_array().unwrap().len(), 2);
            assert!(value["choices"].as_array().unwrap().iter().all(|choice| {
                matches!(choice["finish_reason"].as_str(), Some("length" | "stop"))
                    && choice["text"].as_str().is_some_and(|text| !text.is_empty())
            }));
        }
        controller.drain(Duration::from_secs(10)).unwrap();
        let denied = route(&router, false).await;
        assert_eq!(
            denied.status().as_u16(),
            503,
            "HTTP close must not clear admission"
        );
        drop(denied);
        barrier(serde_json::json!({"chain_stage":name,"attempt_id":provider.attempt(id),"quota_rejected":true})).await;
    }
    barrier(serde_json::json!({"chain_stage":"arm_ack_loss"})).await;
    for _ in 0..2 {
        let denied = route(&router, false).await;
        assert_eq!(
            denied.status().as_u16(),
            503,
            "no request without positive permit"
        );
        drop(denied);
    }
    controller.drain(Duration::from_secs(10)).unwrap();
    barrier(serde_json::json!({"chain_stage":"ack_loss"})).await;
    println!("{}", serde_json::json!({"chain_stage":"done"}));
}
