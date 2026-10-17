// SPDX-License-Identifier: Apache-2.0
//! Fixed-Router host probe for the in-process runtime owner.
//!
//! HTTP forwarding and M3 ownership are real. M1 and M2 remain bounded local
//! fixtures, so this is not online-KV or production-startup evidence.
use axum::{body::to_bytes, response::Response};
use std::{collections::BTreeMap, sync::Arc, time::Duration};
use vllm_router_rs::{
    config::types::{PolicyConfig, RouterConfig, RoutingMode},
    protocols::spec::CompletionRequest,
    routers::http::router::Router,
    server::AppContext,
};

mod prefix_extension {
    pub use vllm_router_rs::routers::http::prefix_extension::*;
}
#[path = "../src/routers/http/runtime_owner.rs"]
mod runtime_owner;
#[path = "../src/routers/http/runtime_provider.rs"]
mod runtime_provider;

use runtime_owner::{CachePort, RuntimeOwner, TerminalResult, WorkerControl};
use runtime_provider::{FeaturePort, RequestView, RuntimeProvider};

const BLOCK: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

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
            identity: model.into(),
            block_hashes: Arc::from([BLOCK.into()]),
            exact: true,
        })
    }
}

#[derive(Debug)]
struct FixtureCache(BTreeMap<(String, String), Option<u32>>);

impl CachePort for FixtureCache {
    fn lookup(&self, worker_url: &str, instance_epoch: &str, _: &str, _: &[String]) -> Option<u32> {
        self.0
            .get(&(worker_url.into(), instance_epoch.into()))
            .copied()
            .flatten()
    }
}

fn worker(url: &str, epoch: &str, identity: &str) -> WorkerControl {
    WorkerControl {
        worker_url: url.into(),
        instance_epoch: epoch.into(),
        worker_version: 1,
        cache_identity: identity.into(),
        healthy: true,
        draining: false,
        capacity: 2,
    }
}

fn cache(workers: &[String]) -> Arc<FixtureCache> {
    Arc::new(FixtureCache(BTreeMap::from([
        ((workers[0].clone(), "worker-a-epoch".into()), Some(0)),
        ((workers[1].clone(), "worker-b-epoch".into()), Some(1)),
    ])))
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
    let owner = Arc::new(
        RuntimeOwner::new(
            cache(&workers),
            "process-a".into(),
            1,
            vec![
                worker(&workers[0], "worker-a-epoch", "runtime-cache"),
                worker(&workers[1], "worker-b-epoch", "runtime-cache"),
            ],
            16,
            16,
        )
        .unwrap(),
    );
    let provider = Arc::new(RuntimeProvider::new(
        Arc::new(FixtureFeatures),
        owner.clone(),
        owner.clone(),
    ));
    let context = Arc::new(AppContext::new(config, client, 16, None, vec![]).unwrap());
    context
        .policy_registry
        .install_http_extension(provider)
        .unwrap();
    let router = Router::new(workers.clone(), &context).await.unwrap();

    consume(route(&router, "runtime-cache").await).await;
    consume(route(&router, "runtime-base").await).await;

    let checkpoint = owner.checkpoint().unwrap();
    assert_eq!(checkpoint.active.len(), 2);
    let selected: BTreeMap<_, _> = checkpoint
        .active
        .iter()
        .map(|attempt| (attempt.request_id.clone(), attempt.worker_url.clone()))
        .collect();
    assert_eq!(selected["request-runtime-cache"], workers[1]);
    assert_eq!(selected["request-runtime-base"], workers[0]);
    let attempts: Vec<_> = checkpoint.active.iter().cloned().collect();
    for attempt in &attempts {
        assert_eq!(
            owner
                .apply_terminal(
                    &attempt.attempt_id,
                    &attempt.worker_url,
                    &attempt.instance_epoch,
                )
                .unwrap(),
            TerminalResult::Applied
        );
    }
    assert_eq!(owner.active_len().unwrap(), 0);
    assert_eq!(
        owner
            .apply_terminal(
                &attempts[0].attempt_id,
                &attempts[0].worker_url,
                &attempts[0].instance_epoch,
            )
            .unwrap(),
        TerminalResult::Duplicate
    );
    owner
        .publish(
            2,
            vec![
                worker(&workers[0], "worker-a-epoch", "runtime-cache"),
                worker(&workers[1], "worker-b-epoch", "runtime-cache"),
            ],
        )
        .unwrap();
    let restored = RuntimeOwner::restore(
        cache(&workers),
        "process-b".into(),
        owner.checkpoint().unwrap(),
        16,
        16,
    )
    .unwrap();
    assert_eq!(restored.active_len().unwrap(), 0);
    println!(
        "{}",
        serde_json::json!({
            "status": "pass",
            "selected": selected,
            "active_before_terminal": attempts.len(),
            "active_after_terminal": owner.active_len().unwrap(),
            "checks": [
                "typed_public_request_path",
                "positive_prefix_selection",
                "load_fallback_after_retained_debt",
                "in_process_admission_owner",
                "real_http_forwarding",
                "transport_close_retains_debt",
                "reliable_terminal_releases_once",
                "monotonic_config_publication",
                "new_process_epoch_restore"
            ]
        })
    );
}
