// SPDX-License-Identifier: Apache-2.0
//! Durable M3 snapshot contract. Storage is off the synchronous Lease callbacks.
use super::runtime_owner::OwnerCheckpoint;
use serde::{Deserialize, Serialize};
use std::{error::Error, fmt::Debug};

pub const RUNTIME_SCHEMA: u32 = 1;
pub const MAX_CURSOR_COUNT: usize = 1024;

fn label(value: &str, limit: usize) -> bool {
    !value.is_empty() && value.len() <= limit && value.bytes().all(|byte| byte.is_ascii_graphic())
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TerminalCursor {
    pub source_id: String,
    pub worker_url: String,
    pub instance_epoch: String,
    pub observer_epoch: String,
    pub through: u64,
}

impl TerminalCursor {
    pub fn valid(&self) -> bool {
        label(&self.source_id, 128)
            && label(&self.worker_url, 2048)
            && label(&self.instance_epoch, 128)
            && label(&self.observer_epoch, 128)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StoredRuntime {
    pub schema_version: u32,
    pub owner_epoch: String,
    pub generation: u64,
    pub checkpoint: OwnerCheckpoint,
    pub terminal_cursors: Vec<TerminalCursor>,
}

impl StoredRuntime {
    pub fn validate(&self) -> Result<(), StoreError> {
        if self.schema_version != RUNTIME_SCHEMA
            || !label(&self.owner_epoch, 64)
            || self.owner_epoch != self.checkpoint.process_epoch
            || self.generation == 0
            || self.terminal_cursors.len() > MAX_CURSOR_COUNT
        {
            return Err(StoreError::Invalid("invalid_runtime_snapshot"));
        }
        let mut keys = std::collections::BTreeSet::new();
        let mut bindings = std::collections::BTreeSet::new();
        for cursor in &self.terminal_cursors {
            if !cursor.valid()
                || !keys.insert(cursor.source_id.as_str())
                || !bindings.insert((cursor.worker_url.as_str(), cursor.instance_epoch.as_str()))
            {
                return Err(StoreError::Invalid("invalid_terminal_cursor"));
            }
        }
        Ok(())
    }
}

#[derive(Debug)]
pub enum StoreError {
    AlreadyExists,
    Missing,
    Busy,
    Fenced,
    Invalid(&'static str),
    Storage(Box<dyn Error + Send + Sync>),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::AlreadyExists => formatter.write_str("runtime_store_already_exists"),
            Self::Missing => formatter.write_str("runtime_store_missing"),
            Self::Busy => formatter.write_str("runtime_store_busy"),
            Self::Fenced => formatter.write_str("runtime_store_fenced"),
            Self::Invalid(message) => formatter.write_str(message),
            Self::Storage(_) => formatter.write_str("runtime_store_io"),
        }
    }
}

impl Error for StoreError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Storage(error) => Some(error.as_ref()),
            _ => None,
        }
    }
}

pub trait RuntimeStore: Debug + Send + Sync {
    /// Create exactly once. Existing or ambiguous state must never bootstrap empty.
    fn initialize(&self, initial: &StoredRuntime) -> Result<(), StoreError>;
    fn load(&self) -> Result<StoredRuntime, StoreError>;
    /// Compare-and-swap under the current process epoch.
    fn commit(
        &self,
        expected_owner: &str,
        expected_generation: u64,
        next: &StoredRuntime,
    ) -> Result<(), StoreError>;
    /// Caller proves the old dispatcher stopped before changing owner epoch.
    fn takeover(
        &self,
        expected_owner: &str,
        expected_generation: u64,
        next: &StoredRuntime,
    ) -> Result<(), StoreError>;
}
