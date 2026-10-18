// SPDX-License-Identifier: Apache-2.0
//! Bounded local SQLite implementation for the candidate durable M3 owner.
use super::runtime_store::{RuntimeStore, StoreError, StoredRuntime, RUNTIME_SCHEMA};
use rusqlite::{params, Connection, OpenFlags, OptionalExtension, TransactionBehavior};
use sha2::{Digest, Sha256};
use std::{
    fs::OpenOptions,
    path::{Path, PathBuf},
    sync::Mutex,
    time::Duration,
};

pub const DEFAULT_MAX_SNAPSHOT_BYTES: usize = 8 * 1024 * 1024;

pub(crate) fn storage(error: impl std::error::Error + Send + Sync + 'static) -> StoreError {
    StoreError::Storage(Box::new(error))
}

pub(crate) fn digest(payload: &[u8]) -> Vec<u8> {
    Sha256::digest(payload).to_vec()
}

pub(crate) fn sql_integer(value: u64) -> Result<i64, StoreError> {
    value
        .try_into()
        .map_err(|_| StoreError::Invalid("runtime_generation_out_of_range"))
}

fn encode(value: &StoredRuntime, max_bytes: usize) -> Result<Vec<u8>, StoreError> {
    value.validate()?;
    let payload = serde_json::to_vec(value).map_err(storage)?;
    if payload.len() > max_bytes {
        return Err(StoreError::Invalid("runtime_snapshot_too_large"));
    }
    Ok(payload)
}

fn configure(connection: &Connection) -> Result<(), StoreError> {
    connection
        .busy_timeout(Duration::from_millis(0))
        .map_err(storage)?;
    let mode: String = connection
        .pragma_update_and_check(None, "journal_mode", "WAL", |row| row.get(0))
        .map_err(storage)?;
    if !mode.eq_ignore_ascii_case("wal") {
        return Err(StoreError::Invalid("sqlite_wal_unavailable"));
    }
    connection
        .pragma_update(None, "synchronous", "FULL")
        .map_err(storage)?;
    connection
        .pragma_update(None, "wal_autocheckpoint", 64_i64)
        .map_err(storage)?;
    connection
        .pragma_update(None, "journal_size_limit", 4_i64 * 1024 * 1024)
        .map_err(storage)?;
    let synchronous: i64 = connection
        .query_row("PRAGMA synchronous", [], |row| row.get(0))
        .map_err(storage)?;
    let checkpoint: i64 = connection
        .query_row("PRAGMA wal_autocheckpoint", [], |row| row.get(0))
        .map_err(storage)?;
    let journal_limit: i64 = connection
        .query_row("PRAGMA journal_size_limit", [], |row| row.get(0))
        .map_err(storage)?;
    if synchronous != 2 || checkpoint != 64 || journal_limit != 4_i64 * 1024 * 1024 {
        return Err(StoreError::Invalid("sqlite_durability_configuration"));
    }
    Ok(())
}

fn open_existing(path: &Path) -> Result<Connection, StoreError> {
    if !path.is_file() {
        return Err(StoreError::Missing);
    }
    let connection = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(storage)?;
    configure(&connection)?;
    let integrity: String = connection
        .query_row("PRAGMA quick_check", [], |row| row.get(0))
        .map_err(storage)?;
    if integrity != "ok" {
        return Err(StoreError::Invalid("sqlite_integrity_failure"));
    }
    Ok(connection)
}

#[derive(Debug)]
pub struct SqliteRuntimeStore {
    path: PathBuf,
    connection: Mutex<Option<Connection>>,
    max_snapshot_bytes: usize,
}

impl SqliteRuntimeStore {
    pub fn create(path: impl AsRef<Path>, max_snapshot_bytes: usize) -> Result<Self, StoreError> {
        if !(1024..=64 * 1024 * 1024).contains(&max_snapshot_bytes) {
            return Err(StoreError::Invalid("invalid_snapshot_budget"));
        }
        let path = path.as_ref().to_path_buf();
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(|error| {
                if error.kind() == std::io::ErrorKind::AlreadyExists {
                    StoreError::AlreadyExists
                } else {
                    storage(error)
                }
            })?;
        let connection = open_existing(&path)?;
        Ok(Self {
            path,
            connection: Mutex::new(Some(connection)),
            max_snapshot_bytes,
        })
    }

