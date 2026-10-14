// SPDX-License-Identifier: Apache-2.0
//! Bounded completion-request feature extraction for the runtime provider.
//!
//! The tokenizer adapter is injected by the fixed Router host. This module
//! owns request validation and the local block digest used by the original M2
//! index. Chat/template handling is deliberately not guessed here.
use sha2::{Digest, Sha256};
use std::{
    fmt::{Debug, Write},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
};

use super::runtime_provider::{FeaturePort, RequestView};

const UNSUPPORTED_KV_IDENTITY_FIELDS: [&str; 9] = [
    "cache_salt",
    "lora_name",
    "lora_path",
    "multi_modal_data",
    "mm_processor_kwargs",
    "prompt_embeds",
    "session_params",
    "truncate_prompt_tokens",
    "truncation_side",
];

fn valid_label(value: &str, max_len: usize) -> bool {
    !value.is_empty() && value.len() <= max_len && value.bytes().all(|byte| byte.is_ascii_graphic())
}

pub trait TokenizerPort: Debug + Send + Sync {
    fn encode(&self, text: &str) -> Result<Vec<u32>, &'static str>;
}

#[derive(Debug)]
pub struct CompletionFeaturePort {
    tokenizer: Arc<dyn TokenizerPort>,
    model: String,
    identity: String,
    process_epoch: String,
    block_size: usize,
    max_prompt_bytes: usize,
    max_prompt_tokens: usize,
    next_request: AtomicU64,
}

impl CompletionFeaturePort {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        tokenizer: Arc<dyn TokenizerPort>,
        model: String,
        identity: String,
        process_epoch: String,
        block_size: usize,
        max_prompt_bytes: usize,
        max_prompt_tokens: usize,
    ) -> Result<Self, &'static str> {
        if !valid_label(&model, 256) || !valid_label(&identity, 256) {
            return Err("invalid_feature_identity");
        }
        if !valid_label(&process_epoch, 64) {
            return Err("invalid_process_epoch");
        }
        if !(1..=4096).contains(&block_size)
            || !(1..=16_777_216).contains(&max_prompt_bytes)
            || !(1..=1_048_576).contains(&max_prompt_tokens)
        {
            return Err("invalid_feature_limit");
        }
        Ok(Self {
            tokenizer,
            model,
            identity,
            process_epoch,
            block_size,
            max_prompt_bytes,
            max_prompt_tokens,
            next_request: AtomicU64::new(0),
        })
    }

    fn allocate_request_id(&self) -> Result<String, &'static str> {
        let mut current = self.next_request.load(Ordering::Relaxed);
        loop {
            let next = current.checked_add(1).ok_or("request_id_exhausted")?;
            match self.next_request.compare_exchange_weak(
                current,
                next,
                Ordering::Relaxed,
                Ordering::Relaxed,
            ) {
                Ok(_) => return Ok(format!("prm-{}-{next}", self.process_epoch)),
                Err(observed) => current = observed,
            }
        }
    }

    fn block_hashes(&self, tokens: &[u32]) -> Arc<[String]> {
        tokens
            .chunks_exact(self.block_size)
            .map(local_block_digest)
            .collect::<Vec<_>>()
            .into()
    }
}

impl FeaturePort for CompletionFeaturePort {
    fn extract(
        &self,
        request: &serde_json::Value,
        route: &str,
    ) -> Result<RequestView, &'static str> {
        if route != "/v1/completions" {
            return Err("unsupported_feature_route");
        }
        if request.get("model").and_then(serde_json::Value::as_str) != Some(&self.model) {
            return Err("unsupported_feature_model");
        }
        if UNSUPPORTED_KV_IDENTITY_FIELDS
            .iter()
            .any(|field| request.get(field).is_some_and(|value| !value.is_null()))
        {
            return Err("unsupported_kv_identity_modifier");
        }
        let prompt = request
            .get("prompt")
            .and_then(serde_json::Value::as_str)
            .ok_or("unsupported_completion_prompt")?;
        if prompt.len() > self.max_prompt_bytes {
            return Err("prompt_byte_limit");
        }
        let tokens = self.tokenizer.encode(prompt)?;
        if tokens.len() > self.max_prompt_tokens {
            return Err("prompt_token_limit");
        }
        Ok(RequestView {
            request_id: self.allocate_request_id()?,
            identity: self.identity.clone(),
            block_hashes: self.block_hashes(&tokens),
            exact: true,
        })
    }
}

fn local_block_digest(tokens: &[u32]) -> String {
    let mut digest = Sha256::new();
    for token in tokens {
        digest.update(token.to_le_bytes());
    }
    let mut encoded = String::with_capacity(64);
    for byte in digest.finalize() {
        write!(encoded, "{byte:02x}").expect("writing to String cannot fail");
    }
    encoded
}
