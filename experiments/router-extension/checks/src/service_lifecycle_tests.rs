// SPDX-License-Identifier: Apache-2.0
use super::*;
use crate::{
    runtime_owner::control_gate::{ControlSet, MonotonicClock, SourceKey, WorkerGate},
    service_lifecycle::{self, Phase, ServiceSupervisor, Shutdown, TrackedBody},
};
use axum::{body::Body, routing::get, Router};
use http_body::Body as _;
use std::{collections::BTreeMap, pin::Pin};

fn install_controls(f: &Fixture) -> Arc<ControlSet> {
    install_controls_for(f, "observer-a", "exclusive_router_v1")
}

fn install_controls_for(f: &Fixture, observer: &str, ownership: &str) -> Arc<ControlSet> {
    let identity = SourceKey {
        instance: "instance-a".into(),
        observer: observer.into(),
        ownership: ownership.into(),
    };
    let gate = Arc::new(
        WorkerGate::new(
            identity.clone(),
            Arc::new(MonotonicClock::default()),
            60_000_000_000,
        )
        .unwrap(),
    );
    gate.complete(gate.begin().unwrap(), &identity).unwrap();
    let controls = Arc::new(ControlSet(BTreeMap::from([(
        "http://worker-a".into(),
        gate,
    )])));
    f.runtime
        .owner()
        .install_controls(controls.clone())
        .unwrap();
    controls
}

