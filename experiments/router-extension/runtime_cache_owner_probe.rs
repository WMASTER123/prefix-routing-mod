// SPDX-License-Identifier: Apache-2.0
//! Fixed-Router probe combining the original KV index with the M3 owner.
//!
//! HTTP forwarding, M2 lookup and M3 ownership are real in-process paths.
//! M1 and normalized KV events remain bounded fixtures; this does not prove a
//! live HUST subscription or production startup.
use axum::{body::to_bytes, response::Response};
use std::{sync::Arc, time::Duration};
use vllm_router_rs::{
    config::types::{PolicyConfig, RouterConfig, RoutingMode},
    protocols::spec::CompletionRequest,
    routers::http::router::Router,
    server::AppContext,
};

pub mod kv_index {
    pub use vllm_router_rs::kv_index::*;
}
#[allow(dead_code)]
#[path = "../src/routers/http/runtime_directory.rs"]
mod directory;
#[allow(dead_code)]
#[path = "../src/routers/http/runtime_pool.rs"]
mod pool;
mod prefix_extension {
    pub use vllm_router_rs::routers::http::prefix_extension::*;
}
#[allow(dead_code)]
#[path = "../src/routers/http/native_kv_wire.rs"]
mod native_kv_wire;
#[path = "../src/routers/http/native_kv_owner.rs"]
#[allow(dead_code)]
mod native_kv_owner;
#[path = "../src/routers/http/native_kv_zmq.rs"]
#[allow(dead_code)]
mod native_kv_zmq;
#[allow(dead_code)]
#[path = "../src/routers/http/runtime_cache_port.rs"]
mod runtime_cache_port;
#[allow(dead_code)]
#[path = "../src/routers/http/runtime_owner.rs"]
mod runtime_owner;
#[path = "../src/routers/http/runtime_provider.rs"]
mod runtime_provider;

use directory::{Batch, Binding, Block, Limits, Op};
use runtime_cache_port::{InProcessCache, MonotonicClock, PartitionSpec};
use runtime_owner::{CachePort, RuntimeOwner, TerminalResult, WorkerControl};
use runtime_provider::{FeaturePort, RequestView, RuntimeProvider};

const BLOCK: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
const CACHE_ID: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const OTHER_ID: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

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
        let identity = match model {
            "runtime-cache" => CACHE_ID,
            "runtime-base" => OTHER_ID,
            _ => return Err("unsupported_model"),
        };
        Ok(RequestView {
            request_id: format!("request-{model}"),
            identity: identity.into(),
            block_hashes: Arc::from([BLOCK.into()]),
            exact: true,
        })
    }
}

fn limits() -> Limits {
    Limits {
        records: 64,
        operations: 64,
        queued_batches: 8,
        queue_bytes: 4096,
        total_bytes: 16384,
        query_blocks: 64,
        parent_depth: 64,
    }
}

fn spec(url: &str, label: &str, instance: u64) -> PartitionSpec {
    PartitionSpec {
        binding: Binding {
            worker: Arc::from(url),
            instance,
            publisher: Arc::from(format!("publisher-{label}")),
            identity: Arc::from(CACHE_ID),
            block_size: 128,
        },
        instance_epoch: Arc::from(format!("worker-{label}-epoch")),
    }
}

