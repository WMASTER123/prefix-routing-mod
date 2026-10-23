// SPDX-License-Identifier: Apache-2.0
//! One service boundary around the existing HTTP host, ledger and terminal tasks.
use super::{
    durable_runtime::DurableRuntime,
    runtime_owner::control_gate::ControlSet,
    terminal_driver::{
        StopReason, TerminalDriver, TerminalMonitor, TerminalTask, TerminalTransport,
    },
};
use axum::{
    body::Body,
    extract::{Request, State},
    http::StatusCode,
    middleware::Next,
    response::{IntoResponse, Response},
};
use http_body::{Body as HttpBody, Frame, SizeHint};
use std::{
    collections::BTreeSet,
    pin::Pin,
    sync::{Arc, Mutex},
    task::{Context, Poll},
    time::{Duration, Instant},
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Starting,
    Serving,
    Draining,
    Quiesced,
    Stopped,
    Failed,
}
struct StateData {
    phase: Phase,
    http: usize,
    deadline: Option<tokio::time::Instant>,
}

pub struct ServiceState {
    runtime: Arc<DurableRuntime>,
    controls: Arc<ControlSet>,
    sources: Vec<(String, String, TerminalMonitor)>,
    max_age: Duration,
    max_http: usize,
    state: Mutex<StateData>,
}
impl ServiceState {
    fn dependencies_ready(&self) -> bool {
        self.runtime.admission_ready()
            && self
                .sources
                .iter()
                .all(|(_, _, source)| source.progress().ready_at(Instant::now(), self.max_age))
            && self
                .sources
                .iter()
                .any(|(worker, instance, _)| self.controls.revision(worker, instance).is_some())
    }

    /// Call after bind. This does not resume a recovered ledger or grant a lease.
    pub fn listening(&self) -> Result<(), &'static str> {
        let mut state = self.state.lock().map_err(|_| "service_poisoned")?;
        if state.phase != Phase::Starting {
            return Err("service_start_conflict");
        }
        state.phase = Phase::Serving;
        Ok(())
    }

    pub fn ready(&self) -> bool {
        self.state
            .lock()
            .is_ok_and(|state| state.phase == Phase::Serving && self.dependencies_ready())
    }
    pub fn http_inflight(&self) -> Result<usize, &'static str> {
        Ok(self.state.lock().map_err(|_| "service_poisoned")?.http)
    }
    pub fn phase(&self) -> Phase {
        self.state
            .lock()
            .map(|state| state.phase)
            .unwrap_or(Phase::Failed)
    }
    pub fn enter(self: &Arc<Self>) -> Result<HttpWork, &'static str> {
        let mut state = self.state.lock().map_err(|_| "service_poisoned")?;
        if state.phase != Phase::Serving || !self.dependencies_ready() {
            return Err("runtime_not_ready");
        }
        if state.http >= self.max_http {
            return Err("http_capacity");
        }
        state.http += 1;
        Ok(HttpWork(self.clone()))
    }

    /// First stop wins; a repeated signal cannot extend this deadline.
    pub fn begin_drain(&self, budget: Duration) -> Result<tokio::time::Instant, &'static str> {
        if budget.is_zero() || budget > Duration::from_secs(300) {
            return Err("invalid_drain_budget");
        }
        let mut state = self.state.lock().map_err(|_| "service_poisoned")?;
        if let Some(deadline) = state.deadline {
            return Ok(deadline);
        }
        if matches!(state.phase, Phase::Stopped | Phase::Failed) {
            return Err("service_closed");
        }
        state.phase = Phase::Draining;
        let deadline = tokio::time::Instant::now() + budget;
        state.deadline = Some(deadline);
        if self.controls.drain().is_err() {
            state.phase = Phase::Failed;
            self.runtime.owner().fail_closed();
            return Err("control_drain_failed");
        }
        Ok(deadline)
    }
    fn fail(&self) {
        if let Ok(mut state) = self.state.lock() {
            state.phase = Phase::Failed;
        }
        self.runtime.owner().fail_closed();
    }
}

