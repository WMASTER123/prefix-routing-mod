// SPDX-License-Identifier: Apache-2.0
//! Bounded decoder for the fixed HUST native KV event profile.
//!
//! Socket ownership and recovery trust live outside this module. Decoding an
//! END marker never creates a snapshot or a publisher epoch.
use crate::directory::{Batch, Block, Op};
use serde::{
    de::{IgnoredAny, SeqAccess, Visitor},
    Deserialize, Deserializer,
};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, fmt, sync::Arc};

pub const MAX_PAYLOAD: usize = 1024 * 1024;
pub const MAX_OPERATIONS: usize = 4096;
pub const MAX_TOKENS: usize = 262_144;
pub const BLOCK_SIZE: usize = 128;
const END_SEQUENCE: [u8; 8] = [0xff; 8];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WireFault {
    Frame,
    Decode,
    Envelope,
    Unsupported,
    Budget,
    Gap,
    Conflict,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct Bytes(Vec<u8>);

struct BytesVisitor;

impl<'de> Visitor<'de> for BytesVisitor {
    type Value = Bytes;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a MessagePack binary value")
    }

    fn visit_bytes<E>(self, value: &[u8]) -> Result<Self::Value, E>
    where
        E: serde::de::Error,
    {
        Ok(Bytes(value.to_vec()))
    }

    fn visit_borrowed_bytes<E>(self, value: &'de [u8]) -> Result<Self::Value, E>
    where
        E: serde::de::Error,
    {
        Ok(Bytes(value.to_vec()))
    }

    fn visit_byte_buf<E>(self, value: Vec<u8>) -> Result<Self::Value, E>
    where
        E: serde::de::Error,
    {
        Ok(Bytes(value))
    }

    fn visit_seq<A>(self, mut sequence: A) -> Result<Self::Value, A::Error>
    where
        A: SeqAccess<'de>,
    {
        let mut value = Vec::with_capacity(sequence.size_hint().unwrap_or(0).min(64));
        while let Some(byte) = sequence.next_element::<u8>()? {
            value.push(byte);
        }
        Ok(Bytes(value))
    }
}

impl<'de> Deserialize<'de> for Bytes {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        deserializer.deserialize_byte_buf(BytesVisitor)
    }
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type", deny_unknown_fields)]
enum NativeEvent {
    BlockStored {
        block_hashes: Vec<Bytes>,
        parent_block_hash: Option<Bytes>,
        token_ids: Vec<u32>,
        block_size: u32,
        lora_id: Option<IgnoredAny>,
        medium: Option<String>,
        lora_name: Option<String>,
        #[serde(default)]
        extra_keys: Option<Vec<Option<IgnoredAny>>>,
        #[serde(default)]
        group_idx: Option<u32>,
        #[serde(default)]
        kv_cache_spec_kind: Option<String>,
        #[serde(default)]
        kv_cache_spec_sliding_window: Option<u32>,
        #[serde(default)]
        locality: Option<String>,
        #[serde(default)]
        ownership: Option<String>,
        #[serde(default)]
        session_id: Option<String>,
    },
    BlockRemoved {
        block_hashes: Vec<Bytes>,
        medium: Option<String>,
        #[serde(default)]
        group_idx: Option<u32>,
        #[serde(default)]
        locality: Option<String>,
        #[serde(default)]
        ownership: Option<String>,
    },
    AllBlocksCleared {},
}

#[derive(Debug, Deserialize)]
struct NativeBatch(f64, Vec<NativeEvent>, u32);

fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(DIGITS[(byte >> 4) as usize] as char);
        output.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    output
}

fn anchor(value: &Bytes) -> Result<Arc<str>, WireFault> {
    if value.0.len() != 32 {
        return Err(WireFault::Unsupported);
    }
    Ok(Arc::from(format!("b:{}", hex(&value.0))))
}

fn local_digest(tokens: &[u32]) -> Arc<str> {
    let mut digest = Sha256::new();
    for token in tokens {
        digest.update(token.to_le_bytes());
    }
    Arc::from(hex(&digest.finalize()))
}

