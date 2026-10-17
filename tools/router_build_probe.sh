#!/usr/bin/env bash
# Manual aarch64 admission probe for the fixed, unmodified Router candidate.
# This prepares a private OpenSSL SDK prefix, not a global RPM installation.
set -euo pipefail

task_stage=${1:-check}
case "$task_stage" in
  check|build) ;;
  *) printf 'usage: bash tools/router_build_probe.sh [check|build]\n' >&2; exit 2 ;;
esac
task_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
task_root="$task_repo/artifacts/router-admission-20261011"
task_source="$task_root/source"
task_sdk="$task_root/build-sysroot"
task_rpm_name=openssl-devel-3.0.12-52.oe2403sp3.aarch64.rpm
task_rpm="$task_root/sdk-rpms/$task_rpm_name"
task_url="https://repo.openeuler.org/openEuler-24.03-LTS-SP3/update/aarch64/Packages/$task_rpm_name"
task_rpm_hash=0c5eedb36f98e2961c97a0e84901535030a97ea1f3c5610bf789ba29bb329e40

test "$(uname -m)" = aarch64
grep -q '^ID="openEuler"$' /etc/os-release
test "$(rpm -q openssl-libs)" = openssl-libs-3.0.12-52.oe2403sp3.aarch64
for task_command in curl rpm rpm2archive tar sha256sum pkg-config gcc g++ cmake make perl timeout; do
  command -v "$task_command" >/dev/null
done
test "$(sha256sum "$task_root/router-source.tar" | cut -d ' ' -f1)" = eba53db252b19decc7d239a4b2f8c3b0e354db899793555cb1dad05f3c71156f
tar --compare --file "$task_root/router-source.tar" --directory "$task_source"
test "$(sha256sum "$task_source/Cargo.toml" | cut -d ' ' -f1)" = 082883aba29f8ac912596325e2114a0e33d001bd23579084b05bd3c87faa1735
test "$(sha256sum "$task_source/Cargo.lock" | cut -d ' ' -f1)" = 8675caa4ef6227d0f2cb273ac921e9dd0a6989cdad304b5600887a9795355156
mkdir -p "$task_root/sdk-rpms" "$task_sdk" "$task_root/probes"
task_run=$(mktemp -d "$task_root/probes/$task_stage.XXXXXX")

if ! test -f "$task_rpm"; then
  curl --fail --silent --show-error --location --connect-timeout 10 --max-time 120 \
    "$task_url" --output "$task_rpm"
fi
test "$(sha256sum "$task_rpm" | cut -d ' ' -f1)" = "$task_rpm_hash"
rpm --checksig "$task_rpm" > "$task_run/rpm-signature.txt"
# rpm2archive in this image writes the archive to stdout.
rpm2archive "$task_rpm" > "$task_run/sdk.tgz"
tar --extract --gzip --file "$task_run/sdk.tgz" --directory "$task_sdk" \
  --no-same-owner --no-same-permissions ./usr/include/openssl ./usr/lib64
# Preserve the versioned targets expected by this exact development RPM.
test "$(readlink "$task_sdk/usr/lib64/libssl.so")" = libssl.so.3.0.12
test "$(readlink "$task_sdk/usr/lib64/libcrypto.so")" = libcrypto.so.3.0.12
cp -L /usr/lib64/libssl.so.3 "$task_sdk/usr/lib64/libssl.so.3.0.12"
cp -L /usr/lib64/libcrypto.so.3 "$task_sdk/usr/lib64/libcrypto.so.3.0.12"
test -f "$task_sdk/usr/lib64/libssl.so"
test -f "$task_sdk/usr/lib64/libcrypto.so"

export RUSTUP_HOME="$task_root/toolchain/rustup"
export CARGO_HOME="$task_root/toolchain/cargo"
export PATH="$CARGO_HOME/bin:$PATH"
export CARGO_TARGET_DIR="$task_root/target"
export CARGO_BUILD_JOBS=4
export OPENSSL_INCLUDE_DIR="$task_sdk/usr/include"
export OPENSSL_LIB_DIR="$task_sdk/usr/lib64"
export OPENSSL_STATIC=0
export PKG_CONFIG_SYSROOT_DIR="$task_sdk"
export PKG_CONFIG_LIBDIR="$task_sdk/usr/lib64/pkgconfig"
export GIT_CONFIG_COUNT=3
export GIT_CONFIG_KEY_0="url.file://$task_root/vllm-rust-shallow.git.insteadOf"
export GIT_CONFIG_VALUE_0=https://github.com/vllm-project/vllm
export GIT_CONFIG_KEY_1="url.file://$task_root/multimodal.git.insteadOf"
export GIT_CONFIG_VALUE_1=https://github.com/smg-project/llm-multimodal
export GIT_CONFIG_KEY_2="url.file://$task_root/harmony.git.insteadOf"
export GIT_CONFIG_VALUE_2=https://github.com/openai/harmony

{
  printf 'sdk_source=%s\n' "$task_url"
  cargo --version
  rustc --version
  gcc --version | head -1
  pkg-config --libs --cflags openssl
  sha256sum "$task_rpm" "$task_source/Cargo.toml" "$task_source/Cargo.lock" \
    "$task_sdk/usr/lib64/libssl.so.3.0.12" \
    "$task_sdk/usr/lib64/libcrypto.so.3.0.12"
} > "$task_run/preflight.txt"

task_args=(check --lib)
if test "$task_stage" = build; then
  task_args=(build --release --bin vllm-router)
fi
printf 'probe=%s\n' "$task_run"
set +e
timeout --kill-after=15s 1200 cargo "${task_args[@]}" --locked \
  --manifest-path "$task_source/Cargo.toml" > "$task_run/cargo.log" 2>&1
task_rc=$?
set -e
printf '%s\n' "$task_rc" > "$task_run/exit-code.txt"
tail -40 "$task_run/cargo.log"
printf 'cargo_exit=%s\n' "$task_rc"
exit "$task_rc"
