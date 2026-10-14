// SPDX-License-Identifier: Apache-2.0
//! Tests the shared helper, not the host patch, HTTP stack or a real backend.
#[path = "../../admission_bridge.rs"]
pub mod admission_bridge;
#[path = "../../lifecycle_bridge.rs"]
pub mod lifecycle_bridge;
#[path = "../../online_bridge.rs"]
pub mod online_bridge;
#[path = "../../prefix_extension.rs"]
pub mod prefix_extension;
#[path = "../../runtime_provider.rs"]
pub mod runtime_provider;

#[cfg(test)]
mod tests;
