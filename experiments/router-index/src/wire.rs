// SPDX-License-Identifier: Apache-2.0
//! Explicit bounded CPU input from M7 normalization into the reused M2 index.
use super::{
    directory::{Batch, Binding, Block, Fault, Limits, Op},
    pool::Pool,
};
use serde::Deserialize;
use std::{collections::BTreeMap, fs::File, io::Read, sync::Arc};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawBinding {
    worker: String,
    instance: u64,
    publisher: String,
    identity: String,
    block_size: u32,
}
impl RawBinding {
    fn into_binding(self) -> Binding {
        Binding {
            worker: Arc::from(self.worker),
            instance: self.instance,
            publisher: Arc::from(self.publisher),
            identity: Arc::from(self.identity),
            block_size: self.block_size,
        }
    }
}
#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum RawOp {
    Store {
        id: String,
        parent: Option<String>,
        local: String,
    },
    Remove {
        id: String,
    },
    Clear,
}
impl RawOp {
    fn into_op(self) -> Op {
        match self {
            Self::Store { id, parent, local } => Op::Store(Block {
                id: Arc::from(id),
                parent: parent.map(Arc::from),
                local: Arc::from(local),
                resident: true,
            }),
            Self::Remove { id } => Op::Remove(Arc::from(id)),
            Self::Clear => Op::Clear,
        }
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawBatch {
    sequence: u64,
    ops: Vec<RawOp>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Trace {
    binding: RawBinding,
    snapshot: Vec<RawOp>,
    watermark: u64,
    #[serde(default)]
    through: Option<u64>,
    #[serde(default)]
    queued: Vec<RawBatch>,
    events: Vec<RawBatch>,
    query: Vec<String>,
    limit_bytes: usize,
    lease_ns: u64,
}

fn snapshot(ops: Vec<RawOp>) -> Result<Vec<Block>, Fault> {
    if !matches!(ops.first(), Some(RawOp::Clear)) || ops.len() > 4096 {
        return Err(Fault::Invalid);
    }
    let mut blocks = BTreeMap::new();
    for op in ops.into_iter().skip(1).map(RawOp::into_op) {
        match op {
            Op::Store(block) => {
                if blocks.insert(block.id.clone(), block).is_some() {
                    return Err(Fault::Conflict);
                }
            }
            Op::Remove(id) => {
                blocks.get_mut(&id).ok_or(Fault::MissingParent)?.resident = false;
            }
            Op::Clear => return Err(Fault::Invalid),
        }
    }
    Ok(blocks.into_values().collect())
}

pub fn run(path: &str) -> Result<(), String> {
    let mut payload = Vec::new();
    File::open(path)
        .map_err(|e| e.to_string())?
        .take(2_097_153)
        .read_to_end(&mut payload)
        .map_err(|e| e.to_string())?;
    if payload.len() > 2_097_152 {
        return Err("normalized input budget".into());
    }
    let trace: Trace = serde_json::from_slice(&payload).map_err(|e| e.to_string())?;
    if trace.events.len() > 128
        || trace.queued.len() > 128
        || trace.query.len() > 4096
        || trace.snapshot.len() > 4096
    {
        return Err("trace item budget".into());
    }
    let binding = trace.binding.into_binding();
    let limits = Limits {
        records: 4096,
        operations: 4096,
        queued_batches: 128,
        queue_bytes: trace.limit_bytes / 4,
        total_bytes: trace.limit_bytes,
        query_blocks: 4096,
        parent_depth: 1024,
    };
    let mut pool = Pool::new(1, trace.limit_bytes).map_err(|e| format!("{e:?}"))?;
    pool.register(binding.clone(), limits, trace.lease_ns)
        .map_err(|e| format!("{e:?}"))?;
    let ticket = pool
        .begin_recovery(&binding)
        .map_err(|e| format!("{e:?}"))?;
    for batch in trace.queued {
        if batch.ops.len() > 4096 {
            return Err("queued operation budget".into());
        }
        pool.queue(
            &binding,
            &ticket,
            Batch {
                sequence: batch.sequence,
                ops: batch.ops.into_iter().map(RawOp::into_op).collect(),
            },
        )
        .map_err(|e| format!("{e:?}"))?;
    }
    let through = trace.through.unwrap_or(trace.watermark);
    pool.install(
        &binding,
        &ticket,
        snapshot(trace.snapshot).map_err(|e| format!("{e:?}"))?,
        trace.watermark,
        through,
    )
    .map_err(|e| format!("{e:?}"))?;
    let query: Vec<Arc<str>> = trace.query.into_iter().map(Arc::from).collect();
    let before_confirm = pool.lookup(&binding, &query, 0);
    pool.confirm(&binding, &ticket, through, 0)
        .map_err(|e| format!("{e:?}"))?;
    let mut matches = vec![pool.lookup(&binding, &query, 0)];
    for batch in trace.events {
        if batch.ops.len() > 4096 {
            return Err("batch operation budget".into());
        }
        pool.live(
            &binding,
            &ticket,
            Batch {
                sequence: batch.sequence,
                ops: batch.ops.into_iter().map(RawOp::into_op).collect(),
            },
        )
        .map_err(|e| format!("{e:?}"))?;
        matches.push(pool.lookup(&binding, &query, 0));
    }
    let after_expiry = pool.lookup(&binding, &query, trace.lease_ns);
    pool.disconnect(&binding).map_err(|e| format!("{e:?}"))?;
    let after_disconnect = pool.lookup(&binding, &query, 0);
    println!(
        "{}",
        serde_json::json!({
            "scope": "normalized CPU wire into unchanged native index",
            "before_control_confirmation": before_confirm, "matched_blocks": matches,
            "after_lease_expiry": after_expiry, "after_disconnect": after_disconnect,
            "reserved_logical_bytes": pool.reserved_bytes(), "npu_validated": false,
        })
    );
    Ok(())
}
