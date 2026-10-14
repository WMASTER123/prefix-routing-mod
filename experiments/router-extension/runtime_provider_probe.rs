// SPDX-License-Identifier: Apache-2.0
//! Fixed-Router host probe for the candidate runtime composition.
//!
//! This uses the Router's typed public request path and real HTTP forwarding.
//! The injected M1/M2/M3 ports are deterministic fixtures, not production
//! tokenization, an online KV directory or a durable admission owner.
use axum::{body::to_bytes, response::Response};
use std::{
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use vllm_router_rs::{
    config::types::{PolicyConfig, RouterConfig, RoutingMode},
    protocols::spec::CompletionRequest,
    routers::http::{
        prefix_extension as host_extension,
        prefix_extension::{CloseEvidence, Lease},
        router::Router,
    },
    server::AppContext,
};

mod prefix_extension {
    pub use vllm_router_rs::routers::http::prefix_extension::*;
}
#[path = "../src/routers/http/runtime_provider.rs"]
mod runtime_provider;
use runtime_provider::{
    AdmissionPort, Candidate, Decision, FeaturePort, RequestView, RuntimeProvider, SnapshotPort,
};

const BLOCK: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

#[derive(Debug, Default)]
struct ProbeState {
    selected: Vec<String>,
    sent: usize,
    closed: usize,
}

#[derive(Debug)]
struct FixtureFeatures;

impl FeaturePort for FixtureFeatures {
    fn extract(
        &self,
        request: &serde_json::Value,
        route: &str,
    ) -> Result<RequestView, &'static str> {
        if route != "/v1/completions" {
            return Err("unsupported_route");
        }
        let model = request["model"].as_str().ok_or("missing_model")?;
        if !matches!(model, "runtime-cache" | "runtime-base") {
            return Err("unsupported_model");
        }
        Ok(RequestView {
            request_id: format!("request-{model}"),
            identity: model.to_owned(),
            block_hashes: Arc::from([BLOCK.to_owned()]),
            exact: true,
        })
    }
}

#[derive(Debug, Default)]
struct FixtureSnapshot;

impl SnapshotPort for FixtureSnapshot {
    fn candidates(
        &self,
        features: &RequestView,
        available: &[String],
    ) -> Result<Vec<Candidate>, &'static str> {
        if available.len() != 2 {
            return Err("two_workers_required");
        }
        Ok(vec![
            Candidate {
                worker_url: available[0].clone(),
                instance_epoch: "worker-a-epoch".into(),
                worker_version: 1,
                config_version: 1,
                healthy: true,
                draining: false,
                local_outstanding: 0,
                matched_blocks: Some(0),
            },
            Candidate {
                worker_url: available[1].clone(),
                instance_epoch: "worker-b-epoch".into(),
                worker_version: 1,
                config_version: 1,
                healthy: true,
                draining: false,
                local_outstanding: 1,
                matched_blocks: (features.identity == "runtime-cache").then_some(1),
            },
        ])
    }
}

#[derive(Debug)]
struct FixtureAdmission {
    next: AtomicU64,
    state: Arc<Mutex<ProbeState>>,
}

impl AdmissionPort for FixtureAdmission {
    fn allocate_attempt(&self, request_id: &str) -> Result<String, &'static str> {
        let sequence = self.next.fetch_add(1, Ordering::Relaxed);
        Ok(format!("{request_id}-attempt-{sequence}"))
    }

    fn reserve_async<'a>(
        &'a self,
        features: &'a RequestView,
        decision: &'a Decision,
    ) -> host_extension::AdmissionFuture<'a> {
        Box::pin(async move {
            if decision.request_id != features.request_id {
                return Err("request_fence");
            }
            self.state
                .lock()
                .unwrap()
                .selected
                .push(decision.worker_url.clone());
            Ok(Box::new(FixtureLease {
                attempt_id: decision.attempt_id.clone(),
                state: self.state.clone(),
            }) as Box<dyn Lease>)
        })
    }
}

#[derive(Debug)]
struct FixtureLease {
    attempt_id: String,
    state: Arc<Mutex<ProbeState>>,
}

impl Lease for FixtureLease {
    fn request_id_header(&self) -> Option<&str> {
        Some(&self.attempt_id)
    }

    fn before_send(&self) -> Result<(), &'static str> {
        Ok(())
    }

    fn sent(&self) {
        self.state.lock().unwrap().sent += 1;
    }

    fn headers(&self, _: u16) {}

    fn closed(&self, _: bool, _: CloseEvidence) {
        self.state.lock().unwrap().closed += 1;
    }
}

fn request(model: &str) -> CompletionRequest {
    serde_json::from_value(serde_json::json!({
        "model": model,
        "prompt": "fixture",
        "max_tokens": 1,
        "stream": false,
    }))
    .unwrap()
}

async fn route(router: &Router, model: &str) -> Response {
    router
        .route_typed_request(None, &request(model), "/v1/completions", None)
        .await
}

async fn consume(response: Response) {
    assert_eq!(response.status().as_u16(), 200);
    let body = to_bytes(response.into_body(), 16_384).await.unwrap();
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&body).unwrap()["choices"][0]["message"]
            ["content"],
        "hello world"
    );
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() {
    let workers: Vec<String> = std::env::args().skip(1).collect();
    assert_eq!(workers.len(), 2, "two loopback fixture origins required");
    let mut config = RouterConfig::new(
        RoutingMode::Regular {
            worker_urls: workers.clone(),
        },
        PolicyConfig::RoundRobin,
    );
    config.disable_retries = true;
    config.disable_circuit_breaker = true;
    config.worker_startup_timeout_secs = 5;
    config.worker_startup_check_interval_secs = 1;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap();
    let state = Arc::new(Mutex::new(ProbeState::default()));
    let provider = Arc::new(RuntimeProvider::new(
        Arc::new(FixtureFeatures),
        Arc::new(FixtureSnapshot),
        Arc::new(FixtureAdmission {
            next: AtomicU64::new(1),
            state: state.clone(),
        }),
    ));
    let context = Arc::new(AppContext::new(config, client, 16, None, vec![]).unwrap());
    context
        .policy_registry
        .install_http_extension(provider)
        .unwrap();
    let router = Router::new(workers.clone(), &context).await.unwrap();

    consume(route(&router, "runtime-cache").await).await;
    consume(route(&router, "runtime-base").await).await;

    let state = state.lock().unwrap();
    assert_eq!(state.selected, vec![workers[1].clone(), workers[0].clone()]);
    assert_eq!((state.sent, state.closed), (2, 2));
    println!(
        "{}",
        serde_json::json!({
            "status": "pass",
            "selected": state.selected.clone(),
            "checks": [
                "typed_public_request_path",
                "positive_prefix_selection",
                "load_fallback",
                "async_admission",
                "real_http_forwarding",
                "terminal_callback"
            ]
        })
    );
}
