// SPDX-License-Identifier: Apache-2.0
//! Fixed-Router tokenizer adapter probe for the candidate completion M1.
//!
//! The probe uses a public deterministic prompt and initializes no model or
//! device. Its runner compares the output with the fixed HUST environment.
use sha2::{Digest, Sha256};
use std::{
    fmt::{Debug, Write},
    path::PathBuf,
    sync::Arc,
};
use vllm_router_rs::tokenizer::Tokenizer;

mod prefix_extension {
    pub use vllm_router_rs::routers::http::prefix_extension::*;
}
#[path = "../src/routers/http/request_feature_port.rs"]
mod request_feature_port;
#[path = "../src/routers/http/runtime_provider.rs"]
mod runtime_provider;

use request_feature_port::{CompletionFeaturePort, TokenizerPort};
use runtime_provider::FeaturePort;

const PROMPT_SEGMENT: &str = "A reusable prefix supports efficient inference. ";

#[derive(Clone)]
struct FixedRouterTokenizer(Tokenizer);

impl Debug for FixedRouterTokenizer {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("FixedRouterTokenizer")
    }
}

impl TokenizerPort for FixedRouterTokenizer {
    fn encode(&self, text: &str) -> Result<Vec<u32>, &'static str> {
        self.0
            .encode(text)
            .map(|encoding| encoding.token_ids().to_vec())
            .map_err(|_| "tokenizer_failed")
    }
}

fn sha256(bytes: &[u8]) -> String {
    let mut encoded = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(encoded, "{byte:02x}").expect("writing to String cannot fail");
    }
    encoded
}

fn main() {
    let mut arguments = std::env::args_os().skip(1);
    let tokenizer_path = PathBuf::from(arguments.next().expect("tokenizer.json path required"));
    let expected_sha = arguments
        .next()
        .expect("expected tokenizer SHA-256 required")
        .into_string()
        .expect("SHA-256 must be UTF-8");
    assert!(arguments.next().is_none(), "unexpected arguments");
    assert!(
        expected_sha.len() == 64
            && expected_sha
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)),
        "expected SHA-256 must be lowercase hex"
    );
    let metadata = std::fs::metadata(&tokenizer_path).expect("stat tokenizer.json");
    assert!(
        metadata.is_file() && metadata.len() <= 256 * 1024 * 1024,
        "tokenizer asset too large"
    );
    let bytes = std::fs::read(&tokenizer_path).expect("read tokenizer.json");
    assert_eq!(sha256(&bytes), expected_sha, "tokenizer SHA-256 mismatch");

    let tokenizer = Arc::new(FixedRouterTokenizer(
        Tokenizer::from_file(
            tokenizer_path
                .to_str()
                .expect("tokenizer path must be valid UTF-8"),
        )
        .expect("load fixed Router tokenizer"),
    ));
    let loaded_sha = sha256(&std::fs::read(&tokenizer_path).expect("re-read tokenizer.json"));
    assert_eq!(loaded_sha, expected_sha, "tokenizer changed while loading");
    let prompt = PROMPT_SEGMENT.repeat(64);
    let token_ids = tokenizer.encode(&prompt).expect("encode fixed prompt");
    let port = CompletionFeaturePort::new(
        tokenizer,
        "qwen14b".into(),
        format!("qwen14b-tokenizer-{expected_sha}"),
        "host-probe".into(),
        128,
        16_384,
        4_096,
    )
    .expect("construct completion feature port");
    let features = port
        .extract(
            &serde_json::json!({"model": "qwen14b", "prompt": prompt}),
            "/v1/completions",
        )
        .expect("extract completion features");
    assert_eq!(features.block_hashes.len(), token_ids.len() / 128);
    println!(
        "{}",
        serde_json::json!({
            "status": "pass",
            "prompt": prompt,
            "token_ids": token_ids,
            "block_hashes": features.block_hashes.as_ref(),
            "identity": features.identity,
            "checks": [
                "tokenizer_asset_sha256",
                "fixed_router_tokenizer",
                "completion_feature_port",
                "complete_block_count"
            ]
        })
    );
}
