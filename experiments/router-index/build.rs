// SPDX-License-Identifier: Apache-2.0
use std::{env, fs, path::PathBuf, process::Command};

fn main() {
    println!("cargo:rerun-if-env-changed=ROUTER_SOURCE_DIR");
    let source =
        PathBuf::from(env::var("ROUTER_SOURCE_DIR").expect("set fixed Router source directory"))
            .canonicalize()
            .expect("Router source must exist");
    let mut modules = String::from("pub mod kv_index {\n");
    for (name, expected) in [
        (
            "types",
            "ed1c370f05a6e377f018a3bdc0a79b183b46dfcff5b1b9045bd5f878f22f3b4b",
        ),
        (
            "indexer",
            "0847bc64ea1fba934932c8cd4e5e43d71869b7cc1b7f23061d3826a04b3ade4d",
        ),
    ] {
        let path = source.join(format!("src/kv_index/{name}.rs"));
        let result = Command::new("sha256sum")
            .arg(&path)
            .output()
            .expect("sha256sum required");
        assert!(result.status.success());
        assert_eq!(
            String::from_utf8(result.stdout)
                .unwrap()
                .split_whitespace()
                .next(),
            Some(expected)
        );
        println!("cargo:rerun-if-changed={}", path.display());
        modules.push_str(&format!(
            "#[path = {:?}] pub mod {name};\n",
            path.to_str().unwrap()
        ));
    }
    // The unchanged indexer implements this upstream public read-only trait.
    modules.push_str("pub trait TieredMatchProvider: Send + Sync { fn find_tiered_matches(&self, query: &indexer::MatchQuery) -> Vec<indexer::TierMatch>; }\n}\n");
    let output = PathBuf::from(env::var("OUT_DIR").unwrap()).join("upstream.rs");
    fs::write(output, modules).unwrap();
}
