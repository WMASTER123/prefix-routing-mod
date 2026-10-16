// SPDX-License-Identifier: Apache-2.0
use crate::prefix_extension::{DirectStream, StreamLimits};
use bytes::Bytes;
use futures_util::{Stream, StreamExt};
use std::{
    pin::Pin,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex,
    },
    task::{Context, Poll},
    time::Duration,
};

use crate::request_feature_port::{CompletionFeaturePort, TokenizerPort};

struct Empty {
    polls: Arc<AtomicUsize>,
    drops: Arc<AtomicUsize>,
}

impl Stream for Empty {
    type Item = Result<Bytes, std::io::Error>;
    fn poll_next(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        self.polls.fetch_add(1, Ordering::SeqCst);
        Poll::Ready(Some(Ok(Bytes::new())))
    }
}

impl Drop for Empty {
    fn drop(&mut self) {
        self.drops.fetch_add(1, Ordering::SeqCst);
    }
}

fn limits() -> StreamLimits {
    StreamLimits {
        chunk_bytes: 4,
        response_bytes: 8,
        idle: Duration::from_millis(20),
    }
}

fn callback(outcomes: &Arc<Mutex<Vec<bool>>>) -> Box<dyn FnOnce(bool) + Send> {
    let outcomes = outcomes.clone();
    Box::new(move |success| outcomes.lock().unwrap().push(success))
}

#[tokio::test(start_paused = true)]
async fn continuously_ready_empty_frames_yield_and_expire_without_resetting_idle() {
    let polls = Arc::new(AtomicUsize::new(0));
    let drops = Arc::new(AtomicUsize::new(0));
    let outcomes = Arc::new(Mutex::new(Vec::new()));
    let mut body = DirectStream::new(
        Box::pin(Empty {
            polls: polls.clone(),
            drops: drops.clone(),
        }),
        limits(),
        callback(&outcomes),
    );
    let waker = futures_util::task::noop_waker();
    let mut context = Context::from_waker(&waker);
    for expected in 1..=5 {
        assert!(Pin::new(&mut body).poll_next(&mut context).is_pending());
        assert_eq!(polls.load(Ordering::SeqCst), expected);
        tokio::time::advance(Duration::from_millis(3)).await;
    }
    tokio::time::advance(Duration::from_millis(6)).await;
    let error = body.next().await.unwrap().unwrap_err();
    assert_eq!(error.to_string(), "stream_idle_limit");
    assert_eq!(drops.load(Ordering::SeqCst), 1);
    assert!(body.next().await.is_none());
    drop(body);
    assert_eq!(*outcomes.lock().unwrap(), vec![false]);
}

#[tokio::test(start_paused = true)]
async fn empty_frames_are_suppressed_and_nonempty_progress_resets_idle() {
    let outcomes = Arc::new(Mutex::new(Vec::new()));
    let source = futures_util::stream::iter(vec![Ok(Bytes::new()), Ok(Bytes::from("a"))])
        .chain(futures_util::stream::pending());
    let mut body = DirectStream::new(Box::pin(source), limits(), callback(&outcomes));
    tokio::time::advance(Duration::from_millis(15)).await;
    assert_eq!(body.next().await.unwrap().unwrap(), Bytes::from("a"));
    tokio::time::advance(Duration::from_millis(15)).await;
    let waker = futures_util::task::noop_waker();
    assert!(Pin::new(&mut body)
        .poll_next(&mut Context::from_waker(&waker))
        .is_pending());
    tokio::time::advance(Duration::from_millis(6)).await;
    assert_eq!(
        body.next().await.unwrap().unwrap_err().to_string(),
        "stream_idle_limit"
    );
    assert_eq!(*outcomes.lock().unwrap(), vec![false]);
}

#[tokio::test(start_paused = true)]
async fn ready_buffered_data_is_allowed_after_a_slow_consumer_pause() {
    let outcomes = Arc::new(Mutex::new(Vec::new()));
    let source = futures_util::stream::iter(vec![Ok(Bytes::from("abcd"))]);
    let mut body = DirectStream::new(Box::pin(source), limits(), callback(&outcomes));
    tokio::time::advance(Duration::from_secs(1)).await;
    assert_eq!(body.next().await.unwrap().unwrap(), Bytes::from("abcd"));
    assert!(body.next().await.is_none());
    drop(body);
    assert_eq!(*outcomes.lock().unwrap(), vec![true]);
}

#[tokio::test]
async fn chunk_and_total_limits_close_once_and_never_expose_oversized_data() {
    for chunks in [vec!["12345"], vec!["1234", "5678", "9"]] {
        let outcomes = Arc::new(Mutex::new(Vec::new()));
        let source =
            futures_util::stream::iter(chunks.into_iter().map(|value| Ok(Bytes::from(value))));
        let mut body = DirectStream::new(Box::pin(source), limits(), callback(&outcomes));
        let mut received = 0;
        loop {
            match body.next().await.unwrap() {
                Ok(bytes) => received += bytes.len(),
                Err(error) => {
                    assert_eq!(error.to_string(), "stream_size_limit");
                    break;
                }
            }
        }
        assert!(received <= 8);
        assert!(body.next().await.is_none());
        drop(body);
        assert_eq!(*outcomes.lock().unwrap(), vec![false]);
    }
}

#[tokio::test]
async fn unconsumed_drop_releases_source_without_polling_or_duplicate_finish() {
    let polls = Arc::new(AtomicUsize::new(0));
    let drops = Arc::new(AtomicUsize::new(0));
    let outcomes = Arc::new(Mutex::new(Vec::new()));
    let body = DirectStream::new(
        Box::pin(Empty {
            polls: polls.clone(),
            drops: drops.clone(),
        }),
        limits(),
        callback(&outcomes),
    );
    drop(body);
    assert_eq!(polls.load(Ordering::SeqCst), 0);
    assert_eq!(drops.load(Ordering::SeqCst), 1);
    assert_eq!(*outcomes.lock().unwrap(), vec![false]);
}

#[tokio::test]
async fn source_errors_are_sanitized_and_finish_once() {
    let outcomes = Arc::new(Mutex::new(Vec::new()));
    let source = futures_util::stream::iter(vec![Err(std::io::Error::other("private"))]);
    let mut body = DirectStream::new(Box::pin(source), limits(), callback(&outcomes));
    assert_eq!(
        body.next().await.unwrap().unwrap_err().to_string(),
        "upstream_stream_error"
    );
    assert!(body.next().await.is_none());
    drop(body);
    assert_eq!(*outcomes.lock().unwrap(), vec![false]);
}

#[derive(Debug)]
struct Prepared;
impl crate::prefix_extension::Features for Prepared {
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}

