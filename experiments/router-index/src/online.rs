// SPDX-License-Identifier: Apache-2.0
//! Bounded, long-lived qualification owner over the SAME Pool/index.
//! No sockets, engine epoch fabrication, tokenizer or production IPC choice.
use super::{
    directory::{Batch, Binding, Fault, Limits, Ticket},
    pool::Pool,
    wire::{RawBatch, RawBinding},
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    io::{self, BufRead, Read, Write},
    sync::Arc,
    time::Instant,
};

const FRAME_BYTES: usize = 1_048_576;
const PARTITION_BYTES: usize = 8 * 1024 * 1024;

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Command {
    Cold {
        binding: RawBinding,
        lease_ms: u64,
    },
    Live {
        binding: RawBinding,
        batch: RawBatch,
    },
    BeginControl {
        binding: RawBinding,
    },
    Confirm {
        binding: RawBinding,
        nonce: u64,
        through: u64,
    },
    Lookup {
        binding: RawBinding,
        hashes: Vec<String>,
    },
    Disconnect {
        binding: RawBinding,
    },
}

struct Session {
    binding: Binding,
    ticket: Ticket,
    pending: Option<(u64, u64)>,
    valid: bool,
}

struct Owner {
    pool: Pool,
    sessions: BTreeMap<Arc<str>, Session>,
    started: Instant,
    nonce: u64,
}

impl Owner {
    fn new() -> Self {
        Self {
            pool: Pool::new(2, PARTITION_BYTES * 2).unwrap(),
            sessions: BTreeMap::new(),
            started: Instant::now(),
            nonce: 0,
        }
    }

    fn session(&mut self, binding: &Binding) -> Result<&mut Session, Fault> {
        let session = self
            .sessions
            .get_mut(&binding.worker)
            .ok_or(Fault::Invalid)?;
        if session.binding != *binding || !session.valid {
            return Err(Fault::Stale);
        }
        Ok(session)
    }

    fn handle(&mut self, command: Command) -> Result<Value, Fault> {
        let now = u64::try_from(self.started.elapsed().as_nanos()).map_err(|_| Fault::Overflow)?;
        match command {
            Command::Cold { binding, lease_ms } => {
                if !(1..=30_000).contains(&lease_ms) {
                    return Err(Fault::Invalid);
                }
                let binding = binding.into_binding();
                self.pool.register(
                    binding.clone(),
                    Limits {
                        records: 4096,
                        operations: 4096,
                        queued_batches: 128,
                        queue_bytes: PARTITION_BYTES / 4,
                        total_bytes: PARTITION_BYTES,
                        query_blocks: 4096,
                        parent_depth: 1024,
                    },
                    lease_ms * 1_000_000,
                )?;
                let ticket = self.pool.begin_recovery(&binding)?;
                // Caller must prove controlled empty startup before Cold.
                self.pool.install(&binding, &ticket, vec![], 0, 0)?;
                self.sessions.insert(
                    binding.worker.clone(),
                    Session {
                        binding,
                        ticket,
                        pending: None,
                        valid: true,
                    },
                );
                Ok(json!({"reserved_logical_bytes": self.pool.reserved_bytes()}))
            }
            Command::Live { binding, batch } => {
                let binding = binding.into_binding();
                let ticket = self.session(&binding)?.ticket.clone();
                // The byte frame and serde bounds precede mutation; the
                // existing Directory validates all ops and hides a bad batch.
                let result = if batch.ops.len() > 4096 {
                    Err(Fault::Budget)
                } else {
                    self.pool.live(
                        &binding,
                        &ticket,
                        Batch {
                            sequence: batch.sequence,
                            ops: batch.ops.into_iter().map(|op| op.into_op()).collect(),
                        },
                    )
                };
                if result.is_err() {
                    self.pool.disconnect(&binding)?;
                    self.session(&binding)?.valid = false;
                }
                result?;
                Ok(json!({}))
            }
            Command::BeginControl { binding } => {
                let binding = binding.into_binding();
                self.nonce = self.nonce.checked_add(1).ok_or(Fault::Overflow)?;
                let nonce = self.nonce;
                self.session(&binding)?.pending = Some((nonce, now));
                Ok(json!({"nonce": nonce}))
            }
            Command::Confirm {
                binding,
                nonce,
                through,
            } => {
                let binding = binding.into_binding();
                let session = self.session(&binding)?;
                let Some((expected, started)) = session.pending.take() else {
                    return Err(Fault::Stale);
                };
                if expected != nonce {
                    self.pool.disconnect(&binding)?;
                    self.session(&binding)?.valid = false;
                    return Err(Fault::Stale);
                }
                let ticket = session.ticket.clone();
                if let Err(fault) = self.pool.confirm(&binding, &ticket, through, started) {
                    self.pool.disconnect(&binding)?;
                    self.session(&binding)?.valid = false;
                    return Err(fault);
                }
                Ok(json!({}))
            }
            Command::Lookup { binding, hashes } => {
                let binding = binding.into_binding();
                if hashes.len() > 4096
                    || hashes.iter().any(|value| {
                        value.len() != 64
                            || !value
                                .bytes()
                                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
                    })
                {
                    return Err(Fault::Invalid);
                }
                let hashes: Vec<Arc<str>> = hashes.into_iter().map(Arc::from).collect();
                Ok(json!({"matched_blocks": self.pool.lookup(&binding, &hashes, now)}))
            }
            Command::Disconnect { binding } => {
                let binding = binding.into_binding();
                self.session(&binding)?;
                self.pool.disconnect(&binding)?;
                self.session(&binding)?.valid = false;
                Ok(json!({}))
            }
        }
    }
}