fn validate_common(
    hashes: &[Bytes],
    medium: &Option<String>,
    group_idx: Option<u32>,
    locality: &Option<String>,
    ownership: &Option<String>,
) -> Result<(), WireFault> {
    if hashes.is_empty()
        || hashes.len() > MAX_OPERATIONS
        || hashes.iter().any(|value| value.0.len() != 32)
        || medium.as_deref() != Some("GPU")
        || group_idx != Some(0)
        || !matches!(locality.as_deref(), None | Some("LOCAL"))
        || ownership.is_some()
    {
        return Err(WireFault::Unsupported);
    }
    Ok(())
}

fn decode_payload(payload: &[u8], sequence: u64) -> Result<Batch, WireFault> {
    if payload.is_empty() || payload.len() > MAX_PAYLOAD {
        return Err(WireFault::Budget);
    }
    let NativeBatch(timestamp, events, rank) =
        rmp_serde::from_slice(payload).map_err(|_| WireFault::Decode)?;
    if !timestamp.is_finite() || rank != 0 || events.is_empty() || events.len() > MAX_OPERATIONS {
        return Err(WireFault::Envelope);
    }
    let mut operations = Vec::new();
    let mut token_total = 0usize;
    for event in events {
        match event {
            NativeEvent::AllBlocksCleared {} => operations.push(Op::Clear),
            NativeEvent::BlockRemoved {
                block_hashes,
                medium,
                group_idx,
                locality,
                ownership,
            } => {
                validate_common(&block_hashes, &medium, group_idx, &locality, &ownership)?;
                if operations.len().saturating_add(block_hashes.len()) > MAX_OPERATIONS {
                    return Err(WireFault::Budget);
                }
                for block_hash in block_hashes {
                    operations.push(Op::Remove(anchor(&block_hash)?));
                }
            }
            NativeEvent::BlockStored {
                block_hashes,
                parent_block_hash,
                token_ids,
                block_size,
                lora_id,
                medium,
                lora_name,
                extra_keys,
                group_idx,
                kv_cache_spec_kind,
                kv_cache_spec_sliding_window,
                locality,
                ownership,
                session_id,
            } => {
                validate_common(&block_hashes, &medium, group_idx, &locality, &ownership)?;
                if block_size as usize != BLOCK_SIZE
                    || lora_id.is_some()
                    || lora_name.is_some()
                    || kv_cache_spec_kind.as_deref() != Some("full_attention")
                    || kv_cache_spec_sliding_window.is_some()
                    || session_id.as_ref().is_some_and(|value| {
                        value.is_empty() || value.len() > 128 || value.chars().any(char::is_control)
                    })
                    || extra_keys.as_ref().is_some_and(|items| {
                        items.len() != block_hashes.len() || items.iter().any(Option::is_some)
                    })
                {
                    return Err(WireFault::Unsupported);
                }
                let required = block_hashes
                    .len()
                    .checked_mul(BLOCK_SIZE)
                    .ok_or(WireFault::Budget)?;
                token_total = token_total
                    .checked_add(token_ids.len())
                    .ok_or(WireFault::Budget)?;
                if token_ids.len() != required
                    || token_total > MAX_TOKENS
                    || operations.len().saturating_add(block_hashes.len()) > MAX_OPERATIONS
                {
                    return Err(WireFault::Budget);
                }
                let mut parent = parent_block_hash.as_ref().map(anchor).transpose()?;
                for (offset, block_hash) in block_hashes.into_iter().enumerate() {
                    let current = anchor(&block_hash)?;
                    let start = offset * BLOCK_SIZE;
                    operations.push(Op::Store(Block {
                        id: current.clone(),
                        parent,
                        local: local_digest(&token_ids[start..start + BLOCK_SIZE]),
                        resident: true,
                    }));
                    parent = Some(current);
                }
            }
        }
    }
    if operations.is_empty() || operations.len() > MAX_OPERATIONS {
        return Err(WireFault::Budget);
    }
    Ok(Batch {
        sequence,
        ops: operations,
    })
}

pub fn decode_live(frames: &[Vec<u8>]) -> Result<Batch, WireFault> {
    if frames.len() != 3
        || !frames[0].is_empty()
        || frames[1].len() != 8
        || frames[1].as_slice() == END_SEQUENCE
    {
        return Err(WireFault::Frame);
    }
    let sequence = u64::from_be_bytes(frames[1].as_slice().try_into().unwrap());
    decode_payload(&frames[2], sequence)
}