#[derive(Debug)]
struct CountedLease {
    denied: bool,
    closes: Arc<AtomicUsize>,
}
impl crate::prefix_extension::Lease for CountedLease {
    fn before_send(&self) -> Result<(), &'static str> {
        if self.denied {
            Err("control_fence")
        } else {
            Ok(())
        }
    }
    fn sent(&self) {}
    fn headers(&self, _: u16) {}
    fn closed(&self, _: bool, _: crate::prefix_extension::CloseEvidence) {
        self.closes.fetch_add(1, Ordering::SeqCst);
    }
}

fn attempt(
    denied: bool,
) -> (
    crate::prefix_extension::AttemptGuard,
    Arc<Mutex<crate::lifecycle_bridge::LifecycleTrace>>,
    Arc<AtomicUsize>,
) {
    use crate::{lifecycle_bridge::*, prefix_extension::*};
    let trace = Arc::new(Mutex::new(
        LifecycleTrace::new(
            "request-a".into(),
            "attempt-a".into(),
            "worker-a".into(),
            "instance-a".into(),
            Arc::new(RouterClock::new("clock-a".into()).unwrap()),
        )
        .unwrap(),
    ));
    let closes = Arc::new(AtomicUsize::new(0));
    let guard = AttemptGuard::new(
        Box::new(TracedLease {
            inner: Box::new(CountedLease {
                denied,
                closes: closes.clone(),
            }),
            trace: trace.clone(),
        }),
        Proposal {
            worker_url: "http://127.0.0.1:1".into(),
            registry_revision: 0,
            decision: Arc::new(Prepared),
        },
    );
    (guard, trace, closes)
}

#[test]
fn rejected_send_and_unstarted_drop_emit_definite_unsent_only() {
    for denied in [false, true] {
        let (mut guard, trace, closes) = attempt(denied);
        if denied {
            assert_eq!(guard.start(), Err("control_fence"));
        }
        drop(guard);
        let events = serde_json::to_value(trace.lock().unwrap().snapshot().unwrap()).unwrap();
        assert_eq!(events.as_array().unwrap().len(), 1);
        assert_eq!(events[0]["kind"], "not_sent");
        assert_eq!(events[0]["confirmation"], "not_accepted");
        assert_eq!(closes.load(Ordering::SeqCst), 1);
    }
}

