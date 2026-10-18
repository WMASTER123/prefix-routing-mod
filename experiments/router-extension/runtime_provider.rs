// SPDX-License-Identifier: Apache-2.0
//! Candidate in-process composition for the first public request chain.
//!
//! This module owns no network service or cache tree. M1, M2 and M3 are
//! injected local ports; the provider only validates their common snapshot,
//! applies the deliberately small baseline policy and delegates reservation.
use super::prefix_extension::{AdmissionFuture, Features, HttpExtension, Lease, Proposal};
use serde_json::Value;
use std::{any::Any, collections::BTreeSet, fmt::Debug, sync::Arc};

pub const MAX_BLOCK_HASHES: usize = 4096;

#[derive(Debug, Clone)]
pub struct RequestView {
    pub request_id: String,
    pub identity: String,
    pub block_hashes: Arc<[String]>,
    pub exact: bool,
}

impl Features for RequestView {
    fn as_any(&self) -> &dyn Any {
        self
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Candidate {
    pub worker_url: String,
    pub instance_epoch: String,
    pub worker_version: u64,
    pub config_version: u64,
    pub healthy: bool,
    pub draining: bool,
    pub local_outstanding: u32,
    /// None is unknown; Some(0) is an observed miss.
    pub matched_blocks: Option<u32>,
}

#[derive(Debug, Clone)]
pub struct Decision {
    pub request_id: String,
    pub attempt_id: String,
    pub worker_url: String,
    pub instance_epoch: String,
    pub worker_version: u64,
    pub config_version: u64,
}

impl Features for Decision {
    fn as_any(&self) -> &dyn Any {
        self
    }
}

pub trait FeaturePort: Debug + Send + Sync {
    fn extract(&self, request: &Value, route: &str) -> Result<RequestView, &'static str>;
}

pub trait SnapshotPort: Debug + Send + Sync {
    /// Read consistent M3 control/debt; M2 observations may change independently.
    /// These are not pinned residency or a global M2/M3 transaction. Admission
    /// rechecks M3 fences. Never wait for directory recovery here.
    fn candidates(
        &self,
        features: &RequestView,
        available: &[String],
    ) -> Result<Vec<Candidate>, &'static str>;
}

pub trait AdmissionPort: Debug + Send + Sync {
    /// Allocate a never-reused attempt ID without mutating request history.
    fn allocate_attempt(&self, request_id: &str) -> Result<String, &'static str>;
    /// Atomically recheck instance/config/worker fences and reserve capacity.
    fn reserve_async<'a>(
        &'a self,
        features: &'a RequestView,
        decision: &'a Decision,
    ) -> AdmissionFuture<'a>;
}

#[derive(Debug)]
pub struct RuntimeProvider {
    features: Arc<dyn FeaturePort>,
    snapshots: Arc<dyn SnapshotPort>,
    admission: Arc<dyn AdmissionPort>,
}

impl RuntimeProvider {
    pub fn new(
        features: Arc<dyn FeaturePort>,
        snapshots: Arc<dyn SnapshotPort>,
        admission: Arc<dyn AdmissionPort>,
    ) -> Self {
        Self {
            features,
            snapshots,
            admission,
        }
    }

    fn choose(candidates: Vec<Candidate>) -> Result<Candidate, &'static str> {
        let mut candidates: Vec<_> = candidates
            .into_iter()
            .filter(|candidate| candidate.healthy && !candidate.draining)
            .collect();
        if candidates.is_empty() {
            return Err("no_eligible_worker");
        }
        // Only a positive trusted match is a cache benefit. Trusted miss and
        // unknown remain distinct observations, but both use the same basic
        // load fallback instead of assigning unknown an invented score.
        if let Some(depth) = candidates
            .iter()
            .filter_map(|candidate| candidate.matched_blocks.filter(|value| *value > 0))
            .max()
        {
            candidates.retain(|candidate| candidate.matched_blocks == Some(depth));
        }
        candidates.sort_by(|left, right| {
            left.local_outstanding
                .cmp(&right.local_outstanding)
                .then(left.worker_url.cmp(&right.worker_url))
        });
        Ok(candidates.remove(0))
    }

