// SPDX-License-Identifier: Apache-2.0
//! CPU transport/ownership proof; no inference, reliable backend end or SLO.
use axum::body::to_bytes;
use futures_util::{Stream, StreamExt};
use std::{
    any::Any,
    pin::Pin,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex,
    },
    task::{Context, Poll},
    time::Duration,
};
use vllm_router_rs::{
    config::types::{PolicyConfig, RouterConfig, RoutingMode},
    protocols::spec::CompletionRequest,
    routers::http::{prefix_extension::*, router::Router},
    server::AppContext,
};

fn limits() -> StreamLimits {
    StreamLimits {
        chunk_bytes: 4096,
        response_bytes: 4096,
        idle: Duration::from_millis(200),
    }
}

struct Counted {
    polls: Arc<AtomicUsize>,
    drops: Arc<AtomicUsize>,
    pending: bool,
}
impl Stream for Counted {
    type Item = Result<bytes::Bytes, std::io::Error>;
    fn poll_next(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        self.polls.fetch_add(1, Ordering::SeqCst);
        if self.pending {
            Poll::Pending
        } else {
            Poll::Ready(Some(Ok(bytes::Bytes::from_static(b"abcd"))))
        }
    }
}
impl Drop for Counted {
    fn drop(&mut self) {
        self.drops.fetch_add(1, Ordering::SeqCst);
    }
}

async fn ownership_checks() {
    let polls = Arc::new(AtomicUsize::new(0));
    let drops = Arc::new(AtomicUsize::new(0));
    let closed = Arc::new(AtomicUsize::new(0));
    let cb = closed.clone();
    let mut body = DirectStream::new(
        Box::pin(Counted {
            polls: polls.clone(),
            drops: drops.clone(),
            pending: false,
        }),
        limits(),
        Box::new(move |_| {
            cb.fetch_add(1, Ordering::SeqCst);
        }),
    );
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert_eq!(
        polls.load(Ordering::SeqCst),
        0,
        "unpolled body cannot eagerly consume"
    );
    assert_eq!(body.next().await.unwrap().unwrap().len(), 4);
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert_eq!(
        polls.load(Ordering::SeqCst),
        1,
        "slow consumer cannot grow an internal queue"
    );
    drop(body);
    assert_eq!(drops.load(Ordering::SeqCst), 1);
    assert_eq!(closed.load(Ordering::SeqCst), 1);

    for pending in [false, true] {
        let cb = closed.clone();
        let l = StreamLimits {
            chunk_bytes: 4,
            response_bytes: 4,
            idle: Duration::from_millis(20),
        };
        let mut body = DirectStream::new(
            Box::pin(Counted {
                polls: polls.clone(),
                drops: drops.clone(),
                pending,
            }),
            l,
            Box::new(move |success| {
                assert!(!success);
                cb.fetch_add(1, Ordering::SeqCst);
            }),
        );
        if !pending {
            body.next().await.unwrap().unwrap();
        }
        assert!(tokio::time::timeout(Duration::from_secs(1), body.next())
            .await
            .unwrap()
            .unwrap()
            .is_err());
        assert!(body.next().await.is_none());
        drop(body);
    }
    assert_eq!(closed.load(Ordering::SeqCst), 3);
    assert_eq!(drops.load(Ordering::SeqCst), 3);

    let cb = closed.clone();
    let source = futures_util::stream::iter(vec![Ok(bytes::Bytes::from_static(b"abcd"))]);
    let mut body = DirectStream::new(
        Box::pin(source),
        limits(),
        Box::new(move |success| {
            assert!(success);
            cb.fetch_add(1, Ordering::SeqCst);
        }),
    );
    body.next().await.unwrap().unwrap();
    assert!(body.next().await.is_none());
    drop(body);
    assert_eq!(closed.load(Ordering::SeqCst), 4);

    let source = futures_util::stream::iter(vec![Ok(bytes::Bytes::from_static(b"12345"))]);
    let mut body = DirectStream::new(
        Box::pin(source),
        StreamLimits {
            chunk_bytes: 4,
            response_bytes: 8,
            ..limits()
        },
        Box::new(|success| assert!(!success)),
    );
    assert!(body.next().await.unwrap().is_err());
    let source = futures_util::stream::iter(vec![Err(std::io::Error::other("fixture"))]);
    let mut body = DirectStream::new(
        Box::pin(source),
        limits(),
        Box::new(|success| assert!(!success)),
    );
    assert!(body.next().await.unwrap().is_err());
}

