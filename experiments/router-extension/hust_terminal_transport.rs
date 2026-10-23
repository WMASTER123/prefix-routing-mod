// SPDX-License-Identifier: Apache-2.0
//! Bounded HTTP adapter for the fixed ced6857/b47795c optional observer.
//! Caller verifies source/profile and owns one driver before enabling admission.
use super::{
    durable_runtime::TerminalRecord,
    runtime_store::TerminalCursor,
    terminal_driver::{Ownership, RpcFault, RpcFuture, TerminalBatch, TerminalTransport},
};
use reqwest::{redirect::Policy, Client, Method, Url};
use serde::Deserialize;
use std::{
    collections::{BTreeMap, BTreeSet},
    time::Duration,
};

const MAX_POLL_BYTES: usize = 1_048_576;
const MAX_RETIRE_BYTES: usize = 4096;

#[derive(Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Binding {
    worker_id: String,
    instance_epoch: String,
    observer_epoch: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Ended {
    binding: Binding,
    sequence: u64,
    attempt_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Family {
    attempt_id: String,
    expected: usize,
    bound: usize,
    ended: usize,
    sealed: bool,
    abandoned: bool,
    core_statuses: BTreeMap<String, usize>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Reply {
    schema_version: u32,
    binding: Binding,
    frontend_pid: u32,
    core_pid: u32,
    boundary: u64,
    mode: String,
    ownership_mode: String,
    records: Vec<Ended>,
    families: Vec<Family>,
    disabled: bool,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Retirement {
    schema_version: u32,
    binding: Binding,
    retired: usize,
    retired_through: u64,
    family_count: usize,
    reserved_children: usize,
}

/// No Debug implementation: credentials must never enter diagnostics.
pub struct HustTerminalTransport {
    client: Client,
    binding: Binding,
    cursor: TerminalCursor,
    token: String,
    frontend_pid: u32,
    core_pid: u32,
    ownership: Ownership,
    deadline: Duration,
    last_poll_statuses: BTreeMap<String, usize>,
    committed_statuses: BTreeMap<String, usize>,
}

impl HustTerminalTransport {
    /// `cursor` and PIDs must come from the existing verified source-control
    /// connection, not an unverified config or version string. Does no I/O.
    pub fn new(
        cursor: TerminalCursor,
        token: String,
        frontend_pid: u32,
        core_pid: u32,
        ownership: Ownership,
        deadline: Duration,
    ) -> Result<Self, RpcFault> {
        let url = Url::parse(&cursor.worker_url).map_err(|_| RpcFault::InvalidSource)?;
        if !cursor.valid()
            || url.scheme() != "http"
            || url.host_str() != Some("127.0.0.1")
            || url.origin().ascii_serialization() != cursor.worker_url
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
            || !(32..=128).contains(&token.len())
            || !token.bytes().all(|byte| byte.is_ascii_graphic())
            || frontend_pid == 0
            || core_pid == 0
            || frontend_pid == core_pid
            || deadline < Duration::from_millis(1)
            || deadline > Duration::from_secs(10)
        {
            return Err(RpcFault::InvalidSource);
        }
        let client = Client::builder()
            .no_proxy()
            .redirect(Policy::none())
            .timeout(deadline)
            .connect_timeout(deadline)
            .build()
            .map_err(|_| RpcFault::Unavailable)?;
        Ok(Self {
            client,
            binding: Binding {
                worker_id: cursor.source_id.clone(),
                instance_epoch: cursor.instance_epoch.clone(),
                observer_epoch: cursor.observer_epoch.clone(),
            },
            cursor,
            token,
            frontend_pid,
            core_pid,
            ownership,
            deadline,
            last_poll_statuses: BTreeMap::new(),
            committed_statuses: BTreeMap::new(),
        })
    }

    fn check_cursor(&self, cursor: &TerminalCursor) -> Result<(), RpcFault> {
        if cursor.source_id != self.cursor.source_id
            || cursor.worker_url != self.cursor.worker_url
            || cursor.instance_epoch != self.cursor.instance_epoch
            || cursor.observer_epoch != self.cursor.observer_epoch
            || cursor.through < self.cursor.through
        {
            return Err(RpcFault::InvalidSource);
        }
        Ok(())
    }

    async fn request(&self, method: Method, url: Url, limit: usize) -> Result<Vec<u8>, RpcFault> {
        // Outer timeout includes every body chunk, not just the response head.
        tokio::time::timeout(self.deadline, async {
            let mut request = self
                .client
                .request(method.clone(), url)
                .header("X-Prefix-Routing-Control", &self.token);
            if method == Method::POST {
                request = request
                    .body("{}")
                    .header("Content-Type", "application/json");
            }
            let mut response = request.send().await.map_err(|_| RpcFault::Unavailable)?;
            if response.status().is_server_error() {
                return Err(RpcFault::Unavailable);
            }
            if response.status().as_u16() != 200 {
                return Err(RpcFault::InvalidSource);
            }
            if response
                .content_length()
                .is_some_and(|size| size > limit as u64)
            {
                return Err(RpcFault::InvalidSource);
            }
            let mut bytes = Vec::new();
            while let Some(chunk) = response.chunk().await.map_err(|_| RpcFault::Unavailable)? {
                if chunk.len() > limit - bytes.len() {
                    return Err(RpcFault::InvalidSource);
                }
                bytes.extend_from_slice(&chunk);
            }
            Ok(bytes)
        })
        .await
        .map_err(|_| RpcFault::Unavailable)?
    }

    /// Finite qualification diagnostics only, never used to settle the ledger.
    pub fn last_poll_statuses(&self) -> &BTreeMap<String, usize> {
        &self.last_poll_statuses
    }

    /// At most seven validated status labels, saturating diagnostic counters.
    /// Never used for settlement or recovery decisions.
    pub fn committed_statuses(&self) -> &BTreeMap<String, usize> {
        &self.committed_statuses
    }

    async fn read(
        &self,
        cursor: &TerminalCursor,
        ack: u64,
    ) -> Result<(TerminalBatch, BTreeMap<String, usize>), RpcFault> {
        self.check_cursor(cursor)?;
        let mut url = Url::parse(&format!(
            "{}/prefix-routing/terminal",
            self.cursor.worker_url
        ))
        .map_err(|_| RpcFault::InvalidSource)?;
        url.query_pairs_mut()
            .append_pair("observer_epoch", &self.binding.observer_epoch)
            .append_pair("after", &cursor.through.to_string())
            .append_pair("acknowledge", &ack.to_string());
        let bytes = self.request(Method::GET, url, MAX_POLL_BYTES).await?;
        let reply: Reply = serde_json::from_slice(&bytes).map_err(|_| RpcFault::InvalidSource)?;
        self.validate_reply(cursor, reply)
    }

    fn validate_reply(
        &self,
        cursor: &TerminalCursor,
        reply: Reply,
    ) -> Result<(TerminalBatch, BTreeMap<String, usize>), RpcFault> {
        let mode = match self.ownership {
            Ownership::ObserveOnly => "observe_only",
            Ownership::ExclusiveRouterV1 => "exclusive_router_v1",
        };
        if reply.schema_version != 1
            || reply.disabled
            || reply.mode != "replay"
            || reply.binding != self.binding
            || reply.frontend_pid != self.frontend_pid
            || reply.core_pid != self.core_pid
            || reply.ownership_mode != mode
            || reply.records.len() > 64
            || reply.families.len() > 64
            || cursor.through.checked_add(reply.records.len() as u64) != Some(reply.boundary)
        {
            return Err(RpcFault::InvalidSource);
        }
        let mut families = BTreeMap::new();
        let mut children = 0_usize;
        for family in &reply.families {
            if family.attempt_id.is_empty()
                || family.attempt_id.len() > 128
                || !family
                    .attempt_id
                    .bytes()
                    .all(|byte| byte.is_ascii_graphic())
                || family.expected == 0
                || family.expected > 256
                || family.bound > family.expected
                || family.ended > family.bound
                || family.sealed && family.bound != family.expected
                || family.sealed && family.abandoned
                || families
                    .insert(family.attempt_id.as_str(), family)
                    .is_some()
            {
                return Err(RpcFault::InvalidSource);
            }
            children = children
                .checked_add(family.expected)
                .ok_or(RpcFault::InvalidSource)?;
            let mut statuses = 0_usize;
            for (status, count) in &family.core_statuses {
                if *count == 0
                    || *count > family.ended
                    || !matches!(
                        status.as_str(),
                        "unknown"
                            | "FINISHED_STOPPED"
                            | "FINISHED_LENGTH_CAPPED"
                            | "FINISHED_ABORTED"
                            | "FINISHED_IGNORED"
                            | "FINISHED_ERROR"
                            | "FINISHED_REPETITION"
                    )
                {
                    return Err(RpcFault::InvalidSource);
                }
                statuses = statuses
                    .checked_add(*count)
                    .ok_or(RpcFault::InvalidSource)?;
            }
            if statuses != family.ended {
                return Err(RpcFault::InvalidSource);
            }
        }
        if children > 256 {
            return Err(RpcFault::InvalidSource);
        }
        let mut attempts = BTreeSet::new();
        let mut records = Vec::with_capacity(reply.records.len());
        let mut statuses = BTreeMap::new();
        for (offset, record) in reply.records.into_iter().enumerate() {
            let family = families
                .get(record.attempt_id.as_str())
                .ok_or(RpcFault::InvalidSource)?;
            if record.binding != self.binding
                || record.sequence != cursor.through + offset as u64 + 1
                || !attempts.insert(record.attempt_id.clone())
                || !family.sealed
                || family.abandoned
                || family.ended != family.expected
            {
                return Err(RpcFault::InvalidSource);
            }
            for (status, count) in &family.core_statuses {
                *statuses.entry(status.clone()).or_insert(0) += count;
            }
            records.push(TerminalRecord {
                sequence: record.sequence,
                attempt_id: record.attempt_id,
                worker_url: cursor.worker_url.clone(),
                instance_epoch: cursor.instance_epoch.clone(),
            });
        }
        Ok((
            TerminalBatch {
                after: cursor.clone(),
                records,
            },
            statuses,
        ))
    }
}

impl TerminalTransport for HustTerminalTransport {
    fn committed(&mut self) {
        for (status, count) in &self.last_poll_statuses {
            let total = self.committed_statuses.entry(status.clone()).or_default();
            *total = total.saturating_add(*count);
        }
    }
    fn poll<'a>(&'a mut self, after: &'a TerminalCursor) -> RpcFuture<'a, TerminalBatch> {
        Box::pin(async move {
            let (batch, statuses) = self.read(after, 0).await?;
            self.last_poll_statuses = statuses;
            Ok(batch)
        })
    }
    fn acknowledge<'a>(&'a mut self, through: &'a TerminalCursor) -> RpcFuture<'a, ()> {
        Box::pin(async move {
            // Fixed handler applies the requested ACK before producing this
            // response. Newer records are validated here and polled later.
            self.read(through, through.through).await.map(|_| ())
        })
    }
    fn retire<'a>(&'a mut self, through: &'a TerminalCursor) -> RpcFuture<'a, ()> {
        Box::pin(async move {
            self.check_cursor(through)?;
            if self.ownership != Ownership::ExclusiveRouterV1 {
                return Err(RpcFault::InvalidSource);
            }
            let mut url = Url::parse(&format!(
                "{}/prefix-routing/terminal/retire",
                self.cursor.worker_url
            ))
            .map_err(|_| RpcFault::InvalidSource)?;
            url.query_pairs_mut()
                .append_pair("observer_epoch", &self.binding.observer_epoch)
                .append_pair("through", &through.through.to_string())
                .append_pair("checkpointed_through", &through.through.to_string());
            let bytes = self.request(Method::POST, url, MAX_RETIRE_BYTES).await?;
            let reply: Retirement =
                serde_json::from_slice(&bytes).map_err(|_| RpcFault::InvalidSource)?;
            if reply.schema_version != 1
                || reply.binding != self.binding
                || reply.retired_through != through.through
                || reply.retired > 64
                || reply.retired as u64 > through.through
                || reply.family_count > 64
                || reply.family_count > reply.reserved_children
                || reply.reserved_children > 256
            {
                return Err(RpcFault::InvalidSource);
            }
            Ok(())
        })
    }
}