pub fn run() -> Result<(), String> {
    let mut owner = Owner::new();
    let mut input = io::stdin().lock();
    let mut output = io::stdout().lock();
    loop {
        let mut frame = Vec::new();
        // take() bounds allocation BEFORE newline/serde processing.
        let bytes = std::io::Read::by_ref(&mut input)
            .take((FRAME_BYTES + 1) as u64)
            .read_until(b'\n', &mut frame)
            .map_err(|e| e.to_string())?;
        if bytes == 0 {
            return Ok(());
        }
        if bytes > FRAME_BYTES || frame.last() != Some(&b'\n') {
            return Err("bounded complete frame required".into());
        }
        // A malformed command closes the owner; no consumer may keep trusting
        // its last answer after process/connection failure.
        let command = serde_json::from_slice(&frame).map_err(|_| "invalid command")?;
        let reply = match owner.handle(command) {
            Ok(value) => json!({"ok": true, "value": value}),
            Err(fault) => json!({"ok": false, "fault": format!("{fault:?}")}),
        };
        writeln!(output, "{reply}").map_err(|e| e.to_string())?;
        output.flush().map_err(|e| e.to_string())?;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn command(owner: &mut Owner, value: Value) -> Result<Value, Fault> {
        owner.handle(serde_json::from_value(value).unwrap())
    }
    fn binding(worker: &str) -> Value {
        json!({"worker": worker, "instance": 1, "publisher": "controlled-session", "identity": "a".repeat(64), "block_size": 128})
    }
    fn cold(owner: &mut Owner, b: &Value) {
        command(
            owner,
            json!({"kind": "cold", "binding": b, "lease_ms": 1000}),
        )
        .unwrap();
    }
    fn confirm(owner: &mut Owner, b: &Value, through: u64) {
        let nonce = command(owner, json!({"kind": "begin_control", "binding": b})).unwrap()
            ["nonce"]
            .clone();
        command(
            owner,
            json!({"kind": "confirm", "binding": b, "nonce": nonce, "through": through}),
        )
        .unwrap();
    }
    fn lookup(owner: &mut Owner, b: &Value) -> Value {
        command(
            owner,
            json!({"kind": "lookup", "binding": b, "hashes": ["b".repeat(64)]}),
        )
        .unwrap()["matched_blocks"]
            .clone()
    }

    #[test]
    fn two_partitions_share_budget_but_not_trust() {
        let mut owner = Owner::new();
        let a = binding("a");
        let b = binding("b");
        cold(&mut owner, &a);
        cold(&mut owner, &b);
        assert_eq!(owner.pool.reserved_bytes(), PARTITION_BYTES * 2);
        assert_eq!(
            command(
                &mut owner,
                json!({"kind": "cold", "binding": binding("c"), "lease_ms": 1000})
            ),
            Err(Fault::Budget)
        );
        assert_eq!(lookup(&mut owner, &a), Value::Null);
        confirm(&mut owner, &a, 0);
        confirm(&mut owner, &b, 0);
        command(&mut owner, json!({"kind": "live", "binding": a, "batch": {"sequence": 0, "ops": [{"kind": "store", "id": "x", "parent": null, "local": "b".repeat(64)}]}})).unwrap();
        assert_eq!(lookup(&mut owner, &a), 1);
        assert_eq!(lookup(&mut owner, &b), 0);
        command(&mut owner, json!({"kind": "disconnect", "binding": a})).unwrap();
        assert_eq!(lookup(&mut owner, &a), Value::Null);
        assert_eq!(lookup(&mut owner, &b), 0);
    }

    #[test]
    fn bad_batch_tail_withdraws_entire_view_and_cannot_reconfirm() {
        let mut owner = Owner::new();
        let b = binding("a");
        cold(&mut owner, &b);
        confirm(&mut owner, &b, 0);
        let result = command(
            &mut owner,
            json!({"kind": "live", "binding": b, "batch": {"sequence": 0, "ops": [
                {"kind": "store", "id": "good", "parent": null, "local": "b".repeat(64)},
                {"kind": "store", "id": "bad", "parent": "absent", "local": "c".repeat(64)}
            ]}}),
        );
        assert_eq!(result, Err(Fault::MissingParent));
        assert_eq!(lookup(&mut owner, &b), Value::Null);
        assert_eq!(
            command(&mut owner, json!({"kind": "begin_control", "binding": b})),
            Err(Fault::Stale)
        );
    }

    #[test]
    fn foreign_identity_and_old_control_cannot_refresh() {
        let mut owner = Owner::new();
        let b = binding("a");
        cold(&mut owner, &b);
        confirm(&mut owner, &b, 0);
        let mut foreign = b.clone();
        foreign["identity"] = json!("c".repeat(64));
        assert_eq!(lookup(&mut owner, &foreign), Value::Null);
        assert_eq!(
            command(
                &mut owner,
                json!({"kind": "disconnect", "binding": foreign})
            ),
            Err(Fault::Stale)
        );
        let old = command(&mut owner, json!({"kind": "begin_control", "binding": b})).unwrap()
            ["nonce"]
            .clone();
        command(&mut owner, json!({"kind": "begin_control", "binding": b})).unwrap();
        assert_eq!(
            command(
                &mut owner,
                json!({"kind": "confirm", "binding": b, "nonce": old, "through": 0})
            ),
            Err(Fault::Stale)
        );
        assert_eq!(lookup(&mut owner, &b), Value::Null);
    }

    #[test]
    fn delayed_control_uses_owner_send_time() {
        let mut owner = Owner::new();
        let b = binding("a");
        command(
            &mut owner,
            json!({"kind": "cold", "binding": b, "lease_ms": 1}),
        )
        .unwrap();
        let nonce = command(&mut owner, json!({"kind": "begin_control", "binding": b})).unwrap()
            ["nonce"]
            .clone();
        owner.started = Instant::now() - std::time::Duration::from_millis(20);
        command(
            &mut owner,
            json!({"kind": "confirm", "binding": b, "nonce": nonce, "through": 0}),
        )
        .unwrap();
        assert_eq!(lookup(&mut owner, &b), Value::Null);
    }
}