/// Covers handler execution and the entire response body. Drop is HTTP evidence
/// only: no ledger release, cancellation confirmation or terminal fabrication.
pub struct HttpWork(Arc<ServiceState>);
impl Drop for HttpWork {
    fn drop(&mut self) {
        if let Ok(mut state) = self.0.state.lock() {
            state.http -= 1;
        } else {
            self.0.runtime.owner().fail_closed();
        }
    }
}
pub struct TrackedBody {
    inner: Body,
    work: Option<HttpWork>,
}
impl TrackedBody {
    pub fn new(inner: Body, work: HttpWork) -> Self {
        let work = (!inner.is_end_stream()).then_some(work);
        Self { inner, work }
    }
}
impl HttpBody for TrackedBody {
    type Data = bytes::Bytes;
    type Error = axum::Error;
    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Self::Data>, Self::Error>>> {
        let result = Pin::new(&mut self.inner).poll_frame(cx);
        if matches!(result, Poll::Ready(None | Some(Err(_)))) || self.inner.is_end_stream() {
            self.work.take();
        }
        result
    }
    fn is_end_stream(&self) -> bool {
        self.inner.is_end_stream()
    }
    fn size_hint(&self) -> SizeHint {
        self.inner.size_hint()
    }
}

/// Apply to the original business router, outside body extraction/concurrency
/// layers. Mount readiness separately; never wrap a second forwarding stack.
pub async fn track_http(
    State(state): State<Arc<ServiceState>>,
    request: Request,
    next: Next,
) -> Response {
    let work = match state.enter() {
        Ok(work) => work,
        Err("http_capacity") => {
            return (StatusCode::TOO_MANY_REQUESTS, "http_capacity").into_response()
        }
        Err(_) => return (StatusCode::SERVICE_UNAVAILABLE, "runtime_not_ready").into_response(),
    };
    let (parts, body) = next.run(request).await.into_parts();
    Response::from_parts(parts, Body::new(TrackedBody::new(body, work)))
}
pub async fn readiness(State(state): State<Arc<ServiceState>>) -> StatusCode {
    if state.ready() {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Shutdown {
    Clean,
    TimedOut,
    Failed,
}
impl Shutdown {
    pub fn exit_code(self) -> i32 {
        match self {
            Self::Clean => 0,
            Self::TimedOut => 75,
            Self::Failed => 70,
        }
    }
}

pub struct ServiceSupervisor<T: TerminalTransport + 'static> {
    pub state: Arc<ServiceState>,
    tasks: Vec<TerminalTask<T>>,
    finished: bool,
}
impl<T: TerminalTransport + 'static> ServiceSupervisor<T> {
    pub fn start(
        runtime: Arc<DurableRuntime>,
        controls: Arc<ControlSet>,
        drivers: Vec<TerminalDriver<T>>,
        interval: Duration,
        max_age: Duration,
        max_http: usize,
    ) -> Result<Self, &'static str> {
        // Validate BEFORE spawning any consumer. No partial or duplicate source set.
        if !runtime.admission_ready() {
            return Err("service_recovery_required");
        }
        let expected = runtime.terminal_cursors()?;
        let workers = runtime.owner().checkpoint()?.workers;
        let mut seen = BTreeSet::new();
        if drivers.is_empty()
            || drivers.len() != expected.len()
            || drivers.len() != controls.0.len()
            || workers.len() != expected.len()
            || !runtime.owner().uses_controls(&controls)
            || max_age.is_zero()
            || max_age > Duration::from_secs(300)
            || max_http == 0
            || max_http > 1_000_000
            || drivers.iter().any(|driver| {
                !driver.uses_runtime(&runtime)
                    || !expected.contains(driver.cursor())
                    || !seen.insert(driver.cursor().source_id.clone())
                    || !controls.0.contains_key(&driver.cursor().worker_url)
                    || !workers.iter().any(|worker| {
                        worker.worker_url == driver.cursor().worker_url
                            && worker.instance_epoch == driver.cursor().instance_epoch
                    })
            })
        {
            runtime.owner().fail_closed();
            return Err("service_source_or_config_invalid");
        }
        let mut tasks = Vec::new();
        let mut sources = Vec::new();
        runtime.claim_service()?;
        for driver in drivers {
            let binding = driver.cursor().clone();
            let task = TerminalTask::spawn(driver, interval)?;
            sources.push((binding.worker_url, binding.instance_epoch, task.monitor()));
            tasks.push(task);
        }
        Ok(Self {
            state: Arc::new(ServiceState {
                runtime,
                controls,
                sources,
                max_age,
                max_http,
                state: Mutex::new(StateData {
                    phase: Phase::Starting,
                    http: 0,
                    deadline: None,
                }),
            }),
            tasks,
            finished: false,
        })
    }

    /// One outer deadline covers draining and joining. Timeout leaves durable
    /// state intact; the caller must terminate, not restart this runtime in place.
    pub async fn shutdown(mut self, budget: Duration) -> Shutdown {
        let deadline = match self.state.begin_drain(budget) {
            Ok(value) => value,
            Err(_) => {
                self.state.fail();
                return Shutdown::Failed;
            }
        };
        let result = tokio::time::timeout_at(deadline, async {
            loop {
                if self.state.runtime.owner().is_failed() {
                    return Shutdown::Failed;
                }
                if self.state.http_inflight() == Ok(0)
                    && self.state.runtime.owner().active_len() == Ok(0)
                    && self.tasks.iter().all(|task| task.progress().settled())
                {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
            for task in &self.tasks {
                task.request_stop();
            }
            for task in self.tasks.drain(..) {
                match task.join().await {
                    Ok((Ok(StopReason::Requested), driver))
                        if !driver.pending_remote_confirmation() => {}
                    _ => return Shutdown::Failed,
                }
            }
            Shutdown::Clean
        })
        .await
        .unwrap_or(Shutdown::TimedOut);
        if result == Shutdown::Clean {
            if let Ok(mut state) = self.state.state.lock() {
                state.phase = Phase::Quiesced;
            } else {
                self.state.runtime.owner().fail_closed();
                return Shutdown::Failed;
            }
            self.finished = true;
        } else {
            self.state.fail();
        }
        result
    }
}
impl<T: TerminalTransport + 'static> Drop for ServiceSupervisor<T> {
    fn drop(&mut self) {
        if !self.finished {
            self.state.fail();
            for task in &self.tasks {
                task.request_stop();
            }
        }
    }
}

