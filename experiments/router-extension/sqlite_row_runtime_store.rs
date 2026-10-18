// SPDX-License-Identifier: Apache-2.0
//! Incremental layout on the same bounded SQLite WAL/FULL connection adapter.
//! The checkpoint API and ordering header still have O(n) CPU/memory cost.
use super::{
    runtime_owner::{ActiveAttempt, OwnerCheckpoint, WorkerControl},
    runtime_store::{RuntimeStore, StoreError, StoredRuntime, TerminalCursor, RUNTIME_SCHEMA},
    sqlite_runtime_store::{digest, sql_integer, storage, SqliteRuntimeStore},
};
use rusqlite::{params, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, path::Path, sync::Mutex};

const LAYOUT: u32 = 1;
const MAX_ROWS: usize = 131072;

fn row_size(row: &rusqlite::Row<'_>, column: usize) -> rusqlite::Result<usize> {
    row.get::<_, i64>(column)?
        .try_into()
        .map_err(|_| rusqlite::Error::InvalidQuery)
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Header {
    layout: u32,
    schema_version: u32,
    owner_epoch: String,
    generation: u64,
    process_epoch: String,
    config_version: u64,
    next_attempt: u64,
    retired: Vec<String>,
    workers: Vec<String>,
    active: Vec<String>,
    cursors: Vec<String>,
}

impl Header {
    fn from_state(state: &StoredRuntime) -> Self {
        Self {
            layout: LAYOUT,
            schema_version: state.schema_version,
            owner_epoch: state.owner_epoch.clone(),
            generation: state.generation,
            process_epoch: state.checkpoint.process_epoch.clone(),
            config_version: state.checkpoint.config_version,
            next_attempt: state.checkpoint.next_attempt,
            retired: state.checkpoint.retired.clone(),
            workers: state
                .checkpoint
                .workers
                .iter()
                .map(|w| w.worker_url.clone())
                .collect(),
            active: state
                .checkpoint
                .active
                .iter()
                .map(|a| a.attempt_id.clone())
                .collect(),
            cursors: state
                .terminal_cursors
                .iter()
                .map(|c| c.source_id.clone())
                .collect(),
        }
    }
}

#[derive(Debug)]
struct Cached {
    state: StoredRuntime,
    sizes: BTreeMap<String, usize>,
}

struct Plan {
    header: Vec<u8>,
    changed: Vec<(String, Vec<u8>)>,
    removed: Vec<String>,
    sizes: BTreeMap<String, usize>,
}

fn rows(state: &StoredRuntime) -> BTreeMap<String, Row<'_>> {
    state
        .checkpoint
        .workers
        .iter()
        .map(|w| (format!("w:{}", w.worker_url), Row::Worker(w)))
        .chain(
            state
                .checkpoint
                .active
                .iter()
                .map(|a| (format!("a:{}", a.attempt_id), Row::Active(a))),
        )
        .chain(
            state
                .terminal_cursors
                .iter()
                .map(|c| (format!("c:{}", c.source_id), Row::Cursor(c))),
        )
        .collect()
}

