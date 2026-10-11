// SPDX-License-Identifier: Apache-2.0
//! Uses the actual patched Router library and loopback HTTP backends. Tokens,
//! instance/control fences and reconciliation proof here are synthetic only.
use axum::{body::to_bytes, response::Response};
use std::{
    any::Any,
    sync::{Arc, Mutex},
    time::Duration,
};
use vllm_router_rs::{
    config::types::{PolicyConfig, RouterConfig, RoutingMode},
    protocols::spec::CompletionRequest,
    routers::http::{
        prefix_extension::{CloseEvidence, Features, HttpExtension, Lease, Proposal},
        router::Router,
    },
    server::AppContext,
};

#[derive(Debug)]
struct FixtureFeatures {
    model: String,
    tokens: Arc<[u32]>,
    control: u64,
    request_id: usize,
}
impl Features for FixtureFeatures {
    fn as_any(&self) -> &dyn Any {
        self
    }
}

#[derive(Debug)]
struct FixtureDecision {
    request_id: usize,
    control: u64,
    instance_epoch: u64,
}
impl Features for FixtureDecision {
    fn as_any(&self) -> &dyn Any {
        self
    }
}

#[derive(Debug, Default)]
struct State {
    prepare: usize,
    select: usize,
    reserve: usize,
    sent: usize,
    headers: usize,
    closed: usize,
    unknown: usize,
    not_sent: usize,
    outstanding: usize,
    control: u64,
    reject_send: bool,
    events: Vec<CloseEvidence>,
}

#[derive(Debug, Default)]
struct FixtureProvider {
    state: Arc<Mutex<State>>,
}
#[derive(Debug)]
struct FixtureLease {
    state: Arc<Mutex<State>>,
    control: u64,
}

impl Lease for FixtureLease {
    fn before_send(&self) -> Result<(), &'static str> {
        let state = self.state.lock().unwrap();
        if state.reject_send || state.control != self.control {
            Err("control_fence")
        } else {
            Ok(())
        }
    }
    fn sent(&self) {
        self.state.lock().unwrap().sent += 1;
    }
    fn headers(&self, _: u16) {
        self.state.lock().unwrap().headers += 1;
    }
    fn closed(&self, sent: bool, evidence: CloseEvidence) {
        let mut state = self.state.lock().unwrap();
        assert!(state.events.len() < 128, "fixture evidence budget");
        state.events.push(evidence);
        state.closed += 1;
        if sent {
            state.unknown += 1;
        } else {
            state.not_sent += 1;
            state.outstanding -= 1;
        }
    }
}

impl HttpExtension for FixtureProvider {
    fn prepare(
        &self,
        request: &serde_json::Value,
        route: &str,
    ) -> Result<Arc<dyn Features>, &'static str> {
        let mut state = self.state.lock().unwrap();
        state.prepare += 1;
        // A deterministic fixture, not a tokenizer or production KV identity.
        assert_eq!(route, "/v1/completions");
        assert_eq!(request["prompt"], "fixture");
        let model = request["model"].as_str().unwrap().to_owned();
        Ok(Arc::new(FixtureFeatures {
            model,
            tokens: Arc::from([10, 11, 12]),
            control: state.control,
            request_id: state.prepare,
        }))
    }
    fn select(
        &self,
        features: &dyn Features,
        available: &[String],
        revision: u64,
    ) -> Result<Option<Proposal>, &'static str> {
        let features = features.as_any().downcast_ref::<FixtureFeatures>().unwrap();
        assert_eq!(&*features.tokens, &[10, 11, 12]);
        self.state.lock().unwrap().select += 1;
        if features.model == "fixture-fallback" {
            return Ok(None);
        }
        let worker_url = if features.model == "fixture-foreign" {
            "http://127.0.0.1:1".into()
        } else {
            available.last().ok_or("empty_fixture_pool")?.clone()
        };
        Ok(Some(Proposal {
            worker_url,
            registry_revision: revision + u64::from(features.model == "fixture-stale"),
            decision: Arc::new(FixtureDecision {
                request_id: features.request_id,
                control: features.control,
                instance_epoch: 1,
            }),
        }))
    }
    fn reserve(
        &self,
        features: &dyn Features,
        proposal: &Proposal,
    ) -> Result<Box<dyn Lease>, &'static str> {
        let features = features.as_any().downcast_ref::<FixtureFeatures>().unwrap();
        let decision = proposal
            .decision
            .as_any()
            .downcast_ref::<FixtureDecision>()
            .unwrap();
        let mut state = self.state.lock().unwrap();
        if features.model == "fixture-reserve-deny" || state.outstanding >= 8 {
            return Err("admission_limit");
        }
        assert_eq!(state.control, features.control);
        assert_eq!(decision.control, state.control);
        assert_eq!(decision.request_id, features.request_id);
        assert_eq!(decision.instance_epoch, 1);
        state.reserve += 1;
        state.outstanding += 1;
        state.reject_send = features.model == "fixture-send-deny";
        if features.model == "fixture-config-change" {
            state.control += 1;
        }
        Ok(Box::new(FixtureLease {
            state: self.state.clone(),
            control: features.control,
        }))
    }
}

fn request(model: &str, stream: bool) -> CompletionRequest {
    serde_json::from_value(serde_json::json!({
        "model": model, "prompt": "fixture", "max_tokens": 4, "stream": stream,
    }))
    .unwrap()
}

async fn consume(response: Response, status: u16, stream: bool) {
    assert_eq!(response.status().as_u16(), status);
    let body = to_bytes(response.into_body(), 16384).await.unwrap();
    if status == 200 {
        if stream {
            assert!(body.windows(12).any(|w| w == b"data: [DONE]"));
        } else {
            assert_eq!(
                serde_json::from_slice::<serde_json::Value>(&body).unwrap()["choices"][0]
                    ["message"]["content"],
                "hello world"
            );
        }
    }
}

