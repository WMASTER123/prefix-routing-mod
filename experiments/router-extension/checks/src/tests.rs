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