fn worker(spec: &PartitionSpec) -> WorkerControl {
    WorkerControl {
        worker_url: spec.binding.worker.to_string(),
        instance_epoch: spec.instance_epoch.to_string(),
        worker_version: 1,
        cache_identity: CACHE_ID.into(),
        healthy: true,
        draining: false,
        capacity: 2,
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
    let payload = rmp_serde::to_vec_named(&serde_json::json!([
        1.0,
        [{"type": "AllBlocksCleared"}],
        0
    ]))
    .unwrap();
    let decoded =
        native_kv_wire::decode_live(&[vec![], 0_u64.to_be_bytes().to_vec(), payload]).unwrap();
    assert_eq!(decoded.ops, vec![Op::Clear]);

    let workers: Vec<String> = std::env::args().skip(1).collect();
    assert_eq!(workers.len(), 2, "two loopback fixture origins required");
    let specs = [spec(&workers[0], "a", 1), spec(&workers[1], "b", 2)];
    let cache =
        Arc::new(InProcessCache::new(2, 32768, Arc::new(MonotonicClock::default())).unwrap());
    for spec in &specs {
        let recovery = cache
            .register(spec.clone(), limits(), 60_000_000_000)
            .unwrap();
        cache.install(&recovery, vec![], 0, 0).unwrap();
        cache.confirm_recovery(&recovery, 0).unwrap();
    }
    cache
        .live(
            &specs[1],
            Batch {
                sequence: 0,
                ops: vec![Op::Store(Block {
                    id: Arc::from(BLOCK),
                    parent: None,
                    local: Arc::from(BLOCK),
                    resident: true,
                })],
            },
        )
        .unwrap();
    let control = cache.begin_control(&specs[1]).unwrap();
    cache.confirm_control(&control, 1).unwrap();
    assert_eq!(
        cache.lookup(
            &workers[0],
            &specs[0].instance_epoch,
            CACHE_ID,
            &[BLOCK.into()]
        ),
        Some(0)
    );
    assert_eq!(
        cache.lookup(
            &workers[1],
            &specs[1].instance_epoch,
            CACHE_ID,
            &[BLOCK.into()]
        ),
        Some(1)
    );

    let owner = Arc::new(
        RuntimeOwner::new(
            cache.clone(),
            "process-a".into(),
            1,
            specs.iter().map(worker).collect(),
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
    let cache_attempt = checkpoint
        .active
        .iter()
        .find(|attempt| attempt.request_id == "request-runtime-cache")
        .unwrap();
    let base_attempt = checkpoint
        .active
        .iter()
        .find(|attempt| attempt.request_id == "request-runtime-base")
        .unwrap();
    assert_eq!(cache_attempt.worker_url, workers[1]);
    assert_eq!(base_attempt.worker_url, workers[0]);
    for attempt in &checkpoint.active {
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
    cache.disconnect(&specs[1]).unwrap();
    assert_eq!(
        cache.lookup(
            &workers[1],
            &specs[1].instance_epoch,
            CACHE_ID,
            &[BLOCK.into()]
        ),
        None
    );
    let replacement = spec(&workers[1], "b-next", 3);
    let recovery = cache
        .replace(&specs[1], replacement.clone(), limits(), 60_000_000_000)
        .unwrap();
    cache.install(&recovery, vec![], 0, 0).unwrap();
    cache.confirm_recovery(&recovery, 0).unwrap();
    assert_eq!(
        cache.lookup(
            &workers[1],
            &specs[1].instance_epoch,
            CACHE_ID,
            &[BLOCK.into()]
        ),
        None
    );
    assert_eq!(
        cache.lookup(
            &workers[1],
            &replacement.instance_epoch,
            CACHE_ID,
            &[BLOCK.into()]
        ),
        Some(0)
    );
    cache.unregister(&replacement).unwrap();
    assert_eq!(cache.reserved_bytes().unwrap(), 16384);
    println!(
        "{}",
        serde_json::json!({
            "status": "pass",
            "selected": {
                "request-runtime-cache": cache_attempt.worker_url,
                "request-runtime-base": base_attempt.worker_url,
            },
            "active_before_terminal": checkpoint.active.len(),
            "active_after_terminal": owner.active_len().unwrap(),
            "reserved_bytes_after_unregister": cache.reserved_bytes().unwrap(),
            "checks": [
                "typed_public_request_path",
                "original_index_positive_prefix_selection",
                "trusted_miss_distinct_from_unknown",
                "load_fallback_after_retained_debt",
                "real_http_forwarding",
                "reliable_terminal_releases_once",
                "disconnect_withdraws_directory_trust",
                "exact_instance_replacement_requires_recovery",
                "unregister_reclaims_partition_quota",
                "bounded_pool_reservation"
            ]
        })
    );
}