/// The existing Axum host supplies `app`; signal selection is owned by its
/// entrypoint. Only the HTTP server is aborted on timeout, never a commit task.
pub async fn serve_until<T: TerminalTransport + 'static>(
    listener: tokio::net::TcpListener,
    app: axum::Router,
    supervisor: ServiceSupervisor<T>,
    stop: impl std::future::Future<Output = ()>,
    budget: Duration,
) -> Shutdown {
    struct HttpServer(
        tokio::task::JoinHandle<std::io::Result<()>>,
        Arc<ServiceState>,
    );
    impl Drop for HttpServer {
        fn drop(&mut self) {
            self.0.abort();
            if self.1.phase() != Phase::Stopped {
                self.1.fail();
            }
        }
    }
    let state = supervisor.state.clone();
    if budget.is_zero() || budget > Duration::from_secs(300) || state.listening().is_err() {
        state.fail();
        return Shutdown::Failed;
    }
    let (stop_http, stopping) = tokio::sync::oneshot::channel();
    let mut http = HttpServer(
        tokio::spawn(async move {
            axum::serve(listener, app)
                .with_graceful_shutdown(async {
                    let _ = stopping.await;
                })
                .await
        }),
        state.clone(),
    );
    tokio::select! {
        _ = stop => (),
        _ = &mut http.0 => { state.fail(); return Shutdown::Failed; }
    }
    let deadline = match state.begin_drain(budget) {
        Ok(value) => value,
        Err(_) => {
            state.fail();
            return Shutdown::Failed;
        }
    };
    let result = supervisor.shutdown(budget).await;
    let _ = stop_http.send(());
    let joined = tokio::time::timeout_at(deadline, &mut http.0).await;
    let result = match joined {
        Ok(Ok(Ok(()))) => result,
        Err(_) if result == Shutdown::Clean => Shutdown::TimedOut,
        _ if result == Shutdown::Clean => Shutdown::Failed,
        _ => result,
    };
    if result == Shutdown::Clean {
        if let Ok(mut data) = state.state.lock() {
            data.phase = Phase::Stopped;
        } else {
            state.runtime.owner().fail_closed();
            return Shutdown::Failed;
        }
    } else {
        state.fail();
    }
    result
}