#[test]
fn headers_and_drop_never_produce_accepted_or_backend_ended() {
    let (mut guard, trace, closes) = attempt(false);
    guard.start().unwrap();
    assert_eq!(guard.start(), Err("duplicate_send"));
    guard.headers(200);
    guard.headers(503);
    drop(guard);
    let events = serde_json::to_value(trace.lock().unwrap().snapshot().unwrap()).unwrap();
    assert_eq!(events.as_array().unwrap().len(), 2);
    assert_eq!(events[0]["kind"], "send_started");
    assert_eq!(events[1]["kind"], "outcome_unknown");
    assert_eq!(events[1]["confirmation"], "none");
    assert_eq!(events[1]["error_class"], "transport_dropped");
    assert_eq!(events[1]["sequence"], 2);
    assert_eq!(closes.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn actual_body_eof_and_error_produce_conservative_sequenced_events() {
    use crate::prefix_extension::observe_body;
    for error in [false, true] {
        let (mut guard, trace, closes) = attempt(false);
        guard.start().unwrap();
        let body = if error {
            axum::body::Body::from_stream(futures_util::stream::iter(vec![Err::<Bytes, _>(
                std::io::Error::other("fixture"),
            )]))
        } else {
            axum::body::Body::from("data: [DONE]\n\n")
        };
        let response = observe_body(axum::response::Response::new(body), guard);
        let result = axum::body::to_bytes(response.into_body(), 1024).await;
        assert_eq!(result.is_err(), error);
        let events = serde_json::to_value(trace.lock().unwrap().snapshot().unwrap()).unwrap();
        assert_eq!(events[1]["kind"], "outcome_unknown");
        assert_eq!(
            events[1]["error_class"],
            if error {
                "transport_error"
            } else {
                "transport_eof"
            }
        );
        assert_eq!(closes.load(Ordering::SeqCst), 1);
    }
}

#[test]
fn lifecycle_identity_rejects_control_bytes_and_event_id_overflow() {
    use crate::lifecycle_bridge::{LifecycleTrace, RouterClock};
    assert!(RouterClock::new("clock with spaces".into()).is_err());
    for attempt_id in ["a\nb".into(), "x".repeat(121)] {
        assert!(LifecycleTrace::new(
            "request".into(),
            attempt_id,
            "worker".into(),
            "instance".into(),
            Arc::new(RouterClock::new("clock".into()).unwrap())
        )
        .is_err());
    }
}

#[derive(Debug)]
struct FixedTokenizer {
    tokens: Vec<u32>,
    error: bool,
}

impl TokenizerPort for FixedTokenizer {
    fn encode(&self, _: &str) -> Result<Vec<u32>, &'static str> {
        if self.error {
            Err("tokenizer_failed")
        } else {
            Ok(self.tokens.clone())
        }
    }
}

fn completion_features(tokens: Vec<u32>) -> CompletionFeaturePort {
    CompletionFeaturePort::new(
        Arc::new(FixedTokenizer {
            tokens,
            error: false,
        }),
        "qwen".into(),
        "qwen-profile".into(),
        "epoch-a".into(),
        2,
        16,
        8,
    )
    .unwrap()
}

#[test]
fn completion_features_hash_complete_blocks_once_and_use_owned_ids() {
    use crate::runtime_provider::FeaturePort;

    let port = completion_features(vec![1, 2, 3, 4, 5]);
    let request = serde_json::json!({"model":"qwen", "prompt":"hello"});
    let first = port.extract(&request, "/v1/completions").unwrap();
    let second = port.extract(&request, "/v1/completions").unwrap();
    assert_eq!(first.identity, "qwen-profile");
    assert!(first.exact);
    assert_eq!(first.request_id, "prm-epoch-a-1");
    assert_eq!(second.request_id, "prm-epoch-a-2");
    assert_eq!(
        first.block_hashes.as_ref(),
        [
            "34fb5c825de7ca4aea6e712f19d439c1da0c92c37b423936c5f618545ca4fa1f",
            "8073c94ef47ecc86dcd78a8d9027a23484fadcd7cea37150319ba8cbf1c70b6b",
        ]
    );
}

#[test]
fn completion_features_reject_unsupported_or_unbounded_inputs() {
    use crate::runtime_provider::FeaturePort;

    let port = completion_features(vec![1, 2]);
    for (route, request, error) in [
        (
            "/v1/chat/completions",
            serde_json::json!({"model":"qwen", "prompt":"hello"}),
            "unsupported_feature_route",
        ),
        (
            "/v1/completions",
            serde_json::json!({"model":"other", "prompt":"hello"}),
            "unsupported_feature_model",
        ),
        (
            "/v1/completions",
            serde_json::json!({"model":"qwen", "prompt":["hello"]}),
            "unsupported_completion_prompt",
        ),
        (
            "/v1/completions",
            serde_json::json!({"model":"qwen", "prompt":"0123456789abcdefg"}),
            "prompt_byte_limit",
        ),
        (
            "/v1/completions",
            serde_json::json!({"model":"qwen", "prompt":"hello", "cache_salt":"tenant-a"}),
            "unsupported_kv_identity_modifier",
        ),
        (
            "/v1/completions",
            serde_json::json!({"model":"qwen", "prompt":"hello", "lora_name":"adapter-a"}),
            "unsupported_kv_identity_modifier",
        ),
        (
            "/v1/completions",
            serde_json::json!({"model":"qwen", "prompt":"hello", "prompt_embeds":[[0.1]]}),
            "unsupported_kv_identity_modifier",
        ),
        (
            "/v1/completions",
            serde_json::json!({"model":"qwen", "prompt":"hello", "truncate_prompt_tokens":4}),
            "unsupported_kv_identity_modifier",
        ),
        (
            "/v1/completions",
            serde_json::json!({"model":"qwen", "prompt":"hello", "session_params":{"id":"a"}}),
            "unsupported_kv_identity_modifier",
        ),
    ] {
        assert_eq!(port.extract(&request, route).unwrap_err(), error);
    }

    assert!(port
        .extract(
            &serde_json::json!({"model":"qwen", "prompt":"hello", "cache_salt":null}),
            "/v1/completions",
        )
        .is_ok());

    let too_many = completion_features(vec![0; 9]);
    assert_eq!(
        too_many
            .extract(
                &serde_json::json!({"model":"qwen", "prompt":"hello"}),
                "/v1/completions",
            )
            .unwrap_err(),
        "prompt_token_limit"
    );
    let failed = CompletionFeaturePort::new(
        Arc::new(FixedTokenizer {
            tokens: vec![],
            error: true,
        }),
        "qwen".into(),
        "qwen-profile".into(),
        "epoch-a".into(),
        2,
        16,
        8,
    )
    .unwrap();
    assert_eq!(
        failed
            .extract(
                &serde_json::json!({"model":"qwen", "prompt":"hello"}),
                "/v1/completions",
            )
            .unwrap_err(),
        "tokenizer_failed"
    );
}

#[test]
fn completion_feature_constructor_rejects_ambiguous_identity_and_limits() {
    let tokenizer: Arc<dyn TokenizerPort> = Arc::new(FixedTokenizer {
        tokens: vec![],
        error: false,
    });
    assert!(CompletionFeaturePort::new(
        tokenizer.clone(),
        "qwen".into(),
        "identity".into(),
        "bad epoch".into(),
        2,
        16,
        8,
    )
    .is_err());
    assert!(CompletionFeaturePort::new(
        tokenizer.clone(),
        "qwen".into(),
        "bad identity".into(),
        "epoch".into(),
        2,
        16,
        8,
    )
    .is_err());
    assert!(CompletionFeaturePort::new(
        tokenizer,
        "qwen".into(),
        "identity".into(),
        "epoch".into(),
        0,
        16,
        8,
    )
    .is_err());
}

mod runtime_composition {
    use crate::{
        prefix_extension::{CloseEvidence, HttpExtension, Lease},
        runtime_provider::{
            AdmissionPort, Candidate, FeaturePort, RequestView, RuntimeProvider, SnapshotPort,
        },
    };
    use serde_json::{json, Value};
    use std::sync::{Arc, Mutex};

    #[derive(Debug)]
    struct FeatureFixture;
    impl FeaturePort for FeatureFixture {
        fn extract(&self, request: &Value, route: &str) -> Result<RequestView, &'static str> {
            if route != "/v1/completions" {
                return Err("unsupported_feature_route");
            }
            Ok(RequestView {
                request_id: request["request_id"].as_str().ok_or("request_id")?.into(),
                identity: "qwen14b-profile-a".into(),
                block_hashes: vec!["ab".repeat(32), "ac".repeat(32), "ad".repeat(32)].into(),
                exact: true,
            })
        }
    }

    #[derive(Debug)]
    struct SnapshotFixture {
        values: Vec<Candidate>,
    }
    impl SnapshotPort for SnapshotFixture {
        fn candidates(
            &self,
            _: &RequestView,
            _: &[String],
        ) -> Result<Vec<Candidate>, &'static str> {
            Ok(self.values.clone())
        }
    }

    #[derive(Debug)]
    struct Permit;
    impl Lease for Permit {
        fn before_send(&self) -> Result<(), &'static str> {
            Ok(())
        }
        fn sent(&self) {}
        fn headers(&self, _: u16) {}
        fn closed(&self, _: bool, _: CloseEvidence) {}
    }

    #[derive(Debug, Default)]
    struct AdmissionFixture {
        sequence: Mutex<u64>,
        reserved: Mutex<Vec<String>>,
    }
    impl AdmissionPort for AdmissionFixture {
        fn allocate_attempt(&self, request_id: &str) -> Result<String, &'static str> {
            let mut sequence = self.sequence.lock().unwrap();
            *sequence += 1;
            Ok(format!("runtime-a:{sequence}:{request_id}"))
        }
        fn reserve_async<'a>(
            &'a self,
            _: &'a RequestView,
            decision: &'a crate::runtime_provider::Decision,
        ) -> crate::prefix_extension::AdmissionFuture<'a> {
            Box::pin(async move {
                self.reserved
                    .lock()
                    .unwrap()
                    .push(decision.attempt_id.clone());
                Ok(Box::new(Permit) as Box<dyn Lease>)
            })
        }
    }

    fn candidate(url: &str, matched_blocks: Option<u32>, outstanding: u32) -> Candidate {
        Candidate {
            worker_url: url.into(),
            instance_epoch: format!("instance-{url}"),
            worker_version: 1,
            config_version: 3,
            healthy: true,
            draining: false,
            local_outstanding: outstanding,
            matched_blocks,
        }
    }

    fn make_provider(values: Vec<Candidate>) -> (RuntimeProvider, Arc<AdmissionFixture>) {
        let admission = Arc::new(AdmissionFixture::default());
        (
            RuntimeProvider::new(
                Arc::new(FeatureFixture),
                Arc::new(SnapshotFixture { values }),
                admission.clone(),
            ),
            admission,
        )
    }

    #[tokio::test]
    async fn known_cache_depth_then_load_selects_and_reserves_one_attempt() {
        let (provider, admission) = make_provider(vec![
            candidate("http://a", Some(1), 0),
            candidate("http://b", Some(3), 7),
        ]);
        let features = provider
            .prepare(&json!({"request_id":"request-a"}), "/v1/completions")
            .unwrap();
        let proposal = provider
            .select(
                features.as_ref(),
                &["http://a".into(), "http://b".into()],
                9,
            )
            .unwrap()
            .unwrap();
        assert_eq!(proposal.worker_url, "http://b");
        assert_eq!(proposal.registry_revision, 9);
        assert_eq!(
            provider.reserve(features.as_ref(), &proposal).unwrap_err(),
            "async_admission_required"
        );
        provider
            .reserve_async(features.as_ref(), &proposal)
            .await
            .unwrap();
        assert_eq!(
            admission.reserved.lock().unwrap().as_slice(),
            ["runtime-a:1:request-a"]
        );
    }

    #[test]
    fn all_unknown_cache_views_fall_back_to_stable_least_load() {
        let (provider, _) = make_provider(vec![
            candidate("http://b", None, 2),
            candidate("http://a", None, 2),
        ]);
        let features = provider
            .prepare(&json!({"request_id":"request-a"}), "/v1/completions")
            .unwrap();
        let proposal = provider
            .select(
                features.as_ref(),
                &["http://a".into(), "http://b".into()],
                1,
            )
            .unwrap()
            .unwrap();
        assert_eq!(proposal.worker_url, "http://a");
    }

    #[test]
    fn trusted_miss_and_unknown_both_use_base_load_without_fake_cache_score() {
        let (provider, _) = make_provider(vec![
            candidate("http://known-miss", Some(0), 9),
            candidate("http://unknown", None, 1),
        ]);
        let features = provider
            .prepare(&json!({"request_id":"request-a"}), "/v1/completions")
            .unwrap();
        let proposal = provider
            .select(
                features.as_ref(),
                &["http://known-miss".into(), "http://unknown".into()],
                1,
            )
            .unwrap()
            .unwrap();
        assert_eq!(proposal.worker_url, "http://unknown");
    }

    #[test]
    fn unhealthy_foreign_and_malformed_inputs_fail_before_admission() {
        let mut unhealthy = candidate("http://a", Some(3), 0);
        unhealthy.healthy = false;
        let (unhealthy_provider, admission) = make_provider(vec![unhealthy]);
        let features = unhealthy_provider
            .prepare(&json!({"request_id":"request-a"}), "/v1/completions")
            .unwrap();
        assert_eq!(
            unhealthy_provider
                .select(features.as_ref(), &["http://a".into()], 1)
                .unwrap_err(),
            "no_eligible_worker"
        );
        assert!(admission.reserved.lock().unwrap().is_empty());

        let (foreign, _) = make_provider(vec![candidate("http://foreign", Some(1), 0)]);
        let features = foreign
            .prepare(&json!({"request_id":"request-a"}), "/v1/completions")
            .unwrap();
        assert_eq!(
            foreign
                .select(features.as_ref(), &["http://a".into()], 1)
                .unwrap_err(),
            "snapshot_contains_foreign_worker"
        );
        assert!(foreign.prepare(&json!({}), "/v1/completions").is_err());
        assert!(foreign
            .prepare(
                &json!({"request_id":"request-a\nforged"}),
                "/v1/completions"
            )
            .is_err());
        assert!(foreign
            .prepare(&json!({"request_id":"request-a"}), "/v1/chat/completions")
            .is_err());
    }

    #[test]
    fn inconsistent_or_impossible_snapshot_is_rejected() {
        let mut too_deep = candidate("http://a", Some(4), 0);
        let (provider, _) = make_provider(vec![too_deep.clone()]);
        let features = provider
            .prepare(&json!({"request_id":"request-a"}), "/v1/completions")
            .unwrap();
        assert_eq!(
            provider
                .select(features.as_ref(), &["http://a".into()], 1)
                .unwrap_err(),
            "invalid_candidate_snapshot"
        );

        too_deep.matched_blocks = Some(1);
        let mut other = candidate("http://b", None, 0);
        other.config_version = 4;
        let (provider, _) = make_provider(vec![too_deep.clone(), other]);
        let features = provider
            .prepare(&json!({"request_id":"request-a"}), "/v1/completions")
            .unwrap();
        assert_eq!(
            provider
                .select(
                    features.as_ref(),
                    &["http://a".into(), "http://b".into()],
                    1,
                )
                .unwrap_err(),
            "mixed_config_snapshot"
        );

        let (provider, _) = make_provider(vec![too_deep.clone(), too_deep]);
        let features = provider
            .prepare(&json!({"request_id":"request-a"}), "/v1/completions")
            .unwrap();
        assert_eq!(
            provider
                .select(features.as_ref(), &["http://a".into()], 1)
                .unwrap_err(),
            "snapshot_contains_duplicate_worker"
        );
    }
}