    fn validate_candidates(
        features: &RequestView,
        candidates: &[Candidate],
        available: &BTreeSet<&str>,
    ) -> Result<(), &'static str> {
        let mut workers = BTreeSet::new();
        let mut config_version = None;
        for candidate in candidates {
            if !available.contains(candidate.worker_url.as_str()) {
                return Err("snapshot_contains_foreign_worker");
            }
            if !workers.insert(candidate.worker_url.as_str()) {
                return Err("snapshot_contains_duplicate_worker");
            }
            if candidate.instance_epoch.is_empty()
                || candidate.instance_epoch.len() > 128
                || !candidate
                    .instance_epoch
                    .bytes()
                    .all(|byte| byte.is_ascii_graphic())
                || candidate.worker_version == 0
                || candidate.config_version == 0
                || candidate.matched_blocks.is_some_and(|depth| {
                    !features.exact || depth as usize > features.block_hashes.len()
                })
            {
                return Err("invalid_candidate_snapshot");
            }
            if config_version
                .replace(candidate.config_version)
                .is_some_and(|version| version != candidate.config_version)
            {
                return Err("mixed_config_snapshot");
            }
        }
        Ok(())
    }
}

impl HttpExtension for RuntimeProvider {
    fn prepare(&self, request: &Value, route: &str) -> Result<Arc<dyn Features>, &'static str> {
        let features = self.features.extract(request, route)?;
        if features.request_id.is_empty()
            || features.request_id.len() > 128
            || !features
                .request_id
                .bytes()
                .all(|byte| byte.is_ascii_graphic())
            || features.identity.is_empty()
            || features.identity.len() > 256
            || !features
                .identity
                .bytes()
                .all(|byte| byte.is_ascii_graphic())
            || features.block_hashes.len() > MAX_BLOCK_HASHES
            || features.block_hashes.iter().any(|hash| {
                hash.len() != 64
                    || !hash
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            })
            || (!features.exact && !features.block_hashes.is_empty())
        {
            return Err("invalid_request_features");
        }
        Ok(Arc::new(features))
    }

    fn select(
        &self,
        features: &dyn Features,
        available: &[String],
        registry_revision: u64,
    ) -> Result<Option<Proposal>, &'static str> {
        let features = features
            .as_any()
            .downcast_ref::<RequestView>()
            .ok_or("feature_type")?;
        let available_set: BTreeSet<&str> = available.iter().map(String::as_str).collect();
        if available_set.len() != available.len() {
            return Err("duplicate_available_worker");
        }
        let candidates = self.snapshots.candidates(features, available)?;
        Self::validate_candidates(features, &candidates, &available_set)?;
        let selected = Self::choose(candidates)?;
        let attempt_id = self.admission.allocate_attempt(&features.request_id)?;
        if attempt_id.is_empty()
            || attempt_id.len() > 128
            || !attempt_id.bytes().all(|byte| byte.is_ascii_graphic())
        {
            return Err("invalid_attempt_identity");
        }
        let decision = Decision {
            request_id: features.request_id.clone(),
            attempt_id,
            worker_url: selected.worker_url.clone(),
            instance_epoch: selected.instance_epoch,
            worker_version: selected.worker_version,
            config_version: selected.config_version,
        };
        Ok(Some(Proposal {
            worker_url: selected.worker_url,
            registry_revision,
            decision: Arc::new(decision),
        }))
    }

    fn reserve(&self, _: &dyn Features, _: &Proposal) -> Result<Box<dyn Lease>, &'static str> {
        Err("async_admission_required")
    }

    fn reserve_async<'a>(
        &'a self,
        features: &'a dyn Features,
        proposal: &'a Proposal,
    ) -> AdmissionFuture<'a> {
        Box::pin(async move {
            let features = features
                .as_any()
                .downcast_ref::<RequestView>()
                .ok_or("feature_type")?;
            let decision = proposal
                .decision
                .as_any()
                .downcast_ref::<Decision>()
                .ok_or("decision_type")?;
            if decision.request_id != features.request_id
                || decision.worker_url != proposal.worker_url
            {
                return Err("proposal_identity_mismatch");
            }
            self.admission.reserve_async(features, decision).await
        })
    }
}
