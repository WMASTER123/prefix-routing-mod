// SPDX-License-Identifier: Apache-2.0
//! Tests the shared helper, not the host patch, HTTP stack or a real backend.
#[path = "../../lifecycle_bridge.rs"]
pub mod lifecycle_bridge;
#[path = "../../prefix_extension.rs"]
pub mod prefix_extension;

#[cfg(test)]
mod tests;