mod runtime_owner {
    use crate::{
        prefix_extension::{CloseEvidence, HttpExtension, Lease},
        runtime_owner::{CachePort, RuntimeOwner, TerminalResult, WorkerControl},
        runtime_provider::{FeaturePort, RequestView, RuntimeProvider, SnapshotPort},
    };
    use serde_json::Value;
    use std::{collections::BTreeMap, sync::Arc};

    const HASH: &str = "abababababababababababababababababababababababababababababababab";

    #[derive(Debug)]
    struct Features;
    impl FeaturePort for Features {
        fn extract(&self, request: &Value, _: &str) -> Result<RequestView, &'static str> {
            Ok(RequestView {
                request_id: request["request_id"].as_str().ok_or("request_id")?.into(),
                identity: request["identity"].as_str().ok_or("identity")?.into(),
                block_hashes: Arc::from([HASH.into()]),
                exact: true,
            })
        }
    }

    #[derive(Debug)]
    struct Cache(BTreeMap<(String, String), Option<u32>>);
    impl CachePort for Cache {
        fn lookup(
            &self,
            worker_url: &str,
            instance_epoch: &str,
            _: &str,
            _: &[String],
        ) -> Option<u32> {
            self.0
                .get(&(worker_url.into(), instance_epoch.into()))
                .copied()
                .flatten()
        }
    }

    fn worker(url: &str, epoch: &str, capacity: u32) -> WorkerControl {
        WorkerControl {
            worker_url: url.into(),
            instance_epoch: epoch.into(),
            worker_version: 1,
            cache_identity: "qwen-profile".into(),
            healthy: true,
            draining: false,
            capacity,
        }
    }

    fn owner(capacity: u32) -> Arc<RuntimeOwner> {
        let cache = Arc::new(Cache(BTreeMap::from([
            (("http://a".into(), "a-1".into()), Some(0)),
            (("http://b".into(), "b-1".into()), Some(1)),
        ])));
        Arc::new(
            RuntimeOwner::new(
                cache,
                "process-a".into(),
                1,
                vec![
                    worker("http://a", "a-1", capacity),
                    worker("http://b", "b-1", capacity),
                ],
                8,
                8,
            )
            .unwrap(),
        )
    }

    fn provider(owner: &Arc<RuntimeOwner>) -> RuntimeProvider {
        RuntimeProvider::new(Arc::new(Features), owner.clone(), owner.clone())
    }

    fn prepared(
        provider: &RuntimeProvider,
        request_id: &str,
    ) -> Arc<dyn crate::prefix_extension::Features> {
        provider
            .prepare(
                &serde_json::json!({"request_id": request_id, "identity": "qwen-profile"}),
                "/v1/completions",
            )
            .unwrap()
    }

    async fn reserve(
        provider: &RuntimeProvider,
        features: &dyn crate::prefix_extension::Features,
    ) -> Box<dyn Lease> {
        let proposal = provider
            .select(features, &["http://a".into(), "http://b".into()], 1)
            .unwrap()
            .unwrap();
        assert_eq!(proposal.worker_url, "http://b");
        provider.reserve_async(features, &proposal).await.unwrap()
    }

    #[tokio::test]
    async fn sent_transport_close_retains_debt_until_reliable_terminal() {
        let owner = owner(2);
        let provider = provider(&owner);
        let features = prepared(&provider, "request-a");
        let lease = reserve(&provider, features.as_ref()).await;
        let attempt = lease.request_id_header().unwrap().to_owned();
        lease.before_send().unwrap();
        lease.sent();
        lease.closed(true, CloseEvidence::TransportEof);
        assert_eq!(owner.active_len().unwrap(), 1);
        let candidates = owner
            .candidates(
                features.as_any().downcast_ref::<RequestView>().unwrap(),
                &["http://a".into(), "http://b".into()],
            )
            .unwrap();
        assert_eq!(
            candidates
                .iter()
                .find(|candidate| candidate.worker_url == "http://b")
                .unwrap()
                .local_outstanding,
            1
        );
        assert_eq!(
            owner.apply_terminal(&attempt, "http://b", "b-1").unwrap(),
            TerminalResult::Applied
        );
        assert_eq!(owner.active_len().unwrap(), 0);
        assert_eq!(
            owner.apply_terminal(&attempt, "http://b", "b-1").unwrap(),
            TerminalResult::Duplicate
        );
    }

    #[tokio::test]
    async fn definitely_not_sent_releases_but_sent_error_does_not() {
        let owner = owner(2);
        let provider = provider(&owner);
        let first = prepared(&provider, "request-a");
        let lease = reserve(&provider, first.as_ref()).await;
        lease.closed(false, CloseEvidence::NotSent);
        assert_eq!(owner.active_len().unwrap(), 0);

        let second = prepared(&provider, "request-b");
        let lease = reserve(&provider, second.as_ref()).await;
        lease.before_send().unwrap();
        lease.sent();
        lease.closed(true, CloseEvidence::TransportError);
        assert_eq!(owner.active_len().unwrap(), 1);
    }

    #[tokio::test]
    async fn changed_configuration_fences_before_send_and_reclaims_reservation() {
        let owner = owner(2);
        let provider = provider(&owner);
        let features = prepared(&provider, "request-a");
        let lease = reserve(&provider, features.as_ref()).await;
        let mut a = worker("http://a", "a-1", 2);
        let mut b = worker("http://b", "b-1", 2);
        a.worker_version = 2;
        b.worker_version = 2;
        owner.publish(2, vec![a, b]).unwrap();
        assert_eq!(lease.before_send(), Err("decision_fence_changed"));
        assert_eq!(owner.active_len().unwrap(), 0);
    }

    #[tokio::test]
    async fn restart_keeps_old_debt_and_uses_a_new_attempt_namespace() {
        let first = owner(1);
        let first_provider = provider(&first);
        let features = prepared(&first_provider, "request-old");
        let lease = reserve(&first_provider, features.as_ref()).await;
        let old_attempt = lease.request_id_header().unwrap().to_owned();
        lease.before_send().unwrap();
        lease.sent();
        lease.closed(true, CloseEvidence::TransportDropped);
        let checkpoint = first.checkpoint().unwrap();
        assert!(RuntimeOwner::restore(
            Arc::new(Cache(BTreeMap::new())),
            "process-a".into(),
            checkpoint.clone(),
            8,
            8,
        )
        .is_err());
        let restored = Arc::new(
            RuntimeOwner::restore(
                Arc::new(Cache(BTreeMap::from([
                    (("http://a".into(), "a-1".into()), Some(0)),
                    (("http://b".into(), "b-1".into()), Some(1)),
                ]))),
                "process-b".into(),
                checkpoint,
                8,
                8,
            )
            .unwrap(),
        );
        let restored_provider = provider(&restored);
        let next = prepared(&restored_provider, "request-new");
        let proposal = restored_provider
            .select(next.as_ref(), &["http://a".into(), "http://b".into()], 1)
            .unwrap()
            .unwrap();
        assert_eq!(
            restored_provider
                .reserve_async(next.as_ref(), &proposal)
                .await
                .unwrap_err(),
            "worker_capacity"
        );
        assert_eq!(
            restored
                .apply_terminal(&old_attempt, "http://b", "b-1")
                .unwrap(),
            TerminalResult::Applied
        );
        let permit = restored_provider
            .reserve_async(next.as_ref(), &proposal)
            .await
            .unwrap();
        assert!(permit
            .request_id_header()
            .unwrap()
            .starts_with("prm-process-b-"));
    }

    #[tokio::test]
    async fn restart_treats_pre_send_reservation_as_uncertain_debt() {
        let first = owner(2);
        let first_provider = provider(&first);
        let features = prepared(&first_provider, "request-old");
        let lease = reserve(&first_provider, features.as_ref()).await;
        let attempt = lease.request_id_header().unwrap().to_owned();
        let restored = RuntimeOwner::restore(
            Arc::new(Cache(BTreeMap::new())),
            "process-b".into(),
            first.checkpoint().unwrap(),
            8,
            8,
        )
        .unwrap();
        assert_eq!(restored.active_len().unwrap(), 1);
        assert_eq!(
            restored
                .apply_terminal(&attempt, "http://b", "b-1")
                .unwrap(),
            TerminalResult::Applied
        );
        assert_eq!(restored.active_len().unwrap(), 0);
    }

    #[tokio::test]
    async fn conflicting_terminal_fails_owner_without_releasing_debt() {
        let owner = owner(2);
        let provider = provider(&owner);
        let features = prepared(&provider, "request-a");
        let lease = reserve(&provider, features.as_ref()).await;
        let attempt = lease.request_id_header().unwrap().to_owned();
        lease.before_send().unwrap();
        lease.sent();
        assert_eq!(
            owner.apply_terminal(&attempt, "http://a", "a-1"),
            Err("terminal_identity_conflict")
        );
        assert_eq!(owner.active_len().unwrap(), 1);
        assert!(owner
            .candidates(
                features.as_any().downcast_ref::<RequestView>().unwrap(),
                &["http://a".into(), "http://b".into()],
            )
            .is_err());
    }

    #[test]
    fn identity_mismatch_is_unknown_and_publication_is_monotonic() {
        let owner = owner(2);
        let features = RequestView {
            request_id: "request".into(),
            identity: "other-profile".into(),
            block_hashes: Arc::from([HASH.into()]),
            exact: true,
        };
        let candidates = owner
            .candidates(&features, &["http://a".into(), "http://b".into()])
            .unwrap();
        assert!(candidates
            .iter()
            .all(|candidate| candidate.matched_blocks.is_none()));
        assert_eq!(
            owner.publish(1, vec![worker("http://a", "a-1", 2)]),
            Err("config_version_not_monotonic")
        );
    }
}

