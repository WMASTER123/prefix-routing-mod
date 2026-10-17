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
    pub disabled: bool,
}

impl SourceIdentity {
    fn valid(&self) -> bool {
        self.schema_version == 1
            && !self.disabled
            && label(&self.binding.worker_id)
            && label(&self.binding.instance_epoch)
            && label(&self.binding.observer_epoch)
            && self.frontend_pid > 0
            && self.core_pid > 0
            && self.frontend_pid != self.core_pid
            && self.family_count <= 64
    }
}

pub struct HustSourceControl {
    client: Client,
    origin: String,
    token: String,
    identity: SourceIdentity,
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
    pub fn connect_cold(
        origin: &str,
        token: String,
        worker_id: &str,
        owned_frontend_pid: u32,
    ) -> Result<Self, OwnerFault> {
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
        let client = Client::builder()
            .no_proxy()
            .redirect(Policy::none())
            .timeout(Duration::from_secs(2))
            .connect_timeout(Duration::from_secs(1))
            .build()
            .map_err(|_| OwnerFault::Source)?;
        let identity = Self::read(&client, origin, &token)?;
        if !identity.valid()
            || identity.binding.worker_id != worker_id
            || identity.frontend_pid != owned_frontend_pid
            || identity.core_boundary != 0
            || identity.frontend_boundary != 0
            || identity.family_count != 0
        {
            return Err(OwnerFault::Source);
        }
        Ok(Self {
            client,
            origin: origin.into(),
            token,
            identity,
        })
    }

    pub fn identity(&self) -> &SourceIdentity {
        &self.identity
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
        if spec.binding.worker.as_ref() != self.origin
            || spec.instance_epoch.as_ref() != self.identity.binding.instance_epoch
        {
            return Err(OwnerFault::Source);
        }
        let current = Self::read(&self.client, &self.origin, &self.token)?;
        if !current.valid()
            || current.binding != self.identity.binding
            || current.frontend_pid != self.identity.frontend_pid
            || current.core_pid != self.identity.core_pid
            || current.core_boundary < self.identity.core_boundary
            || current.frontend_boundary < self.identity.frontend_boundary
        {
            return Err(OwnerFault::Source);
        }
        self.identity = current;
        Ok(())
    }
}

#[cfg(test)]
#[path = "hust_source_control_tests.rs"]
mod tests;
