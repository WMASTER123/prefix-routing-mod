#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Opt-in finite experiment. Run only on a fresh copy of the reviewed Router host.
set -euo pipefail
if test "$#" != 2 || test "$1" != --execute; then
  printf '%s\n' 'usage: stage_durable_router_probe.sh --execute <fresh private host copy>' >&2
  exit 2
fi
task_repo=$(cd "$(dirname "$0")/.." && pwd -P)
task_host=$(realpath -e -- "$2")
for task_file in Cargo.toml Cargo.lock; do
  test ! -L "$task_host/$task_file"
  case "$(realpath -e -- "$task_host/$task_file")" in
    "$task_host"/*) ;;
    *) printf '%s\n' 'Cargo input escapes private host' >&2; exit 2 ;;
  esac
done
# The baseline stage verifies original Cargo hashes and destination boundaries.
bash "$task_repo/tools/stage_router_runtime.sh" --execute "$task_host"
task_patch="$task_repo/experiments/router-extension/durable-host.patch"
task_lock_patch="$task_repo/experiments/router-extension/durable-host-minimal-lock.patch"
GIT_CEILING_DIRECTORIES=$(dirname "$task_host") git -c core.autocrlf=true -C "$task_host" apply --check --whitespace=error "$task_patch" "$task_lock_patch"
GIT_CEILING_DIRECTORIES=$(dirname "$task_host") git -c core.autocrlf=true -C "$task_host" apply "$task_patch" "$task_lock_patch"
# Cargo writes lockfiles with LF; keep the generated candidate reproducible
# even when the reviewed source archive contains CRLF text.
sed -i 's/\r$//' "$task_host/Cargo.lock"
for task_module in durable_runtime runtime_store sqlite_runtime_store sqlite_row_runtime_store terminal_driver; do
  task_target="$task_host/src/routers/http/$task_module.rs"
  test ! -L "$task_target"
  case "$(realpath -m -- "$task_target")" in
    "$task_host"/*) ;;
    *) printf '%s\n' 'destination escapes reviewed host' >&2; exit 2 ;;
  esac
  cp -T -- "$task_repo/experiments/router-extension/$task_module.rs" "$task_target"
done
printf 'durable_candidate_staged=%s\n' "$task_host"