#[derive(PartialEq)]
enum Row<'a> {
    Worker(&'a WorkerControl),
    Active(&'a ActiveAttempt),
    Cursor(&'a TerminalCursor),
}
impl Row<'_> {
    fn encode(&self) -> Result<Vec<u8>, StoreError> {
        match self {
            Self::Worker(value) => serde_json::to_vec(value),
            Self::Active(value) => serde_json::to_vec(value),
            Self::Cursor(value) => serde_json::to_vec(value),
        }
        .map_err(storage)
    }
}

fn prepare(state: &StoredRuntime, old: Option<&Cached>, budget: usize) -> Result<Plan, StoreError> {
    state.validate()?;
    let count = state
        .checkpoint
        .workers
        .len()
        .checked_add(state.checkpoint.active.len())
        .and_then(|n| n.checked_add(state.terminal_cursors.len()))
        .ok_or(StoreError::Invalid("runtime_row_limit"))?;
    if count > MAX_ROWS {
        return Err(StoreError::Invalid("runtime_row_limit"));
    }
    let current = rows(state);
    if current.len() != count {
        return Err(StoreError::Invalid("duplicate_runtime_row"));
    }
    let previous = old.map(|old| rows(&old.state)).unwrap_or_default();
    let header = serde_json::to_vec(&Header::from_state(state)).map_err(storage)?;
    let mut total = header.len();
    if total > budget {
        return Err(StoreError::Invalid("runtime_snapshot_too_large"));
    }
    let mut changed = Vec::new();
    let mut sizes = BTreeMap::new();
    for (key, value) in &current {
        let size = if previous.get(key) == Some(value) {
            *old.and_then(|old| old.sizes.get(key))
                .ok_or(StoreError::Invalid("runtime_cache_incomplete"))?
        } else {
            let payload = value.encode()?;
            let size = payload.len();
            changed.push((key.clone(), payload));
            size
        };
        total = total
            .checked_add(key.len())
            .and_then(|n| n.checked_add(size))
            .ok_or(StoreError::Invalid("runtime_snapshot_too_large"))?;
        if total > budget {
            return Err(StoreError::Invalid("runtime_snapshot_too_large"));
        }
        sizes.insert(key.clone(), size);
    }
    let removed = previous
        .keys()
        .filter(|key| !current.contains_key(*key))
        .cloned()
        .collect();
    Ok(Plan {
        header,
        changed,
        removed,
        sizes,
    })
}

#[derive(Debug)]
pub struct SqliteRowRuntimeStore {
    sqlite: SqliteRuntimeStore,
    cache: Mutex<Option<Cached>>,
    budget: usize,
}

impl SqliteRowRuntimeStore {
    pub fn create(path: impl AsRef<Path>, budget: usize) -> Result<Self, StoreError> {
        Ok(Self {
            sqlite: SqliteRuntimeStore::create(path, budget)?,
            cache: Mutex::new(None),
            budget,
        })
    }

    pub fn open(path: impl AsRef<Path>, budget: usize) -> Result<Self, StoreError> {
        let store = Self {
            sqlite: SqliteRuntimeStore::open(path, budget)?,
            cache: Mutex::new(None),
            budget,
        };
        // Verify layout and complete state now; never initialize over another layout.
        store.load()?;
        Ok(store)
    }

    fn replace(
        &self,
        expected_owner: &str,
        expected_generation: u64,
        next: &StoredRuntime,
        takeover: bool,
    ) -> Result<(), StoreError> {
        if next.generation
            != expected_generation
                .checked_add(1)
                .ok_or(StoreError::Invalid("runtime_generation_exhausted"))?
            || (!takeover && next.owner_epoch != expected_owner)
            || (takeover && next.owner_epoch == expected_owner)
        {
            return Err(StoreError::Invalid("invalid_runtime_transition"));
        }
        let mut cache = self.cache.lock().map_err(|_| StoreError::Busy)?;
        let current = cache.as_ref().ok_or(StoreError::Missing)?;
        if current.state.owner_epoch != expected_owner
            || current.state.generation != expected_generation
        {
            return Err(StoreError::Fenced);
        }
        let plan = prepare(next, Some(current), self.budget)?;
        let generation = sql_integer(next.generation)?;
        let expected = sql_integer(expected_generation)?;
        self.sqlite.with_connection(|connection| {
            let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate).map_err(storage)?;
            let changed = tx.execute(
                "UPDATE runtime_head SET owner=?1,generation=?2,payload=?3,digest=?4 WHERE id=1 AND owner=?5 AND generation=?6",
                params![next.owner_epoch, generation, plan.header, digest(&plan.header), expected_owner, expected],
            ).map_err(storage)?;
            if changed != 1 { return Err(StoreError::Fenced); }
            for key in &plan.removed {
                tx.execute("DELETE FROM runtime_items WHERE key=?1", [key]).map_err(storage)?;
            }
            for (key, payload) in &plan.changed {
                tx.execute("INSERT INTO runtime_items VALUES(?1,?2,?3) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,digest=excluded.digest",
                    params![key, payload, digest(payload)]).map_err(storage)?;
            }
            tx.commit().map_err(storage)
        })?;
        *cache = Some(Cached {
            state: next.clone(),
            sizes: plan.sizes,
        });
        Ok(())
    }
}

impl RuntimeStore for SqliteRowRuntimeStore {
    fn initialize(&self, initial: &StoredRuntime) -> Result<(), StoreError> {
        if initial.generation != 1 {
            return Err(StoreError::Invalid("initial_generation_must_be_one"));
        }
        let plan = prepare(initial, None, self.budget)?;
        let mut cache = self.cache.lock().map_err(|_| StoreError::Busy)?;
        self.sqlite.with_connection(|connection| {
            let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate).map_err(storage)?;
            let exists: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table')", [], |row| row.get(0)).map_err(storage)?;
            if exists { return Err(StoreError::AlreadyExists); }
            tx.execute_batch("CREATE TABLE runtime_head(id INTEGER PRIMARY KEY CHECK(id=1),owner TEXT NOT NULL,generation INTEGER NOT NULL CHECK(generation>0),payload BLOB NOT NULL,digest BLOB NOT NULL CHECK(length(digest)=32)) STRICT;
                CREATE TABLE runtime_items(key TEXT PRIMARY KEY,payload BLOB NOT NULL,digest BLOB NOT NULL CHECK(length(digest)=32)) STRICT;").map_err(storage)?;
            tx.execute("INSERT INTO runtime_head VALUES(1,?1,1,?2,?3)", params![initial.owner_epoch, plan.header, digest(&plan.header)]).map_err(storage)?;
            for (key, payload) in &plan.changed {
                tx.execute("INSERT INTO runtime_items VALUES(?1,?2,?3)", params![key, payload, digest(payload)]).map_err(storage)?;
            }
            tx.commit().map_err(storage)
        })?;
        *cache = Some(Cached {
            state: initial.clone(),
            sizes: plan.sizes,
        });
        Ok(())
    }