    pub fn open(path: impl AsRef<Path>, max_snapshot_bytes: usize) -> Result<Self, StoreError> {
        if !(1024..=64 * 1024 * 1024).contains(&max_snapshot_bytes) {
            return Err(StoreError::Invalid("invalid_snapshot_budget"));
        }
        let path = path.as_ref().to_path_buf();
        let connection = open_existing(&path)?;
        Ok(Self {
            path,
            connection: Mutex::new(Some(connection)),
            max_snapshot_bytes,
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub(crate) fn with_connection<T>(
        &self,
        action: impl FnOnce(&mut Connection) -> Result<T, StoreError>,
    ) -> Result<T, StoreError> {
        let mut guard = self.connection.lock().map_err(|_| StoreError::Busy)?;
        action(guard.as_mut().ok_or(StoreError::Busy)?)
    }

    fn replace(
        &self,
        expected_owner: &str,
        expected_generation: u64,
        next: &StoredRuntime,
        takeover: bool,
    ) -> Result<(), StoreError> {
        let payload = encode(next, self.max_snapshot_bytes)?;
        if next.generation
            != expected_generation
                .checked_add(1)
                .ok_or(StoreError::Invalid("runtime_generation_exhausted"))?
            || (!takeover && next.owner_epoch != expected_owner)
            || (takeover && next.owner_epoch == expected_owner)
        {
            return Err(StoreError::Invalid("invalid_runtime_transition"));
        }
        let next_generation = sql_integer(next.generation)?;
        let expected_generation = sql_integer(expected_generation)?;
        self.with_connection(|connection| {
            let transaction = connection
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(|error| {
                    if matches!(
                        error.sqlite_error_code(),
                        Some(
                            rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked
                        )
                    ) {
                        StoreError::Busy
                    } else {
                        storage(error)
                    }
                })?;
            let changed = transaction
                .execute(
                    "UPDATE runtime SET owner_epoch=?1,generation=?2,payload=?3,digest=?4 \
                     WHERE id=1 AND schema_version=?5 AND owner_epoch=?6 AND generation=?7",
                    params![
                        next.owner_epoch,
                        next_generation,
                        payload,
                        digest(&payload),
                        RUNTIME_SCHEMA,
                        expected_owner,
                        expected_generation
                    ],
                )
                .map_err(storage)?;
            if changed != 1 {
                return Err(StoreError::Fenced);
            }
            transaction.commit().map_err(storage)
        })
    }
}

impl RuntimeStore for SqliteRuntimeStore {
    fn initialize(&self, initial: &StoredRuntime) -> Result<(), StoreError> {
        let payload = encode(initial, self.max_snapshot_bytes)?;
        if initial.generation != 1 {
            return Err(StoreError::Invalid("initial_generation_must_be_one"));
        }
        let initial_generation = sql_integer(initial.generation)?;
        self.with_connection(|connection| {
            let exists: bool = connection
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table')",
                    [],
                    |row| row.get(0),
                )
                .map_err(storage)?;
            if exists {
                return Err(StoreError::AlreadyExists);
            }
            let transaction = connection
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(storage)?;
            transaction
                .execute_batch(
                    "CREATE TABLE runtime(\
                       id INTEGER PRIMARY KEY CHECK(id=1),\
                       schema_version INTEGER NOT NULL,\
                       owner_epoch TEXT NOT NULL,\
                       generation INTEGER NOT NULL CHECK(generation>0),\
                       payload BLOB NOT NULL,\
                       digest BLOB NOT NULL CHECK(length(digest)=32)\
                     ) STRICT;",
                )
                .map_err(storage)?;
            transaction
                .execute(
                    "INSERT INTO runtime VALUES(1,?1,?2,?3,?4,?5)",
                    params![
                        RUNTIME_SCHEMA,
                        initial.owner_epoch,
                        initial_generation,
                        payload,
                        digest(&payload)
                    ],
                )
                .map_err(storage)?;
            transaction.commit().map_err(storage)
        })
    }

    fn load(&self) -> Result<StoredRuntime, StoreError> {
        self.with_connection(|connection| {
            let row = connection
                .query_row(
                    "SELECT schema_version,owner_epoch,generation,payload,digest FROM runtime WHERE id=1",
                    [],
                    |row| {
                        Ok((
                            row.get::<_, u32>(0)?,
                            row.get::<_, String>(1)?,
                            row.get::<_, i64>(2)?,
                            row.get::<_, Vec<u8>>(3)?,
                            row.get::<_, Vec<u8>>(4)?,
                        ))
                    },
                )
                .optional()
                .map_err(storage)?
                .ok_or(StoreError::Missing)?;
            if row.0 != RUNTIME_SCHEMA || row.3.len() > self.max_snapshot_bytes {
                return Err(StoreError::Invalid("invalid_persisted_runtime"));
            }
            if digest(&row.3) != row.4 {
                return Err(StoreError::Invalid("runtime_snapshot_digest_mismatch"));
            }
            let value: StoredRuntime = serde_json::from_slice(&row.3).map_err(storage)?;
            value.validate()?;
            let generation: u64 = row
                .2
                .try_into()
                .map_err(|_| StoreError::Invalid("invalid_runtime_generation"))?;
            if value.schema_version != row.0
                || value.owner_epoch != row.1
                || value.generation != generation
            {
                return Err(StoreError::Invalid("runtime_snapshot_metadata_mismatch"));
            }
            Ok(value)
        })
    }

    fn commit(
        &self,
        expected_owner: &str,
        expected_generation: u64,
        next: &StoredRuntime,
    ) -> Result<(), StoreError> {
        self.replace(expected_owner, expected_generation, next, false)
    }

    fn takeover(
        &self,
        expected_owner: &str,
        expected_generation: u64,
        next: &StoredRuntime,
    ) -> Result<(), StoreError> {
        self.replace(expected_owner, expected_generation, next, true)
    }
}