mod durable_owner {
    use crate::{
        durable_runtime::{DurableRuntime, TerminalRecord},
        prefix_extension::{CloseEvidence, Lease},
        runtime_owner::{CachePort, RuntimeOwner, WorkerControl},
        runtime_provider::{AdmissionPort, Decision, RequestView, SnapshotPort},
        runtime_store::{RuntimeStore, StoreError, StoredRuntime, TerminalCursor},
        sqlite_runtime_store::{SqliteRuntimeStore, DEFAULT_MAX_SNAPSHOT_BYTES},
    };
    use std::{
        fs,
        path::{Path, PathBuf},
        sync::{
            atomic::{AtomicBool, AtomicU8, Ordering},
            Arc, Mutex,
        },
        time::{Duration, SystemTime, UNIX_EPOCH},
    };

    #[derive(Debug)]
    struct EmptyCache;
    impl CachePort for EmptyCache {
        fn lookup(&self, _: &str, _: &str, _: &str, _: &[String]) -> Option<u32> {
            None
        }
    }

    fn worker() -> WorkerControl {
        WorkerControl {
            worker_url: "http://worker-a".into(),
            instance_epoch: "instance-a".into(),
            worker_version: 1,
            cache_identity: "qwen-profile".into(),
            healthy: true,
            draining: false,
            capacity: 4,
        }
    }