#[derive(Debug)]
struct Prepared;
impl Features for Prepared {
    fn as_any(&self) -> &dyn Any {
        self
    }
}
#[derive(Debug, Default)]
struct Provider {
    closes: Arc<AtomicUsize>,
    events: Arc<Mutex<Vec<CloseEvidence>>>,
    invalid: bool,
}
#[derive(Debug)]
struct Reservation {
    closes: Arc<AtomicUsize>,
    events: Arc<Mutex<Vec<CloseEvidence>>>,
}
impl Lease for Reservation {
    fn before_send(&self) -> Result<(), &'static str> {
        Ok(())
    }
    fn sent(&self) {}
    fn headers(&self, _: u16) {}
    fn closed(&self, sent: bool, evidence: CloseEvidence) {
        assert!(sent);
        self.closes.fetch_add(1, Ordering::SeqCst);
        let mut events = self.events.lock().unwrap();
        assert!(events.len() < 4);
        events.push(evidence);
    }
}
impl HttpExtension for Provider {
    fn stream_limits(&self) -> Option<StreamLimits> {
        Some(if self.invalid {
            StreamLimits {
                response_bytes: 0,
                ..limits()
            }
        } else {
            limits()
        })
    }
    fn prepare(&self, _: &serde_json::Value, _: &str) -> Result<Arc<dyn Features>, &'static str> {
        Ok(Arc::new(Prepared))
    }
    fn select(
        &self,
        _: &dyn Features,
        available: &[String],
        revision: u64,
    ) -> Result<Option<Proposal>, &'static str> {
        Ok(Some(Proposal {
            worker_url: available[0].clone(),
            registry_revision: revision,
            decision: Arc::new(Prepared),
        }))
    }
    fn reserve(&self, _: &dyn Features, _: &Proposal) -> Result<Box<dyn Lease>, &'static str> {
        Ok(Box::new(Reservation {
            closes: self.closes.clone(),
            events: self.events.clone(),
        }))
    }
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() {
    ownership_checks().await;
    let workers: Vec<String> = std::env::args().skip(1).collect();
    assert_eq!(workers.len(), 2);
    // Exercise both native load-tracking and plain HTTP branches.
    for policy in [
        PolicyConfig::RoundRobin,
        PolicyConfig::PowerOfTwo {
            load_check_interval_secs: 1,
        },
    ] {
        let mut config = RouterConfig::new(
            RoutingMode::Regular {
                worker_urls: workers.clone(),
            },
            policy,
        );
        config.disable_retries = true;
        config.disable_circuit_breaker = true;
        config.worker_startup_timeout_secs = 5;
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(3))
            .build()
            .unwrap();
        let ctx =
            Arc::new(AppContext::new(config.clone(), client.clone(), 16, None, vec![]).unwrap());
        let provider = Arc::new(Provider::default());
        ctx.policy_registry
            .install_http_extension(provider.clone())
            .unwrap();
        let router = Router::new(workers.clone(), &ctx).await.unwrap();
        for model in [
            "fixture",
            "fixture-large",
            "fixture-stream-stall",
            "fixture-drop",
        ] {
            let request: CompletionRequest = serde_json::from_value(serde_json::json!({"model":model, "prompt":"fixture", "stream":true, "max_tokens":4})).unwrap();
            let response = router
                .route_typed_request(None, &request, "/v1/completions", None)
                .await;
            assert_eq!(response.status(), 200);
            if model == "fixture-drop" {
                drop(response);
            } else {
                let body = tokio::time::timeout(
                    Duration::from_secs(2),
                    to_bytes(response.into_body(), 8192),
                )
                .await
                .unwrap();
                if model == "fixture" {
                    assert!(body.unwrap().windows(12).any(|w| w == b"data: [DONE]"));
                } else {
                    assert!(body.is_err());
                }
            }
        }
        assert_eq!(
            provider.closes.load(Ordering::SeqCst),
            4,
            "one closure per sent attempt; all still need backend reconciliation"
        );
        assert_eq!(
            *provider.events.lock().unwrap(),
            vec![
                CloseEvidence::TransportEof,
                CloseEvidence::TransportError,
                CloseEvidence::TransportError,
                CloseEvidence::TransportDropped
            ]
        );
        for worker in ctx.worker_registry.get_all() {
            assert_eq!(
                worker.load(),
                0,
                "release native transport count once; this is not M3 backend debt"
            );
        }
        let bad_ctx = Arc::new(AppContext::new(config, client, 16, None, vec![]).unwrap());
        let bad = Arc::new(Provider {
            invalid: true,
            ..Provider::default()
        });
        bad_ctx
            .policy_registry
            .install_http_extension(bad.clone())
            .unwrap();
        let bad_router = Router::new(workers.clone(), &bad_ctx).await.unwrap();
        let request: CompletionRequest = serde_json::from_value(serde_json::json!({"model":"fixture", "prompt":"fixture", "stream":true, "max_tokens":4})).unwrap();
        let response = bad_router
            .route_typed_request(None, &request, "/v1/completions", None)
            .await;
        assert_eq!(response.status(), 503);
        drop(response);
        assert_eq!(
            bad.closes.load(Ordering::SeqCst),
            0,
            "invalid limits must fail before reserve/send"
        );
    }
    println!("{{\"status\":\"pass\",\"checks\":[\"no_eager_pulls\",\"slow_consumer\",\"drop_releases_source\",\"idle_limit\",\"chunk_limit\",\"total_limit\",\"upstream_error\",\"normal_eof\",\"native_http_both_load_branches\",\"close_exactly_once\"],\"backend_posts\":8}}");
}
