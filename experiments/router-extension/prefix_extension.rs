// SPDX-License-Identifier: Apache-2.0
//! CPU admission seam, not a stable production ABI. Callbacks must be local,
//! bounded and non-blocking; HTTP evidence never proves backend termination.
use axum::{body::Body, response::Response};
use std::{
    any::Any,
    fmt::Debug,
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
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
    /// Recheck M3 control/configuration fences immediately before sending.
    fn before_send(&self) -> Result<(), &'static str>;
    fn sent(&self);
    fn headers(&self, status: u16);
    /// Exactly once. `sent` means execution may have started, not acceptance.
    fn closed(&self, sent: bool, evidence: CloseEvidence);
}

pub trait HttpExtension: Debug + Send + Sync {
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

#[derive(Debug)]
pub struct AttemptGuard {
    lease: Option<Box<dyn Lease>>,
    pub proposal: Proposal,
    sent: bool,
    transport_error: bool,
}

impl AttemptGuard {
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