    fn owner(epoch: &str) -> Arc<RuntimeOwner> {
        Arc::new(
            RuntimeOwner::new(
                Arc::new(EmptyCache),
                epoch.into(),
                1,
                vec![worker()],
                16,
                16,
            )
            .unwrap(),
        )
    }

    fn request(id: &str) -> RequestView {
        RequestView {
            request_id: id.into(),
            identity: "qwen-profile".into(),
            block_hashes: Arc::from([]),
            exact: true,
        }
    }

    fn decision(runtime: &DurableRuntime, request: &RequestView) -> Decision {
        Decision {
            request_id: request.request_id.clone(),
            attempt_id: runtime.allocate_attempt(&request.request_id).unwrap(),
            worker_url: "http://worker-a".into(),
            instance_epoch: "instance-a".into(),
            worker_version: 1,
            config_version: 1,
        }
    }

    fn cursor(through: u64) -> TerminalCursor {
        TerminalCursor {
            source_id: "terminal-source-a".into(),
            worker_url: "http://worker-a".into(),
            instance_epoch: "instance-a".into(),
            observer_epoch: "observer-a".into(),
            through,
        }
    }

    #[derive(Debug, Default)]
    struct MemoryStore {
        value: Mutex<Option<StoredRuntime>>,
        // 0 normal; 1 fail before commit; 2 commit then return uncertain error.
        fault: AtomicU8,
        block_next: AtomicBool,
        block_entered: AtomicBool,
        block_release: AtomicBool,
        block_finished: AtomicBool,
    }

    impl MemoryStore {
        fn fault(&self, mode: u8) {
            self.fault.store(mode, Ordering::Release);
        }

        fn block_next_commit(&self) {
            self.block_entered.store(false, Ordering::Release);
            self.block_release.store(false, Ordering::Release);
            self.block_finished.store(false, Ordering::Release);
            self.block_next.store(true, Ordering::Release);
        }

        fn release_commit(&self) {
            self.block_release.store(true, Ordering::Release);
        }

        fn replace(
            &self,
            expected_owner: &str,
            expected_generation: u64,
            next: &StoredRuntime,
            takeover: bool,
        ) -> Result<(), StoreError> {
            next.validate()?;
            if self.block_next.swap(false, Ordering::AcqRel) {
                self.block_entered.store(true, Ordering::Release);
                while !self.block_release.load(Ordering::Acquire) {
                    std::thread::sleep(Duration::from_millis(1));
                }
                self.block_finished.store(true, Ordering::Release);
            }
            let mut value = self.value.lock().map_err(|_| StoreError::Busy)?;
            let current = value.as_ref().ok_or(StoreError::Missing)?;
            if current.owner_epoch != expected_owner
                || current.generation != expected_generation
                || next.generation != expected_generation + 1
                || (!takeover && next.owner_epoch != expected_owner)
                || (takeover && next.owner_epoch == expected_owner)
            {
                return Err(StoreError::Fenced);
            }
            if self.fault.load(Ordering::Acquire) == 1 {
                return Err(StoreError::Storage(Box::new(std::io::Error::other(
                    "injected before commit",
                ))));
            }
            *value = Some(next.clone());
            if self.fault.load(Ordering::Acquire) == 2 {
                return Err(StoreError::Storage(Box::new(std::io::Error::other(
                    "injected after commit",
                ))));
            }
            Ok(())
        }
    }

    impl RuntimeStore for MemoryStore {
        fn initialize(&self, initial: &StoredRuntime) -> Result<(), StoreError> {
            initial.validate()?;
            let mut value = self.value.lock().map_err(|_| StoreError::Busy)?;
            if value.is_some() {
                return Err(StoreError::AlreadyExists);
            }
            *value = Some(initial.clone());
            Ok(())
        }

