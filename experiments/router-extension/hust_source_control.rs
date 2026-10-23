// SPDX-License-Identifier: Apache-2.0
//! Fixed HUST source identity via its opt-in, authenticated loopback endpoint.
//! Construct and call on the KV owner's blocking thread, never an HTTP worker.
use crate::{
    native_kv_owner::{OwnerFault, SourceControl},
    runtime_cache_port::PartitionSpec,
};
use reqwest::{blocking::Client, redirect::Policy, Url};
use serde::Deserialize;
use std::{fmt, io::Read, time::Duration};

const MAX_IDENTITY_BYTES: u64 = 4096;

fn label(value: &str) -> bool {
    !value.is_empty() && value.len() <= 96 && value.bytes().all(|b| b.is_ascii_graphic())
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SourceBinding {
    pub worker_id: String,
    pub instance_epoch: String,
    pub observer_epoch: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceIdentity {
    pub schema_version: u32,
    pub binding: SourceBinding,
    pub frontend_pid: u32,
    pub core_pid: u32,
    pub core_boundary: u64,
    pub frontend_boundary: u64,
    pub family_count: usize,
    pub reserved_children: usize,
    pub frontend_event_count: usize,
    pub child_status_count: usize,
    pub core_retained_records: usize,
    pub core_duplicate_guards: usize,
    pub acknowledged: u64,
    pub retired_through: u64,
    pub ownership_mode: String,
    pub disabled: bool,
}

impl SourceIdentity {
    pub(crate) fn valid(&self) -> bool {
        self.schema_version == 1
            && !self.disabled
            && label(&self.binding.worker_id)
            && label(&self.binding.instance_epoch)
            && label(&self.binding.observer_epoch)
            && self.frontend_pid > 0
            && self.core_pid > 0
            && self.frontend_pid != self.core_pid
            && self.family_count <= 64
            && self.family_count <= self.reserved_children
            && self.reserved_children <= 256
            && self.frontend_event_count <= 64
            && self.child_status_count <= self.reserved_children
            && self.core_retained_records <= 256
            && self.core_retained_records == self.core_duplicate_guards
            && self.acknowledged <= self.frontend_boundary
            && self.retired_through <= self.acknowledged
            && matches!(
                self.ownership_mode.as_str(),
                "observe_only" | "exclusive_router_v1"
            )
    }
}

pub struct HustSourceControl {
    client: Client,
    origin: String,
    token: String,
    identity: SourceIdentity,
    health_origin: Option<String>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum HealthFault {
    Unavailable,
    IdentityChanged,
}

impl fmt::Debug for HustSourceControl {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("HustSourceControl")
            .field("origin", &self.origin)
            .field("identity", &self.identity)
            .finish_non_exhaustive()
    }
}

impl HustSourceControl {
    fn client(
        origin: &str,
        token: &str,
        worker_id: &str,
        owned_frontend_pid: u32,
    ) -> Result<Client, OwnerFault> {
        let url = Url::parse(origin).map_err(|_| OwnerFault::Source)?;
        if url.scheme() != "http"
            || url.host_str() != Some("127.0.0.1")
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
            || url.origin().ascii_serialization() != origin
            || !(32..=128).contains(&token.len())
            || !token.bytes().all(|b| b.is_ascii_graphic())
            || !label(worker_id)
            || owned_frontend_pid == 0
        {
            return Err(OwnerFault::Source);
        }
        Client::builder()
            .no_proxy()
            .redirect(Policy::none())
            .timeout(Duration::from_secs(2))
            .connect_timeout(Duration::from_secs(1))
            .build()
            .map_err(|_| OwnerFault::Source)
    }

    pub fn connect_cold(
        origin: &str,
        token: String,
        worker_id: &str,
        owned_frontend_pid: u32,
    ) -> Result<Self, OwnerFault> {
        let client = Self::client(origin, &token, worker_id, owned_frontend_pid)?;
        let identity = Self::read(&client, origin, &token)?;
        if !identity.valid()
            || identity.binding.worker_id != worker_id
            || identity.frontend_pid != owned_frontend_pid
            || identity.core_boundary != 0
            || identity.frontend_boundary != 0
            || identity.family_count != 0
            || identity.reserved_children != 0
            || identity.frontend_event_count != 0
            || identity.child_status_count != 0
            || identity.core_retained_records != 0
            || identity.core_duplicate_guards != 0
            || identity.acknowledged != 0
            || identity.retired_through != 0
        {
            return Err(OwnerFault::Source);
        }
        Ok(Self {
            client,
            origin: origin.into(),
            token,
            identity,
            health_origin: None,
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub fn connect_existing(
        origin: &str,
        token: String,
        worker_id: &str,
        owned_frontend_pid: u32,
        expected_instance_epoch: &str,
        expected_observer_epoch: &str,
        expected_ownership_mode: &str,
        durable_through: u64,
    ) -> Result<Self, OwnerFault> {
        if !label(expected_instance_epoch)
            || !label(expected_observer_epoch)
            || !matches!(
                expected_ownership_mode,
                "observe_only" | "exclusive_router_v1"
            )
        {
            return Err(OwnerFault::Source);
        }
        let client = Self::client(origin, &token, worker_id, owned_frontend_pid)?;
        let identity = Self::read(&client, origin, &token)?;
        if !identity.valid()
            || identity.binding.worker_id != worker_id
            || identity.binding.instance_epoch != expected_instance_epoch
            || identity.binding.observer_epoch != expected_observer_epoch
            || identity.frontend_pid != owned_frontend_pid
            || identity.ownership_mode != expected_ownership_mode
            || durable_through > identity.frontend_boundary
            || identity.acknowledged > durable_through
            || identity.retired_through > durable_through
        {
            return Err(OwnerFault::Source);
        }
        Ok(Self {
            client,
            origin: origin.into(),
            token,
            identity,
            health_origin: None,
        })
    }

    pub fn identity(&self) -> &SourceIdentity {
        &self.identity
    }

    pub fn fork_for_health(&self) -> Self {
        Self {
            client: self.client.clone(),
            origin: self.origin.clone(),
            token: self.token.clone(),
            identity: self.identity.clone(),
            health_origin: None,
        }
    }

    /// Qualification-only transport override; logical worker binding is unchanged.
    pub fn fork_for_health_at(&self, origin: Option<&str>) -> Result<Self, OwnerFault> {
        let mut fork = self.fork_for_health();
        if let Some(origin) = origin {
            fork.client = Self::client(
                origin,
                &self.token,
                &self.identity.binding.worker_id,
                self.identity.frontend_pid,
            )?;
            fork.health_origin = Some(origin.into());
        }
        Ok(fork)
    }

    pub fn refresh_health(&mut self, spec: &PartitionSpec) -> Result<(), HealthFault> {
        if spec.binding.worker.as_ref() != self.origin
            || spec.instance_epoch.as_ref() != self.identity.binding.instance_epoch
        {
            return Err(HealthFault::IdentityChanged);
        }
        let current = Self::read(
            &self.client,
            self.health_origin.as_deref().unwrap_or(&self.origin),
            &self.token,
        )
        .map_err(|_| HealthFault::Unavailable)?;
        if !current.valid() {
            return Err(HealthFault::Unavailable);
        }
        if current.binding != self.identity.binding
            || current.frontend_pid != self.identity.frontend_pid
            || current.core_pid != self.identity.core_pid
            || current.ownership_mode != self.identity.ownership_mode
        {
            return Err(HealthFault::IdentityChanged);
        }
        if current.core_boundary < self.identity.core_boundary
            || current.frontend_boundary < self.identity.frontend_boundary
            || current.acknowledged < self.identity.acknowledged
            || current.retired_through < self.identity.retired_through
            || (current.retired_through == self.identity.retired_through
                && (current.family_count < self.identity.family_count
                    || current.reserved_children < self.identity.reserved_children))
        {
            return Err(HealthFault::Unavailable);
        }
        self.identity = current;
        Ok(())
    }

    fn read(client: &Client, origin: &str, token: &str) -> Result<SourceIdentity, OwnerFault> {
        let response = client
            .get(format!("{origin}/prefix-routing/identity"))
            .header("X-Prefix-Routing-Control", token)
            .send()
            .map_err(|_| OwnerFault::Source)?;
        if response.status() != reqwest::StatusCode::OK {
            return Err(OwnerFault::Source);
        }
        let mut bytes = Vec::new();
        response
            .take(MAX_IDENTITY_BYTES + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| OwnerFault::Source)?;
        if bytes.len() as u64 > MAX_IDENTITY_BYTES {
            return Err(OwnerFault::Source);
        }
        serde_json::from_slice(&bytes).map_err(|_| OwnerFault::Source)
    }
}

impl SourceControl for HustSourceControl {
    fn verify(&mut self, spec: &PartitionSpec) -> Result<(), OwnerFault> {
        self.refresh_health(spec).map_err(|_| OwnerFault::Source)
    }
}

#[cfg(test)]
#[path = "hust_source_control_tests.rs"]
mod tests;