pub fn decode_replay(frames: &[Vec<u8>]) -> Result<Option<Batch>, WireFault> {
    if frames.len() != 4 || !frames[0].is_empty() {
        return Err(WireFault::Frame);
    }
    if frames[1].is_empty() && frames[2].as_slice() == END_SEQUENCE && frames[3].is_empty() {
        return Ok(None);
    }
    decode_live(&frames[1..]).map(Some)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Acceptance {
    Apply,
    Duplicate,
}

#[derive(Debug)]
pub struct Continuity {
    next: u64,
    window: usize,
    recent: BTreeMap<u64, [u8; 32]>,
    failed: bool,
}

fn fingerprint(batch: &Batch) -> [u8; 32] {
    fn text(digest: &mut Sha256, value: &str) {
        digest.update((value.len() as u64).to_be_bytes());
        digest.update(value.as_bytes());
    }

    let mut digest = Sha256::new();
    digest.update(batch.sequence.to_be_bytes());
    digest.update((batch.ops.len() as u64).to_be_bytes());
    for operation in &batch.ops {
        match operation {
            Op::Store(block) => {
                digest.update([0]);
                text(&mut digest, &block.id);
                if let Some(parent) = &block.parent {
                    digest.update([1]);
                    text(&mut digest, parent);
                } else {
                    digest.update([0]);
                }
                text(&mut digest, &block.local);
                digest.update([u8::from(block.resident)]);
            }
            Op::Remove(id) => {
                digest.update([1]);
                text(&mut digest, id);
            }
            Op::Clear => digest.update([2]),
        }
    }
    digest.finalize().into()
}

impl Continuity {
    pub fn new(window: usize) -> Result<Self, WireFault> {
        if !(1..=128).contains(&window) {
            return Err(WireFault::Budget);
        }
        Ok(Self {
            next: 0,
            window,
            recent: BTreeMap::new(),
            failed: false,
        })
    }

    pub fn accept(&mut self, batch: &Batch) -> Result<Acceptance, WireFault> {
        if self.failed {
            return Err(WireFault::Failed);
        }
        if batch.sequence < self.next {
            return match self.recent.get(&batch.sequence) {
                Some(previous) if previous == &fingerprint(batch) => Ok(Acceptance::Duplicate),
                _ => {
                    self.failed = true;
                    Err(WireFault::Conflict)
                }
            };
        }
        if batch.sequence != self.next {
            self.failed = true;
            return Err(WireFault::Gap);
        }
        self.recent.insert(batch.sequence, fingerprint(batch));
        self.next = self.next.checked_add(1).ok_or_else(|| {
            self.failed = true;
            WireFault::Gap
        })?;
        while self.recent.len() > self.window {
            let oldest = *self.recent.first_key_value().unwrap().0;
            self.recent.remove(&oldest);
        }
        Ok(Acceptance::Apply)
    }

    pub fn next(&self) -> u64 {
        self.next
    }

    pub fn failed(&self) -> bool {
        self.failed
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Serialize;

    #[derive(Serialize)]
    struct Stored<'a> {
        #[serde(rename = "type")]
        kind: &'a str,
        block_hashes: Vec<Vec<u8>>,
        parent_block_hash: Option<Vec<u8>>,
        token_ids: Vec<u32>,
        block_size: u32,
        lora_id: Option<u32>,
        medium: Option<&'a str>,
        lora_name: Option<&'a str>,
        group_idx: Option<u32>,
        kv_cache_spec_kind: Option<&'a str>,
        locality: Option<&'a str>,
        session_id: Option<&'a str>,
    }

    #[derive(Serialize)]
    struct Envelope<T>(f64, Vec<T>, u32);

    fn payload() -> Vec<u8> {
        rmp_serde::to_vec_named(&Envelope(
            1.0,
            vec![Stored {
                kind: "BlockStored",
                block_hashes: vec![vec![1; 32], vec![2; 32]],
                parent_block_hash: None,
                token_ids: (0..256).collect(),
                block_size: 128,
                lora_id: None,
                medium: Some("GPU"),
                lora_name: None,
                group_idx: Some(0),
                kv_cache_spec_kind: Some("full_attention"),
                locality: Some("LOCAL"),
                session_id: Some("request-a"),
            }],
            0,
        ))
        .unwrap()
    }

    fn live(sequence: u64, payload: Vec<u8>) -> Vec<Vec<u8>> {
        vec![vec![], sequence.to_be_bytes().to_vec(), payload]
    }

    fn decode_hex(value: &str) -> Vec<u8> {
        let value = value.trim();
        assert_eq!(value.len() % 2, 0);
        value
            .as_bytes()
            .chunks_exact(2)
            .map(|digits| {
                let text = std::str::from_utf8(digits).unwrap();
                u8::from_str_radix(text, 16).unwrap()
            })
            .collect()
    }

    #[test]
    fn fixed_profile_normalizes_binary_hashes_and_little_endian_tokens() {
        let batch = decode_live(&live(0, payload())).unwrap();
        assert_eq!(batch.sequence, 0);
        assert_eq!(batch.ops.len(), 2);
        let Op::Store(first) = &batch.ops[0] else {
            panic!("store expected")
        };
        let Op::Store(second) = &batch.ops[1] else {
            panic!("store expected")
        };
        assert_eq!(first.id.as_ref(), format!("b:{}", "01".repeat(32)));
        assert_eq!(second.parent.as_ref(), Some(&first.id));
        assert_eq!(first.local.len(), 64);
        assert_ne!(first.local, second.local);
    }

    #[test]
    fn exact_fixed_hust_msgspec_fixture_decodes() {
        let payload = decode_hex(include_str!(
            "../router-index/fixtures/hust-cedb477-msgspec-v1.hex"
        ));
        let batch = decode_live(&live(0, payload)).unwrap();
        assert_eq!(batch.sequence, 0);
        assert_eq!(batch.ops.len(), 2);
    }

    #[test]
    fn live_replay_and_end_markers_are_distinct() {
        let live = live(7, payload());
        let mut replay = vec![vec![]];
        replay.extend(live.clone());
        assert_eq!(
            decode_replay(&replay).unwrap(),
            Some(decode_live(&live).unwrap())
        );
        assert_eq!(
            decode_replay(&[vec![], vec![], END_SEQUENCE.to_vec(), vec![]]).unwrap(),
            None
        );
        assert_eq!(
            decode_live(&[vec![], END_SEQUENCE.to_vec(), vec![]]),
            Err(WireFault::Frame)
        );
    }

    #[test]
    fn profile_mismatch_and_unknown_fields_fail_closed() {
        let mut value = rmp_serde::to_vec_named(&serde_json::json!([
            1.0,
            [{
                "type": "BlockRemoved",
                "block_hashes": [vec![1_u8; 32]],
                "medium": "CPU",
                "group_idx": 0
            }],
            0
        ]))
        .unwrap();
        assert_eq!(decode_live(&live(0, value)), Err(WireFault::Unsupported));
        value = rmp_serde::to_vec_named(&serde_json::json!([
            1.0,
            [{"type": "AllBlocksCleared", "unknown": true}],
            0
        ]))
        .unwrap();
        assert_eq!(decode_live(&live(0, value)), Err(WireFault::Decode));
    }

    #[test]
    fn framing_and_payload_budgets_precede_decode() {
        assert_eq!(decode_live(&[vec![]]), Err(WireFault::Frame));
        assert_eq!(
            decode_live(&live(0, vec![0; MAX_PAYLOAD + 1])),
            Err(WireFault::Budget)
        );
        assert_eq!(decode_live(&live(0, vec![0xc0])), Err(WireFault::Decode));
    }

    #[test]
    fn continuity_deduplicates_exact_batches_and_fences_gaps() {
        let first = decode_live(&live(0, payload())).unwrap();
        let mut continuity = Continuity::new(2).unwrap();
        assert_eq!(continuity.accept(&first), Ok(Acceptance::Apply));
        assert_eq!(continuity.accept(&first), Ok(Acceptance::Duplicate));
        let gap = Batch {
            sequence: 2,
            ops: vec![Op::Clear],
        };
        assert_eq!(continuity.accept(&gap), Err(WireFault::Gap));
        assert!(continuity.failed());
        assert_eq!(continuity.accept(&first), Err(WireFault::Failed));
    }

    #[test]
    fn evicted_or_conflicting_duplicate_fails_closed() {
        let mut continuity = Continuity::new(1).unwrap();
        let first = Batch {
            sequence: 0,
            ops: vec![Op::Clear],
        };
        let second = Batch {
            sequence: 1,
            ops: vec![Op::Clear],
        };
        continuity.accept(&first).unwrap();
        continuity.accept(&second).unwrap();
        assert_eq!(continuity.next(), 2);
        assert_eq!(continuity.accept(&first), Err(WireFault::Conflict));
    }
}
