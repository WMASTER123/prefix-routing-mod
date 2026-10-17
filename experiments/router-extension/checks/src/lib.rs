// SPDX-License-Identifier: Apache-2.0
//! Tests the shared helper, not the host patch, HTTP stack or a real backend.
#[path = "../../admission_bridge.rs"]
pub mod admission_bridge;
#[path = "../../durable_runtime.rs"]
pub mod durable_runtime;
#[path = "../../lifecycle_bridge.rs"]
pub mod lifecycle_bridge;
#[path = "../../online_bridge.rs"]
pub mod online_bridge;
#[path = "../../prefix_extension.rs"]
pub mod prefix_extension;
#[path = "../../request_feature_port.rs"]
pub mod request_feature_port;
#[path = "../../runtime_owner.rs"]
pub mod runtime_owner;
#[path = "../../runtime_provider.rs"]
pub mod runtime_provider;
#[path = "../../runtime_store.rs"]
pub mod runtime_store;
#[path = "../../sqlite_runtime_store.rs"]
pub mod sqlite_runtime_store;

#[cfg(test)]
mod tests;
