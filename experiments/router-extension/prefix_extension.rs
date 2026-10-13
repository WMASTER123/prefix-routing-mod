// SPDX-License-Identifier: Apache-2.0
//! CPU admission seam, not a stable production ABI. Callbacks must be local,
//! bounded and non-blocking; HTTP evidence never proves backend termination.
use axum::{body::Body, response::Response};
use std::{
    any::Any,
    fmt::Debug,
    future::Future,
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::Duration,
};

pub trait Features: Debug + Send + Sync {
    fn as_any(&self) -> &dyn Any;
}

#[derive(Debug, Clone)]
pub struct Proposal {
    pub worker_url: String,
    pub registry_revision: u64,
    /// Opaque complete M4 decision, including request/attempt/instance fences.
    pub decision: Arc<dyn Features>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CloseEvidence {
    NotSent,
    TransportEof,
    TransportError,
    TransportDropped,
}

pub trait Lease: Debug + Send + Sync {
    /// Router-owned correlation only, not authentication. The host replaces
    /// client X-Request-Id values only when this optional carrier is present.
    fn request_id_header(&self) -> Option<&str> {
        None
    }
    /// Recheck M3 control/configuration fences immediately before sending.
    fn before_send(&self) -> Result<(), &'static str>;
    fn sent(&self);
    fn headers(&self, status: u16);
    /// Exactly once. `sent` means execution may have started, not acceptance.
    fn closed(&self, sent: bool, evidence: CloseEvidence);
}

pub type AdmissionFuture<'a> =
    Pin<Box<dyn Future<Output = Result<Box<dyn Lease>, &'static str>> + Send + 'a>>;

pub trait HttpExtension: Debug + Send + Sync {
    /// Optional asynchronous durable admission seam. Dropping this future must
    /// never cause a send; a possibly committed reservation remains unresolved.
    fn reserve_async<'a>(
        &'a self,
        features: &'a dyn Features,
        proposal: &'a Proposal,
    ) -> AdmissionFuture<'a> {
        Box::pin(async move { self.reserve(features, proposal) })
    }
    /// Explicit experiment option; existing providers keep native transport.
    fn stream_limits(&self) -> Option<StreamLimits> {
        None
    }
    fn prepare(
        &self,
        request: &serde_json::Value,
        route: &str,
    ) -> Result<Arc<dyn Features>, &'static str>;
    /// None explicitly delegates selection to the existing native policy.
    fn select(
        &self,
        features: &dyn Features,
        available: &[String],
        revision: u64,
    ) -> Result<Option<Proposal>, &'static str>;
    fn reserve(
        &self,
        features: &dyn Features,
        proposal: &Proposal,
    ) -> Result<Box<dyn Lease>, &'static str>;
}

#[derive(Debug, Clone, Copy)]
pub struct StreamLimits {
    pub chunk_bytes: usize,
    pub response_bytes: usize,
    pub idle: Duration,
}

impl StreamLimits {
    pub fn valid(self) -> bool {
        (1..=1_048_576).contains(&self.chunk_bytes)
            && (self.chunk_bytes..=67_108_864).contains(&self.response_bytes)
            && self.idle >= Duration::from_millis(1)
            && self.idle <= Duration::from_secs(300)
    }
}

type ByteStream =
    Pin<Box<dyn futures_util::Stream<Item = Result<bytes::Bytes, std::io::Error>> + Send>>;

/// Pull from the existing reqwest stream only when the response body is polled.
/// No queue or detached producer. The HTTP stack/socket buffers are separate.
pub struct DirectStream {
    inner: Option<ByteStream>,
    timer: Pin<Box<tokio::time::Sleep>>,
    limits: StreamLimits,
    bytes: usize,
    finish: Option<Box<dyn FnOnce(bool) + Send>>,
}

impl DirectStream {
    pub fn new(
        inner: ByteStream,
        limits: StreamLimits,
        finish: Box<dyn FnOnce(bool) + Send>,
    ) -> Self {
        assert!(limits.valid(), "validate before admitting a request");
        Self {
            inner: Some(inner),
            timer: Box::pin(tokio::time::sleep(limits.idle)),
            limits,
            bytes: 0,
            finish: Some(finish),
        }
    }
    fn end(&mut self, success: bool) {
        self.inner = None;
        if let Some(finish) = self.finish.take() {
            finish(success);
        }
    }
    fn error(
        &mut self,
        reason: &'static str,
    ) -> Poll<Option<Result<bytes::Bytes, std::io::Error>>> {
        self.end(false);
        Poll::Ready(Some(Err(std::io::Error::other(reason))))
    }
}

