# 新终态驱动：完整宿主与双 NPU 接线验收

日期：2026-10-22。输入提交 `b1bda73`，实际驱动/HTTP 运输来自实现 `4b867d9`。本文续接[CPU 运输记录](hust-terminal-transport-2026-10-22.md)，不继承旧二进制的实机成绩。

## 来源、构建与 CPU 回归

用户从平台终端核验新分配 SSH 指纹后，严格校验连接；只操作 `/root/work/prefix-routing-mod`。同步 bundle SHA256 `bcd2b74541f24a8f0300943f1d34a49431429b177c9d104535405b49a5dcf0a7`，服务器从干净 `7110d16` 快进到 `b1bda73`。未访问受限 alias、旧项目或改全局配置。

新分配为 aarch64、8 核 CPU 时间配额、64GiB cgroup 内存、16GiB shm，两张 Ascend 910B2（本次物理 5/6、可见逻辑 0/1）。初检无自有推理进程。公共 Qwen2.5-14B-Instruct 作为只读输入，沿用固定 HUST Core ced6857 / Ascend b47795c、FP16/eager、两个 TP1。没有变更引擎、CANN、torch_npu 或服务依赖。

旧宿主 probe/driver 的 Git blob 与 `7110d16` 一致，已备份后复制本轮唯一源码；Cargo.toml/lock SHA256 保持 `01c349c73db9f52372ac1a1e844e6f20129a0d1a604f63d784886e22de94a0d1` / `a323180a12d0219463ea5ddad8b2077007fe51114ee8f5aa6a4f5ecf98a6cca1`。复用项目私有 Rust 1.99.0、OpenSSL SDK 和已有依赖缓存，离线构建：

```bash
cargo +1.99.0 clippy --offline --locked --manifest-path experiments/router-extension/checks/Cargo.toml --all-targets -- -D warnings
cargo +1.99.0 test --offline --locked --manifest-path experiments/router-extension/checks/Cargo.toml
cargo +1.99.0 test --offline --release --locked --features durable-candidate --example hust_runtime_probe --manifest-path artifacts/durable-host-minimal-20261017/Cargo.toml -j 4
cargo +1.99.0 build --offline --release --locked --features durable-candidate --example hust_runtime_probe --manifest-path artifacts/durable-host-minimal-20261017/Cargo.toml -j 4
```

aarch64 helper 86 项、严格 Clippy、八场景 Rust↔FrontendEnds HTTP CPU 协议联调及完整宿主 39 项均通过；最终正常 release 二进制也已构建（测试二进制不替代它）。原宿主的 deprecated fetch_update / 组合 dead-code 告警保留，不宣称整个宿主严格 Clippy 全清。

本轮运行二进制 SHA256 `37e255f35c6dc203881e0357988c7b7a2b762868f1be3a432c0fed088c38af05`。其源码摘要与[CPU 记录](hust-terminal-transport-2026-10-22.md)相同，发起 NPU 前再次逐项核验；不是旧 `f4fa5fe...` 资格二进制。

## 现场诊断修正

初次运行旧 inspect_runtime，因硬编码 `vllm-ascend-hust` 元数据名报错；固定候选 profile 实际使用 `vllm-ascend`。新增显式 `--profile`，先核验版本和接口指纹，再导入并按该映射报告，不对其他包名静默回退；不带 profile 的旧库存查询仍保留。

第二次报告因 Triton 包注册了 amd/ascend/nvidia 而失败。审查现场 `triton/runtime/driver.py`，其 `_create_driver` 按各 driver.is_active() 筛选且要求唯一；当前三个注册项中只有 Ascend 可激活。诊断改为同时报告注册和 active 集合，拒绝零个、非 Ascend 或多个 active，不按目录存在误判。最终 profile/import/active 子集通过，仍不将 environment_gate_passed 置真。延迟导入 profile，独立 `--help` 不要求先装 MOD/引擎。

诊断新增 9 项 CPU 回归，Windows 统一 CPU 605 通过、7 跳过、3 deselected，integration 3 通过，Ruff/格式和文档链接检查通过。上述修正只在工具层，无服务环境安装或降级。

全量 pip check 仍失败：affinity-sched/mindstudio-kpp/ms-service-profiler/schedule-search 的缺依赖，以及 profiler 的 OpenTelemetry/pandas 版本约束。原始审计保留在忽略区，不把 profile/import 子集成功写成整个环境审计通过，也不为消除该诊断而调整当前推理依赖。

## 双 NPU 资格

使用新建输出目录和以下显式模式，资格参数仍是 1024 上下文/小 KV 预算，不是生产容量参数：

```bash
env -u PYTHONHOME -u VIRTUAL_ENV \
  artifacts/hust-cedb477-rebuild-20261013/service-venv/bin/python -m tools.hust_runtime_chain \
  --execute --model /data/shared_models/Qwen2.5-14B-Instruct \
  --binary /root/work/prefix-routing-mod/artifacts/durable-host-minimal-20261017/target/release/examples/hust_runtime_probe \
  --output artifacts/c22-server32096-driver \
  --pycache-root /root/work/prefix-routing-mod/artifacts/g2-recovery-20261011/new-server/pyc \
  --durable-store --managed-terminal-retirement --use-terminal-driver
```

状态：本轮新二进制双 NPU 有限资格通过，报告明确 terminal_driver_transport_verified=true。实际目标 A/A/B、cached_tokens=0/384/384；生成后取消取得核心 FINISHED_ABORTED=1，原生目录订阅、持久 commit→ACK→retire 与暂停重开检查通过，retired_families=4、active_after_terminal=0。两份后端 parent_returncode=0、forced_group_cleanup=false，结束后 npu-smi 无自有推理进程。

原始报告 `artifacts/c22-server32096-driver/report.json`，本地副本 `artifacts/c22-evidence/server32096-driver-report.json`，均忽略入库；SHA256 `0c19e9857e27c3aa420981cd570a3bfae6e956666003c539914aa98a6cd320df`。没有注入回复丢失，retire_response_losses=0；报告中的 retire_response_loss_recovered=true 只表示计数与请求模式相符，不能解读为本轮故障注入通过。durable_recovery_verified/production_lifecycle_verified/performance_verified 保持 false。静态 interface/doctor 的保守能力布尔值不因这次有限链而自动提升，实机范围以 chain 字段为准。

## 仍未交付

该有限路径在请求之间调用 Driver.step；常驻 run、HTTP/readiness/信号排空、Manager 激活和生产目录/公开配置仍待装配。真实 ACK/retire 回复丢失、大量 churn 和崩溃恢复属于此前路径的资格，本轮没有宣称在新路径重新覆盖。MOD 仍 import_only，没有新增容量/性能结论；源码和自动化自审不替代独立成员评审，保持草稿 PR、不自动合并。