async fn route(router: &Router, model: &str, stream: bool) -> Response {
    router
        .route_typed_request(None, &request(model, stream), "/v1/completions", None)
        .await
}

fn settle_fixture(provider: &FixtureProvider) {
    // Explicit synthetic ENDED receipt. HTTP EOF/drop did NOT clear the debt.
    let mut state = provider.state.lock().unwrap();
    assert_eq!(state.outstanding, state.unknown);
    state.outstanding = 0;
    state.unknown = 0;
    state.reject_send = false;
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
    let off_ctx =
        Arc::new(AppContext::new(config.clone(), client.clone(), 16, None, vec![]).unwrap());
    let off_router = Router::new(workers.clone(), &off_ctx).await.unwrap();

    // Patched library with no provider preserves existing HTTP/SSE policy path.
    consume(route(&off_router, "fixture", false).await, 200, false).await;
    consume(route(&off_router, "fixture", true).await, 200, true).await;
    consume(route(&off_router, "fixture-error", false).await, 503, false).await;

    let provider = Arc::new(FixtureProvider::default());
    let ctx = Arc::new(AppContext::new(config, client, 16, None, vec![]).unwrap());
    ctx.policy_registry
        .install_http_extension(provider.clone())
        .unwrap();
    assert!(ctx
        .policy_registry
        .install_http_extension(provider.clone())
        .is_err());
    let router = Router::new(workers, &ctx).await.unwrap();
    for stream in [false, true] {
        consume(route(&router, "fixture", stream).await, 200, stream).await;
        assert_eq!(provider.state.lock().unwrap().outstanding, 1);
        assert_eq!(provider.state.lock().unwrap().unknown, 1);
        settle_fixture(&provider);
    }
    for model in ["fixture-stale", "fixture-foreign", "fixture-reserve-deny"] {
        let before = provider.state.lock().unwrap().reserve;
        consume(route(&router, model, false).await, 503, false).await;
        assert_eq!(provider.state.lock().unwrap().reserve, before);
    }
    for model in ["fixture-send-deny", "fixture-config-change"] {
        let before = provider.state.lock().unwrap().sent;
        consume(route(&router, model, false).await, 503, false).await;
        assert_eq!(provider.state.lock().unwrap().sent, before);
        assert_eq!(provider.state.lock().unwrap().outstanding, 0);
        settle_fixture(&provider);
    }
    consume(route(&router, "fixture-error", false).await, 503, false).await;
    assert_eq!(provider.state.lock().unwrap().unknown, 1);
    settle_fixture(&provider);

    consume(
        route(&router, "fixture-disconnect", false).await,
        500,
        false,
    )
    .await;
    assert_eq!(provider.state.lock().unwrap().unknown, 1);
    assert_eq!(
        provider.state.lock().unwrap().events.last(),
        Some(&CloseEvidence::TransportError)
    );
    settle_fixture(&provider);

    let response = route(&router, "fixture", true).await;
    assert_eq!(response.status(), 200);
    drop(response);
    assert_eq!(provider.state.lock().unwrap().unknown, 1);
    assert_eq!(
        provider.state.lock().unwrap().events.last(),
        Some(&CloseEvidence::TransportDropped)
    );
    settle_fixture(&provider);

    // Cancel the route future AFTER the send boundary; backend outcome unknown.
    let result = tokio::time::timeout(
        Duration::from_millis(100),
        route(&router, "fixture-stall", false),
    )
    .await;
    assert!(result.is_err());
    assert_eq!(provider.state.lock().unwrap().unknown, 1);
    settle_fixture(&provider);

    let before = provider.state.lock().unwrap().reserve;
    consume(route(&router, "fixture-fallback", false).await, 200, false).await;
    assert_eq!(provider.state.lock().unwrap().reserve, before);

    // Bounded conservative debt refuses a ninth attempt, never silently clears.
    for _ in 0..8 {
        consume(route(&router, "fixture", false).await, 200, false).await;
    }
    let before = provider.state.lock().unwrap().sent;
    consume(route(&router, "fixture", false).await, 503, false).await;
    {
        let state = provider.state.lock().unwrap();
        assert_eq!(state.sent, before);
        assert_eq!(state.outstanding, 8);
    }
    settle_fixture(&provider);
    let replies = futures_util::future::join_all((0..16).map(|_| async {
        let response = route(&router, "fixture", false).await;
        let status = response.status().as_u16();
        assert!(matches!(status, 200 | 503));
        consume(response, status, false).await;
        status
    }))
    .await;
    assert_eq!(replies.iter().filter(|&&s| s == 200).count(), 8);
    let state = provider.state.lock().unwrap();
    assert_eq!(state.outstanding, 8);
    assert_eq!(state.reserve, state.closed);
    println!("{{\"status\":\"pass\",\"prepare\":{},\"select\":{},\"reserve\":{},\"sent\":{},\"headers\":{},\"closed\":{},\"not_sent\":{},\"unresolved_fixture_debt\":{},\"checks\":[\"default_off\",\"install_once\",\"prepared_tokens\",\"stale_or_foreign_target\",\"reserve_rejection\",\"before_send_control_fence\",\"http_eof_is_unknown\",\"503_no_resend\",\"body_drop\",\"future_cancel\",\"explicit_fallback\",\"bounded_debt\",\"concurrent_reservation\"]}}", state.prepare, state.select, state.reserve, state.sent, state.headers, state.closed, state.not_sent, state.outstanding);
}