impl futures_util::Stream for DirectStream {
    type Item = Result<bytes::Bytes, std::io::Error>;
    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        use std::future::Future;
        if self.inner.is_none() {
            return Poll::Ready(None);
        }
        match self.inner.as_mut().unwrap().as_mut().poll_next(cx) {
            Poll::Ready(Some(Ok(chunk))) => {
                if chunk.is_empty() {
                    // Empty frames are not progress. Check the deadline even
                    // when the source is always ready, and yield after one
                    // frame so a consumer cannot spin inside this poll.
                    if self.timer.deadline() <= tokio::time::Instant::now()
                        || self.timer.as_mut().poll(cx).is_ready()
                    {
                        return self.error("stream_idle_limit");
                    }
                    cx.waker().wake_by_ref();
                    return Poll::Pending;
                }
                let Some(total) = self.bytes.checked_add(chunk.len()) else {
                    return self.error("stream_size_limit");
                };
                if chunk.len() > self.limits.chunk_bytes || total > self.limits.response_bytes {
                    return self.error("stream_size_limit");
                }
                self.bytes = total;
                let deadline = tokio::time::Instant::now() + self.limits.idle;
                self.timer.as_mut().reset(deadline);
                Poll::Ready(Some(Ok(chunk)))
            }
            Poll::Ready(Some(Err(_))) => self.error("upstream_stream_error"),
            Poll::Ready(None) => {
                self.end(true);
                Poll::Ready(None)
            }
            Poll::Pending => {
                if self.timer.as_mut().poll(cx).is_ready() {
                    self.error("stream_idle_limit")
                } else {
                    Poll::Pending
                }
            }
        }
    }
}

impl Drop for DirectStream {
    fn drop(&mut self) {
        self.end(false);
    }
}

#[derive(Debug)]
pub struct AttemptGuard {
    lease: Option<Box<dyn Lease>>,
    pub proposal: Proposal,
    sent: bool,
    transport_error: bool,
}

impl AttemptGuard {
    pub fn request_id_header(&self) -> Option<&str> {
        self.lease
            .as_ref()
            .and_then(|lease| lease.request_id_header())
    }
    pub fn new(lease: Box<dyn Lease>, proposal: Proposal) -> Self {
        Self {
            lease: Some(lease),
            proposal,
            sent: false,
            transport_error: false,
        }
    }
    pub fn start(&mut self) -> Result<(), &'static str> {
        if self.sent {
            return Err("duplicate_send");
        }
        let lease = self.lease.as_ref().expect("live guard");
        lease.before_send()?;
        self.sent = true;
        lease.sent();
        Ok(())
    }
    pub fn headers(&self, status: u16) {
        self.lease.as_ref().expect("live guard").headers(status);
    }
    pub fn transport_failed(&mut self) {
        self.transport_error = true;
    }
    fn close(&mut self, evidence: CloseEvidence) {
        if let Some(lease) = self.lease.take() {
            let evidence = if !self.sent {
                CloseEvidence::NotSent
            } else if self.transport_error {
                CloseEvidence::TransportError
            } else {
                evidence
            };
            lease.closed(self.sent, evidence);
        }
    }
}

impl Drop for AttemptGuard {
    fn drop(&mut self) {
        self.close(CloseEvidence::TransportDropped);
    }
}

struct ObservedBody {
    inner: Pin<Box<dyn futures_util::Stream<Item = Result<bytes::Bytes, axum::Error>> + Send>>,
    guard: AttemptGuard,
}

impl futures_util::Stream for ObservedBody {
    type Item = Result<bytes::Bytes, axum::Error>;
    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let next = self.inner.as_mut().poll_next(cx);
        match &next {
            Poll::Ready(None) => self.guard.close(CloseEvidence::TransportEof),
            Poll::Ready(Some(Err(_))) => self.guard.close(CloseEvidence::TransportError),
            _ => {}
        }
        next
    }
}

pub fn observe_body(response: Response, guard: AttemptGuard) -> Response {
    let (parts, body) = response.into_parts();
    Response::from_parts(
        parts,
        Body::from_stream(ObservedBody {
            inner: Box::pin(body.into_data_stream()),
            guard,
        }),
    )
}