        fn load(&self) -> Result<StoredRuntime, StoreError> {
            self.value
                .lock()
                .map_err(|_| StoreError::Busy)?
                .clone()
                .ok_or(StoreError::Missing)
        }

        fn commit(
            &self,
            expected_owner: &str,
            expected_generation: u64,
            next: &StoredRuntime,
        ) -> Result<(), StoreError> {
            self.replace(expected_owner, expected_generation, next, false)
        }

        fn takeover(
            &self,
            expected_owner: &str,
            expected_generation: u64,
            next: &StoredRuntime,
        ) -> Result<(), StoreError> {
            self.replace(expected_owner, expected_generation, next, true)
        }
    }

    async fn reserve(runtime: &DurableRuntime, id: &str) -> Box<dyn Lease> {
        let request = request(id);
        let decision = decision(runtime, &request);
        runtime.reserve_async(&request, &decision).await.unwrap()
    }

    async fn wait_for(flag: &AtomicBool) {
        tokio::time::timeout(Duration::from_secs(2), async {
            while !flag.load(Ordering::Acquire) {
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .expect("blocking store transition did not reach the expected phase");
    }

    #[tokio::test]
    async fn permit_follows_commit_and_terminal_cursor_is_atomic() {
        let store = Arc::new(MemoryStore::default());
        let runtime = DurableRuntime::create(owner("process-a"), store.clone()).unwrap();
        let lease = reserve(&runtime, "request-a").await;
        let attempt = lease.request_id_header().unwrap().to_owned();
        let after_reserve = store.load().unwrap();
        assert_eq!(after_reserve.generation, 2);
        assert_eq!(after_reserve.checkpoint.active.len(), 1);

        lease.before_send().unwrap();
        lease.sent();
        lease.closed(true, CloseEvidence::TransportEof);
        assert_eq!(
            runtime
                .apply_terminal_batch(
                    cursor(0),
                    &[TerminalRecord {
                        sequence: 1,
                        attempt_id: attempt,
                        worker_url: "http://worker-a".into(),
                        instance_epoch: "instance-a".into(),
                    }],
                )
                .await
                .unwrap(),
            1
        );
        let committed = store.load().unwrap();
        assert_eq!(committed.generation, 3);
        assert!(committed.checkpoint.active.is_empty());
        assert_eq!(committed.terminal_cursors, vec![cursor(1)]);
    }

    #[tokio::test]
    async fn uncertain_reservation_commit_never_returns_a_send_permit() {
        for fault in [1, 2] {
            let store = Arc::new(MemoryStore::default());
            let runtime = DurableRuntime::create(owner("process-a"), store.clone()).unwrap();
            store.fault(fault);
            let request = request("request-a");
            let selected = decision(&runtime, &request);
            assert_eq!(
                runtime
                    .reserve_async(&request, &selected)
                    .await
                    .unwrap_err(),
                "durable_store_failed"
            );
            assert!(!runtime.admission_ready());
            assert_eq!(runtime.candidates(&request, &[]), Err("runtime_paused"));
            let persisted = store.load().unwrap();
            assert_eq!(persisted.checkpoint.active.len(), usize::from(fault == 2));
        }
    }

    #[tokio::test]
    async fn terminal_commit_failure_pauses_and_recovery_retains_debt() {
        let store = Arc::new(MemoryStore::default());
        let runtime = DurableRuntime::create(owner("process-a"), store.clone()).unwrap();
        let lease = reserve(&runtime, "request-a").await;
        let attempt = lease.request_id_header().unwrap().to_owned();
        lease.before_send().unwrap();
        lease.sent();
        store.fault(1);
        assert_eq!(
            runtime
                .apply_terminal_batch(
                    cursor(0),
                    &[TerminalRecord {
                        sequence: 1,
                        attempt_id: attempt,
                        worker_url: "http://worker-a".into(),
                        instance_epoch: "instance-a".into(),
                    }],
                )
                .await,
            Err("durable_store_failed")
        );
        assert!(!runtime.admission_ready());
        assert_eq!(store.load().unwrap().checkpoint.active.len(), 1);
        store.fault(0);
        let recovered =
            DurableRuntime::recover(Arc::new(EmptyCache), "process-b".into(), store, 16, 16)
                .unwrap();
        assert!(!recovered.admission_ready());
        assert_eq!(recovered.owner().active_len().unwrap(), 1);
    }

    #[tokio::test]
    async fn terminal_source_binding_cannot_change_at_an_existing_cursor() {
        let store = Arc::new(MemoryStore::default());
        let runtime = DurableRuntime::create(owner("process-a"), store.clone()).unwrap();

        let first = reserve(&runtime, "request-a").await;
        let first_attempt = first.request_id_header().unwrap().to_owned();
        first.before_send().unwrap();
        first.sent();
        runtime
            .apply_terminal_batch(
                cursor(0),
                &[TerminalRecord {
                    sequence: 1,
                    attempt_id: first_attempt,
                    worker_url: "http://worker-a".into(),
                    instance_epoch: "instance-a".into(),
                }],
            )
            .await
            .unwrap();

        let second = reserve(&runtime, "request-b").await;
        let second_attempt = second.request_id_header().unwrap().to_owned();
        second.before_send().unwrap();
        second.sent();
        let mut changed_source = cursor(1);
        changed_source.observer_epoch = "observer-b".into();
        assert_eq!(
            runtime
                .apply_terminal_batch(
                    changed_source,
                    &[TerminalRecord {
                        sequence: 2,
                        attempt_id: second_attempt,
                        worker_url: "http://worker-a".into(),
                        instance_epoch: "instance-a".into(),
                    }],
                )
                .await,
            Err("terminal_source_conflict")
        );
        assert!(!runtime.admission_ready());
        assert_eq!(runtime.owner().active_len().unwrap(), 1);
        let persisted = store.load().unwrap();
        assert_eq!(persisted.checkpoint.active.len(), 1);
        assert_eq!(persisted.terminal_cursors, vec![cursor(1)]);
    }

    #[tokio::test]
    async fn cancelling_reservation_commit_retires_the_runtime_instance() {
        let store = Arc::new(MemoryStore::default());
        let runtime = Arc::new(DurableRuntime::create(owner("process-a"), store.clone()).unwrap());
        let request = request("request-a");
        let selected = decision(&runtime, &request);
        store.block_next_commit();

        let task = tokio::spawn({
            let runtime = runtime.clone();
            async move { runtime.reserve_async(&request, &selected).await }
        });
        wait_for(&store.block_entered).await;
        task.abort();
        let _ = task.await;
        assert!(!runtime.admission_ready());
        assert_eq!(runtime.allocate_attempt("request-b"), Err("runtime_paused"));

        store.release_commit();
        wait_for(&store.block_finished).await;
        assert_eq!(store.load().unwrap().checkpoint.active.len(), 1);
    }

    #[tokio::test]
    async fn cancelling_terminal_commit_retires_the_runtime_instance() {
        let store = Arc::new(MemoryStore::default());
        let runtime = Arc::new(DurableRuntime::create(owner("process-a"), store.clone()).unwrap());
        let lease = reserve(&runtime, "request-a").await;
        let attempt = lease.request_id_header().unwrap().to_owned();
        lease.before_send().unwrap();
        lease.sent();
        store.block_next_commit();

        let task = tokio::spawn({
            let runtime = runtime.clone();
            async move {
                runtime
                    .apply_terminal_batch(
                        cursor(0),
                        &[TerminalRecord {
                            sequence: 1,
                            attempt_id: attempt,
                            worker_url: "http://worker-a".into(),
                            instance_epoch: "instance-a".into(),
                        }],
                    )
                    .await
            }
        });
        wait_for(&store.block_entered).await;
        task.abort();
        let _ = task.await;
        assert!(!runtime.admission_ready());
        assert_eq!(runtime.allocate_attempt("request-b"), Err("runtime_paused"));

        store.release_commit();
        wait_for(&store.block_finished).await;
        let persisted = store.load().unwrap();
        assert!(persisted.checkpoint.active.is_empty());
        assert_eq!(persisted.terminal_cursors, vec![cursor(1)]);
    }

    #[tokio::test]
    async fn recovery_is_paused_until_explicit_same_instance_takeover() {
        let store = Arc::new(MemoryStore::default());
        let first = DurableRuntime::create(owner("process-a"), store.clone()).unwrap();
        let lease = reserve(&first, "request-old").await;
        lease.before_send().unwrap();
        lease.sent();
        drop(first);

        let recovered = DurableRuntime::recover(
            Arc::new(EmptyCache),
            "process-b".into(),
            store.clone(),
            16,
            16,
        )
        .unwrap();
        assert!(!recovered.admission_ready());
        assert_eq!(
            recovered.resume(2, vec![worker()], false).await,
            Err("runtime_takeover_not_allowed")
        );
        let mut foreign = worker();
        foreign.instance_epoch = "instance-b".into();
        assert_eq!(
            recovered.resume(2, vec![foreign], true).await,
            Err("runtime_takeover_identity")
        );
        recovered.resume(2, vec![worker()], true).await.unwrap();
        assert!(recovered.admission_ready());
        let persisted = store.load().unwrap();
        assert_eq!(persisted.owner_epoch, "process-b");
        assert_eq!(persisted.checkpoint.active.len(), 1);
        assert_eq!(persisted.checkpoint.workers[0].worker_version, 1);
    }

    struct TempDirectory(PathBuf);
    impl TempDirectory {
        fn new(name: &str) -> Self {
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let path = std::env::temp_dir().join(format!(
                "prefix-routing-{name}-{}-{nonce}",
                std::process::id()
            ));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
        fn join(&self, name: &str) -> PathBuf {
            self.0.join(name)
        }
    }
    impl Drop for TempDirectory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn initial(epoch: &str) -> StoredRuntime {
        let checkpoint = owner(epoch).checkpoint().unwrap();
        StoredRuntime {
            schema_version: 1,
            owner_epoch: epoch.into(),
            generation: 1,
            checkpoint,
            terminal_cursors: vec![],
        }
    }

    fn sqlite(path: &Path) -> SqliteRuntimeStore {
        SqliteRuntimeStore::create(path, DEFAULT_MAX_SNAPSHOT_BYTES).unwrap()
    }

    #[test]
    fn sqlite_store_is_create_once_bounded_and_owner_fenced() {
        let directory = TempDirectory::new("sqlite-fence");
        let path = directory.join("runtime.sqlite");
        let store = sqlite(&path);
        store.initialize(&initial("process-a")).unwrap();
        assert!(matches!(
            store.initialize(&initial("process-a")),
            Err(StoreError::AlreadyExists)
        ));
        assert!(matches!(
            SqliteRuntimeStore::create(&path, DEFAULT_MAX_SNAPSHOT_BYTES),
            Err(StoreError::AlreadyExists)
        ));

        let mut next = store.load().unwrap();
        next.generation = 2;
        store.commit("process-a", 1, &next).unwrap();
        let mut takeover = next.clone();
        takeover.owner_epoch = "process-b".into();
        takeover.checkpoint.process_epoch = "process-b".into();
        takeover.generation = 3;
        store.takeover("process-a", 2, &takeover).unwrap();
        let mut stale = next;
        stale.generation = 3;
        assert!(matches!(
            store.commit("process-a", 2, &stale),
            Err(StoreError::Fenced)
        ));
        drop(store);

        let reopened = SqliteRuntimeStore::open(&path, DEFAULT_MAX_SNAPSHOT_BYTES).unwrap();
        assert_eq!(reopened.load().unwrap(), takeover);
    }

    #[test]
    fn sqlite_store_detects_payload_corruption_and_snapshot_overflow() {
        let directory = TempDirectory::new("sqlite-corrupt");
        let path = directory.join("runtime.sqlite");
        let store = sqlite(&path);
        store.initialize(&initial("process-a")).unwrap();
        drop(store);
        let connection = rusqlite::Connection::open(&path).unwrap();
        connection
            .execute("UPDATE runtime SET payload=x'00' WHERE id=1", [])
            .unwrap();
        drop(connection);
        let reopened = SqliteRuntimeStore::open(&path, DEFAULT_MAX_SNAPSHOT_BYTES).unwrap();
        assert!(matches!(
            reopened.load(),
            Err(StoreError::Invalid("runtime_snapshot_digest_mismatch"))
        ));

        let small_path = directory.join("small.sqlite");
        let small = SqliteRuntimeStore::create(&small_path, 1024).unwrap();
        let mut oversized = initial("process-a");
        oversized.checkpoint.retired = (0..128).map(|index| format!("attempt-{index}")).collect();
        assert!(matches!(
            small.initialize(&oversized),
            Err(StoreError::Invalid("runtime_snapshot_too_large"))
        ));
    }

    #[test]
    fn sqlite_missing_store_never_bootstraps_empty() {
        let directory = TempDirectory::new("sqlite-missing");
        assert!(matches!(
            SqliteRuntimeStore::open(directory.join("missing.sqlite"), DEFAULT_MAX_SNAPSHOT_BYTES),
            Err(StoreError::Missing)
        ));
    }
}
