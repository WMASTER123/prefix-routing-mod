#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copy shared candidates into an already reviewed, patched private Router host.
# This does not apply the host patch or modify its Cargo files or HUST sources.
set -euo pipefail
if test "$#" != 2 || test "$1" != --execute; then
  printf '%s\n' 'usage: stage_router_runtime.sh --execute <existing private host>' >&2
  exit 2
fi
task_repo=$(cd "$(dirname "$0")/.." && pwd -P)
task_host=$(realpath -e -- "$2")
case "$task_host" in
  "$task_repo"/artifacts/*) ;;
  *) printf '%s\n' 'host must be below this project artifacts directory' >&2; exit 2 ;;
esac
test "$(sha256sum "$task_host/Cargo.toml" | cut -d ' ' -f1)" = 082883aba29f8ac912596325e2114a0e33d001bd23579084b05bd3c87faa1735
test "$(sha256sum "$task_host/Cargo.lock" | cut -d ' ' -f1)" = 8675caa4ef6227d0f2cb273ac921e9dd0a6989cdad304b5600887a9795355156
for task_path in src/routers/http examples src/routers/router-index/fixtures; do
  task_resolved=$(realpath -m -- "$task_host/$task_path")
  case "$task_resolved" in
    "$task_host"/*) ;;
    *) printf '%s\n' 'destination escapes reviewed host' >&2; exit 2 ;;
  esac
done
test -d "$task_host/src/routers/http"
test -d "$task_host/examples"
stage_file() {
  local task_source="$1" task_destination="$task_host/$2"
  local task_real
  task_real=$(realpath -m -- "$task_destination")
  case "$task_real" in
    "$task_host"/*) ;;
    *) printf '%s\n' 'file destination escapes reviewed host' >&2; exit 2 ;;
  esac
  test ! -L "$task_destination"
  cp -T -- "$task_source" "$task_destination"
}
for task_module in prefix_extension runtime_provider runtime_owner runtime_cache_port request_feature_port native_kv_wire native_kv_owner native_kv_zmq native_kv_zmq_tests hust_source_control hust_source_control_tests; do
  stage_file "$task_repo/experiments/router-extension/$task_module.rs" "src/routers/http/$task_module.rs"
done
stage_file "$task_repo/experiments/router-index/src/directory.rs" src/routers/http/runtime_directory.rs
stage_file "$task_repo/experiments/router-index/src/pool.rs" src/routers/http/runtime_pool.rs
mkdir -p -- "$task_host/src/routers/router-index/fixtures"
stage_file "$task_repo/experiments/router-index/fixtures/hust-cedb477-msgspec-v1.hex" src/routers/router-index/fixtures/hust-cedb477-msgspec-v1.hex
for task_example in prefix_extension_probe stream_extension_probe runtime_provider_probe runtime_owner_probe runtime_cache_owner_probe request_feature_host_probe hust_runtime_probe lifecycle_bridge admission_bridge online_bridge; do
  stage_file "$task_repo/experiments/router-extension/$task_example.rs" "examples/$task_example.rs"
done
printf 'staged=%s\n' "$task_host"