    fn load(&self) -> Result<StoredRuntime, StoreError> {
        let mut cache = self.cache.lock().map_err(|_| StoreError::Busy)?;
        // A failed revalidation must not leave an old diff baseline usable.
        *cache = None;
        let (state, sizes) = self.sqlite.with_connection(|connection| {
            let tx = connection.transaction().map_err(storage)?;
            let row = tx.query_row("SELECT owner,generation,length(payload),payload,digest FROM runtime_head WHERE id=1", [], |row| {
                let size = row_size(row, 2)?;
                if size > self.budget { return Err(rusqlite::Error::InvalidQuery); }
                Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?, row.get::<_, Vec<u8>>(3)?, row.get::<_, Vec<u8>>(4)?))
            }).optional().map_err(storage)?.ok_or(StoreError::Missing)?;
            if digest(&row.2) != row.3 { return Err(StoreError::Invalid("runtime_snapshot_digest_mismatch")); }
            let head: Header = serde_json::from_slice(&row.2).map_err(storage)?;
            if head.layout != LAYOUT || head.schema_version != RUNTIME_SCHEMA || head.owner_epoch != row.0 || sql_integer(head.generation)? != row.1 {
                return Err(StoreError::Invalid("runtime_snapshot_metadata_mismatch"));
            }
            let count = tx.query_row("SELECT count(*) FROM runtime_items", [], |row| row_size(row, 0)).map_err(storage)?;
            if count > MAX_ROWS { return Err(StoreError::Invalid("runtime_row_limit")); }
            let mut total = row.2.len();
            let mut items = BTreeMap::new();
            let mut sizes = BTreeMap::new();
            let mut statement = tx.prepare("SELECT length(CAST(key AS BLOB)),length(payload),key,payload,digest FROM runtime_items").map_err(storage)?;
            let mut query = statement.query([]).map_err(storage)?;
            while let Some(row) = query.next().map_err(storage)? {
                let key_size = row_size(row, 0).map_err(storage)?;
                let size = row_size(row, 1).map_err(storage)?;
                total = total.checked_add(key_size).and_then(|n| n.checked_add(size)).filter(|n| *n <= self.budget).ok_or(StoreError::Invalid("runtime_snapshot_too_large"))?;
                let key: String = row.get(2).map_err(storage)?;
                let payload: Vec<u8> = row.get(3).map_err(storage)?;
                let checksum: Vec<u8> = row.get(4).map_err(storage)?;
                if digest(&payload) != checksum { return Err(StoreError::Invalid("runtime_snapshot_digest_mismatch")); }
                sizes.insert(key.clone(), size);
                items.insert(key, payload);
            }
            let mut take = |kind: &str, keys: Vec<String>| -> Result<Vec<Vec<u8>>, StoreError> {
                keys.into_iter().map(|key| items.remove(&format!("{kind}:{key}")).ok_or(StoreError::Invalid("runtime_row_missing"))).collect()
            };
            let workers = take("w", head.workers)?.into_iter().map(|v| serde_json::from_slice(&v).map_err(storage)).collect::<Result<Vec<WorkerControl>, _>>()?;
            let active = take("a", head.active)?.into_iter().map(|v| serde_json::from_slice(&v).map_err(storage)).collect::<Result<Vec<ActiveAttempt>, _>>()?;
            let terminal_cursors = take("c", head.cursors)?.into_iter().map(|v| serde_json::from_slice(&v).map_err(storage)).collect::<Result<Vec<TerminalCursor>, _>>()?;
            if !items.is_empty() { return Err(StoreError::Invalid("runtime_row_unreferenced")); }
            let state = StoredRuntime { schema_version: head.schema_version, owner_epoch: head.owner_epoch, generation: head.generation,
                checkpoint: OwnerCheckpoint { process_epoch: head.process_epoch, config_version: head.config_version, workers, active, retired: head.retired, next_attempt: head.next_attempt }, terminal_cursors };
            state.validate()?;
            // Verify keys correspond to typed row identities, not just the header.
            if rows(&state).keys().ne(sizes.keys())
                || serde_json::to_vec(&Header::from_state(&state)).map_err(storage)? != row.2
            { return Err(StoreError::Invalid("runtime_row_identity_mismatch")); }
            Ok((state, sizes))
        })?;
        *cache = Some(Cached {
            state: state.clone(),
            sizes,
        });
        Ok(state)
    }

    fn commit(&self, owner: &str, generation: u64, next: &StoredRuntime) -> Result<(), StoreError> {
        self.replace(owner, generation, next, false)
    }
    fn takeover(
        &self,
        owner: &str,
        generation: u64,
        next: &StoredRuntime,
    ) -> Result<(), StoreError> {
        self.replace(owner, generation, next, true)
    }
}