fn service(
    f: &Fixture,
    records: Vec<TerminalRecord>,
    max_http: usize,
) -> ServiceSupervisor<Transport> {
    let controls = install_controls(f);
    let driver = TerminalDriver::new(
        f.runtime.clone(),
        Transport::new(f.store.clone(), records),
        cursor(0),
        Ownership::ExclusiveRouterV1,
    )
    .unwrap();
    ServiceSupervisor::start(
        f.runtime.clone(),
        controls,
        vec![driver],
        Duration::from_millis(10),
        Duration::from_secs(5),
        max_http,
    )
    .unwrap()
}
async fn ready(state: &Arc<service_lifecycle::ServiceState>) {
    tokio::time::timeout(Duration::from_secs(2), async {
        while !state.ready() {
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn readiness_requires_listening_confirmation_and_fresh_control() {
    let f = fixture();
    let supervisor = service(&f, vec![], 4);
    let state = supervisor.state.clone();
    assert!(!state.ready());
    assert!(state.enter().is_err());
    state.listening().unwrap();
    ready(&state).await;
    assert!(state.listening().is_err());
    let work = state.enter().unwrap();
    assert_eq!(state.http_inflight(), Ok(1));
    drop(work);
    assert_eq!(state.http_inflight(), Ok(0));
    state.begin_drain(Duration::from_secs(1)).unwrap();
    assert!(!state.ready());
    assert!(state.enter().is_err());
    assert_eq!(
        supervisor.shutdown(Duration::from_secs(2)).await,
        Shutdown::Clean
    );
}

#[tokio::test]
async fn http_capacity_and_body_drop_do_not_release_backend_debt() {
    let f = fixture();
    let supervisor = service(&f, vec![], 1);
    let state = supervisor.state.clone();
    state.listening().unwrap();
    ready(&state).await;
    let _record = sent(&f.runtime, "still-running", 1).await;
    let body = TrackedBody::new(
        Body::from_stream(futures_util::stream::pending::<
            Result<bytes::Bytes, std::io::Error>,
        >()),
        state.enter().unwrap(),
    );
    assert!(matches!(state.enter(), Err("http_capacity")));
    drop(body);
    assert_eq!(state.http_inflight(), Ok(0));
    assert_eq!(f.runtime.owner().active_len(), Ok(1));
    assert_eq!(
        supervisor.shutdown(Duration::from_millis(25)).await,
        Shutdown::TimedOut
    );
    assert_eq!(state.phase(), Phase::Failed);
    assert_eq!(f.store.load().unwrap().checkpoint.active.len(), 1);
    let recovered = DurableRuntime::recover(
        Arc::new(EmptyCache),
        "process-b".into(),
        f.store.clone(),
        16,
        16,
    )
    .unwrap();
    assert!(!recovered.admission_ready());
    assert_eq!(recovered.owner().active_len(), Ok(1));
}

#[tokio::test]
async fn drain_waits_for_http_even_with_zero_backend_debt() {
    let f = fixture();
    let supervisor = service(&f, vec![], 4);
    let state = supervisor.state.clone();
    state.listening().unwrap();
    ready(&state).await;
    let work = state.enter().unwrap();
    let shutdown = tokio::spawn(supervisor.shutdown(Duration::from_secs(1)));
    tokio::time::sleep(Duration::from_millis(20)).await;
    assert!(!shutdown.is_finished());
    assert!(!state.ready());
    drop(work);
    assert_eq!(shutdown.await.unwrap(), Shutdown::Clean);
}

#[tokio::test]
async fn repeated_drain_does_not_extend_deadline_and_timeout_keeps_http_visible() {
    let f = fixture();
    let supervisor = service(&f, vec![], 4);
    let state = supervisor.state.clone();
    state.listening().unwrap();
    ready(&state).await;
    let work = state.enter().unwrap();
    let deadline = state.begin_drain(Duration::from_millis(10)).unwrap();
    assert_eq!(state.begin_drain(Duration::from_secs(300)), Ok(deadline));
    assert_eq!(
        supervisor.shutdown(Duration::from_secs(300)).await,
        Shutdown::TimedOut
    );
    assert_eq!(state.http_inflight(), Ok(1));
    drop(work);
    assert_eq!(state.http_inflight(), Ok(0));
    assert!(state.listening().is_err());
}

#[tokio::test]
async fn dropping_or_cancelling_supervision_fails_closed() {
    for cancel in [false, true] {
        let f = fixture();
        let supervisor = service(&f, vec![], 4);
        let state = supervisor.state.clone();
        state.listening().unwrap();
        ready(&state).await;
        let work = state.enter().unwrap();
        if cancel {
            let task = tokio::spawn(supervisor.shutdown(Duration::from_secs(60)));
            tokio::time::sleep(Duration::from_millis(10)).await;
            task.abort();
            assert!(task.await.is_err());
        } else {
            drop(supervisor);
        }
        assert!(!state.ready());
        assert!(f.runtime.owner().is_failed());
        drop(work);
    }
}

#[tokio::test]
async fn empty_or_duplicate_source_coverage_is_rejected() {
    let f = fixture();
    let controls = Arc::new(ControlSet(BTreeMap::new()));
    assert!(ServiceSupervisor::<Transport>::start(
        f.runtime.clone(),
        controls,
        vec![],
        Duration::from_millis(10),
        Duration::from_secs(1),
        4
    )
    .is_err());
    assert!(f.runtime.owner().is_failed());
    let f = fixture();
    let supervisor = service(&f, vec![], 4);
    // A second supervisor cannot substitute a different control set, even with
    // identical source strings. No new consumers may be started on rejection.
    let driver = TerminalDriver::new(
        f.runtime.clone(),
        Transport::new(f.store.clone(), vec![]),
        cursor(0),
        Ownership::ExclusiveRouterV1,
    )
    .unwrap();
    assert!(ServiceSupervisor::start(
        f.runtime.clone(),
        Arc::new(ControlSet(BTreeMap::new())),
        vec![driver],
        Duration::from_millis(10),
        Duration::from_secs(1),
        4
    )
    .is_err());
    drop(supervisor);
}

#[tokio::test]
async fn real_http_counts_stream_lifetime_and_rejects_during_drain() {
    let f = fixture();
    let supervisor = service(&f, vec![], 4);
    let state = supervisor.state.clone();
    let app = Router::new()
        .route(
            "/stream",
            get(|| async {
                Body::from_stream(
                    futures_util::stream::once(async {
                        Ok::<_, std::io::Error>(bytes::Bytes::from_static(b"data: first\n\n"))
                    })
                    .chain(futures_util::stream::pending()),
                )
            }),
        )
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            service_lifecycle::track_http,
        ))
        .route(
            "/readyz",
            get(service_lifecycle::readiness).with_state(state.clone()),
        );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let origin = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let client = reqwest::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap();
    assert_eq!(
        client
            .get(format!("{origin}/readyz"))
            .send()
            .await
            .unwrap()
            .status(),
        503
    );
    state.listening().unwrap();
    ready(&state).await;
    assert_eq!(
        client
            .get(format!("{origin}/readyz"))
            .send()
            .await
            .unwrap()
            .status(),
        200
    );
    assert_eq!(state.http_inflight(), Ok(0));
    let mut response = client.get(format!("{origin}/stream")).send().await.unwrap();
    assert!(!response.chunk().await.unwrap().unwrap().is_empty());
    assert_eq!(state.http_inflight(), Ok(1));
    state.begin_drain(Duration::from_secs(1)).unwrap();
    assert_eq!(
        client
            .get(format!("{origin}/readyz"))
            .send()
            .await
            .unwrap()
            .status(),
        503
    );
    assert_eq!(
        client
            .get(format!("{origin}/stream"))
            .send()
            .await
            .unwrap()
            .status(),
        503
    );
    drop(response);
    assert_eq!(
        supervisor.shutdown(Duration::from_secs(1)).await,
        Shutdown::Clean
    );
    assert_eq!(state.http_inflight(), Ok(0));
    server.abort();
    let _ = server.await;
}

#[tokio::test]
async fn body_preserves_trailers_and_releases_on_error_or_eof() {
    use futures_util::TryStreamExt;
    struct Frames(VecDeque<Result<http_body::Frame<bytes::Bytes>, std::io::Error>>);
    impl http_body::Body for Frames {
        type Data = bytes::Bytes;
        type Error = std::io::Error;
        fn poll_frame(
            mut self: Pin<&mut Self>,
            _: &mut std::task::Context<'_>,
        ) -> std::task::Poll<Option<Result<http_body::Frame<Self::Data>, Self::Error>>> {
            std::task::Poll::Ready(self.0.pop_front())
        }
    }
    let f = fixture();
    let supervisor = service(&f, vec![], 4);
    let state = supervisor.state.clone();
    state.listening().unwrap();
    ready(&state).await;
    let mut trailers = axum::http::HeaderMap::new();
    trailers.insert("x-end", "yes".parse().unwrap());
    let mut body = TrackedBody::new(
        Body::new(Frames(VecDeque::from([
            Ok(http_body::Frame::data(bytes::Bytes::from_static(b"hello"))),
            Ok(http_body::Frame::trailers(trailers.clone())),
        ]))),
        state.enter().unwrap(),
    );
    assert_eq!(
        futures_util::future::poll_fn(|cx| Pin::new(&mut body).poll_frame(cx))
            .await
            .unwrap()
            .unwrap()
            .into_data()
            .unwrap(),
        "hello"
    );
    assert_eq!(
        futures_util::future::poll_fn(|cx| Pin::new(&mut body).poll_frame(cx))
            .await
            .unwrap()
            .unwrap()
            .into_trailers()
            .unwrap(),
        trailers
    );
    assert!(
        futures_util::future::poll_fn(|cx| Pin::new(&mut body).poll_frame(cx))
            .await
            .is_none()
    );
    assert_eq!(state.http_inflight(), Ok(0));
    let body = TrackedBody::new(
        Body::new(Frames(VecDeque::from([Err(std::io::Error::other(
            "fixture",
        ))]))),
        state.enter().unwrap(),
    );
    assert!(Body::new(body)
        .into_data_stream()
        .try_collect::<Vec<_>>()
        .await
        .is_err());
    assert_eq!(state.http_inflight(), Ok(0));
    assert_eq!(
        supervisor.shutdown(Duration::from_secs(1)).await,
        Shutdown::Clean
    );
}

use futures_util::StreamExt;

#[tokio::test]
async fn service_rejects_cross_bound_observer_and_ownership_before_any_rpc() {
    for (observer, mode, ownership) in [
        (
            "other-observer",
            "exclusive_router_v1",
            Ownership::ExclusiveRouterV1,
        ),
        ("observer-a", "observe_only", Ownership::ExclusiveRouterV1),
        ("observer-a", "exclusive_router_v1", Ownership::ObserveOnly),
    ] {
        let f = fixture();
        let controls = install_controls_for(&f, observer, mode);
        let transport = Transport::new(f.store.clone(), vec![]);
        let calls = transport.calls.clone();
        let driver =
            TerminalDriver::new(f.runtime.clone(), transport, cursor(0), ownership).unwrap();
        assert!(matches!(
            ServiceSupervisor::start(
                f.runtime.clone(),
                controls,
                vec![driver],
                Duration::from_millis(10),
                Duration::from_secs(5),
                4
            ),
            Err("service_source_or_config_invalid")
        ));
        assert!(calls.lock().unwrap().values.is_empty());
        assert!(f.runtime.owner().is_failed());
    }
}

#[tokio::test]
async fn service_timeout_does_not_abort_a_committed_pending_confirmation() {
    let f = fixture();
    let record = sent(&f.runtime, "pending", 1).await;
    let controls = install_controls(&f);
    let entered = Arc::new(tokio::sync::Notify::new());
    let release = Arc::new(tokio::sync::Notify::new());
    let transport = HoldAcknowledgement {
        inner: Transport::new(f.store.clone(), vec![record]),
        entered: entered.clone(),
        release: release.clone(),
    };
    let calls = transport.inner.calls.clone();
    let driver = TerminalDriver::new(
        f.runtime.clone(),
        transport,
        cursor(0),
        Ownership::ExclusiveRouterV1,
    )
    .unwrap();
    let supervisor = ServiceSupervisor::start(
        f.runtime.clone(),
        controls,
        vec![driver],
        Duration::from_millis(10),
        Duration::from_secs(5),
        4,
    )
    .unwrap();
    tokio::time::timeout(Duration::from_secs(1), entered.notified())
        .await
        .unwrap();
    assert_eq!(f.runtime.owner().active_len(), Ok(0));
    assert_eq!(
        supervisor.shutdown(Duration::from_millis(20)).await,
        Shutdown::TimedOut
    );
    assert_eq!(f.store.load().unwrap().terminal_cursors, vec![cursor(1)]);
    release.notify_one();
    tokio::time::timeout(Duration::from_secs(1), async {
        while !calls.lock().unwrap().values.contains(&("retire", 1)) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn control_outage_revokes_readiness_and_new_proof_restores_it_but_not_after_drain() {
    let f = fixture();
    let controls = install_controls(&f);
    let driver = TerminalDriver::new(
        f.runtime.clone(),
        Transport::new(f.store.clone(), vec![]),
        cursor(0),
        Ownership::ExclusiveRouterV1,
    )
    .unwrap();
    let supervisor = ServiceSupervisor::start(
        f.runtime.clone(),
        controls.clone(),
        vec![driver],
        Duration::from_millis(10),
        Duration::from_secs(5),
        4,
    )
    .unwrap();
    let state = supervisor.state.clone();
    state.listening().unwrap();
    ready(&state).await;
    let gate = &controls.0["http://worker-a"];
    gate.reject(gate.begin().unwrap()).unwrap();
    assert!(!state.ready());
    assert!(state.enter().is_err());
    let identity = SourceKey {
        instance: "instance-a".into(),
        observer: "observer-a".into(),
        ownership: "exclusive_router_v1".into(),
    };
    gate.complete(gate.begin().unwrap(), &identity).unwrap();
    assert!(state.ready());
    let old_probe = gate.begin().unwrap();
    state.begin_drain(Duration::from_secs(1)).unwrap();
    assert!(gate.complete(old_probe, &identity).is_err());
    assert!(!state.ready());
    assert_eq!(
        supervisor.shutdown(Duration::from_secs(1)).await,
        Shutdown::Clean
    );
}

#[tokio::test]
async fn duplicate_consumer_and_paused_recovery_are_not_started() {
    let f = fixture();
    let controls = install_controls(&f);
    let make_driver = || {
        TerminalDriver::new(
            f.runtime.clone(),
            Transport::new(f.store.clone(), vec![]),
            cursor(0),
            Ownership::ExclusiveRouterV1,
        )
        .unwrap()
    };
    let supervisor = ServiceSupervisor::start(
        f.runtime.clone(),
        controls.clone(),
        vec![make_driver()],
        Duration::from_millis(10),
        Duration::from_secs(5),
        4,
    )
    .unwrap();
    assert!(matches!(
        ServiceSupervisor::start(
            f.runtime.clone(),
            controls.clone(),
            vec![make_driver()],
            Duration::from_millis(10),
            Duration::from_secs(5),
            4
        ),
        Err("service_already_claimed")
    ));
    assert!(!f.runtime.owner().is_failed());
    drop(supervisor);
    let recovered = Arc::new(
        DurableRuntime::recover(
            Arc::new(EmptyCache),
            "process-new".into(),
            f.store.clone(),
            16,
            16,
        )
        .unwrap(),
    );
    assert!(matches!(
        ServiceSupervisor::<Transport>::start(
            recovered.clone(),
            controls,
            vec![],
            Duration::from_millis(10),
            Duration::from_secs(5),
            4
        ),
        Err("service_recovery_required")
    ));
    assert!(!recovered.owner().is_failed());
    assert!(!recovered.admission_ready());
}

#[tokio::test]
async fn failed_terminal_source_never_looks_like_a_clean_shutdown() {
    let f = fixture();
    let controls = install_controls(&f);
    let driver = TerminalDriver::new(
        f.runtime.clone(),
        PanicPoll(Transport::new(f.store.clone(), vec![])),
        cursor(0),
        Ownership::ExclusiveRouterV1,
    )
    .unwrap();
    let supervisor = ServiceSupervisor::start(
        f.runtime.clone(),
        controls,
        vec![driver],
        Duration::from_millis(10),
        Duration::from_secs(5),
        4,
    )
    .unwrap();
    tokio::task::yield_now().await;
    assert!(!supervisor.state.ready());
    assert_eq!(
        supervisor.shutdown(Duration::from_secs(1)).await,
        Shutdown::Failed
    );
}
