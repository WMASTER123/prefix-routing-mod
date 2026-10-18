# 增量存储与真实持久公开链资格

日期：2026-10-17。代码输入：`3264050b72eb4568eab996a07a7bc5423124fe90`；后续文档提交不改变本轮二进制输入。状态：CPU、存储对照和真实双 TP1 持久公开链有限资格通过；不是生产恢复或性能验收。

## 输入与范围

- HUST Core `ced6857afa0ea7b2e3f0846a62e1394e90f15607` / Ascend `b47795c63a8e63e7aed4d311f951428d94e045ab`，继续使用项目隔离 venv；未改 HUST 源码、全局 Python、CANN 或驱动。
- 原 Router `91b65ee4bdfbe98863a32a2205dced5dbb249ffe` 的已审私有宿主复制，显式 `durable-candidate`；实际目录 `artifacts/durable-host-minimal-20261017`。原默认关闭路径保留。
- aarch64/openEuler；两张 910B2，本次物理 4/5、逻辑 0/1；`cpu.max=800000 100000`，`memory.max=68719476736`；项目数据库位于 ext4。配额不等于独占物理核心/磁盘。
- 公共 Qwen2.5-14B-Instruct，只读模型；FP16/eager、两个 TP1/DP1、group0/block128、1024 context/max-seqs2 功能配置。不是图模式或容量调优配置。
- 新 wheel 干净安装校验通过；仅替换项目服务 venv 中本 MOD，`pip check` 通过。

| 输入/制品 | SHA256 |
|---|---|
| `hust_runtime_probe.rs` | `97a3024b9da1f64d94c28a8841bba65c1409915a02e5fb42da01ddaa8f8d714d` |
| 最终 ARM release binary | `d92e856069c2a51e3ed8f7df1f437a0f7b857144fdf9fbadc0f1565da48ee792` |
| 候选 Cargo.toml | `01c349c73db9f52372ac1a1e844e6f20129a0d1a604f63d784886e22de94a0d1` |
| 候选 Cargo.lock | `a323180a12d0219463ea5ddad8b2077007fe51114ee8f5aa6a4f5ecf98a6cca1` |
| 本次安装 wheel | `0216159584c4bf98d0519ebccc3e34ae8e7b018e2a4859f3a3c1105889e53fea` |

## 构建与依赖审查

`tools/stage_durable_router_probe.sh --execute <fresh-private-copy>` 在第二份干净复制重演成功，manifest/lock 与最终构建目录逐字节一致。锁审计确认：原包版本、来源、checksum 和原依赖边均保留，只新增 rusqlite 0.40.2、libsqlite3-sys 0.38.2、fallible-streaming-iterator 0.1.9，宿主根包增加 rusqlite 边。使用 `default-features=false, bundled`，不启用未使用的 cache/wasm 默认分支。

最终构建使用项目隔离 Rust 1.99.0 工具链、项目 OpenSSL sysroot、`CARGO_BUILD_JOBS=2`，执行 `cargo build --offline --locked --release --features durable-candidate --example hust_runtime_probe`，完成用时 10m17s。完整宿主产生既有弃用/示例未使用方法等 warning；不是全宿主零 warning 认证。共享 helper 的 Clippy 使用 `-D warnings` 并通过。

## CPU、CI 与审查

Windows Python CPU 576 passed/7 skipped/3 deselected，integration 3 passed；Ruff、离线 wheel 构建和干净安装通过。ARM helper 57 项通过；GitHub 代码提交 `3264050` 的两个 Python validate、两个固定 HUST source-methods 以及 Rust validate-helper 共 5 项通过，见[该次运行](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38995812664)和[Rust 运行](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38995812620)。原索引未改，本轮不将旧索引 CI 算为该 head 新跑成绩。

新增反例包含变化行事务回滚、SQL 触发器拒绝后重试、子进程在未提交事务中退出、已提交 active 保债、重开默认暂停、完整终态批尾错误不得部分结清、行键与 typed 身份错配、失败 load 清除 diff 基线。自审和自动回归已做；另一团队成员的独立人工审查尚未完成，PR 保持草稿、不自动合并。

## 相同持久性存储对照

最终构建结束、NPU 服务启动前执行；每项 50 次预热+1000 次计时，WAL/FULL，相同目录/配额，每次修改元数据及一条 active 记录（active=0 时仅元数据）。两轮反转布局与 active 顺序，结果单位 ms。没有本项目并发构建/模型服务，但未证明物理主机或磁盘完全独占。

| active | 完整快照 P50（轮1/2） | 行布局 P50（轮1/2） | 完整快照 P99（轮1/2） | 行布局 P99（轮1/2） |
|---|---|---|---|---|
| 0 | 0.670 / 0.652 | 0.633 / 0.689 | 2.666 / 2.519 | 2.718 / 3.597 |
| 64 | 0.990 / 0.992 | 0.846 / 0.840 | 4.404 / 4.464 | 3.901 / 3.696 |
| 512 | 4.239 / 4.665 | 2.598 / 2.577 | 10.557 / 10.366 | 5.330 / 5.086 |

原始 JSON/数据库：`artifacts/storage-final-20261017/`。更早 `artifacts/storage-20261017/` 与构建竞争，仅为诊断，不作为本表成绩。完整快照旧证据负载与本次不同，不直接比较数值。

判断：行布局在本机中等/较大 active 下减少了存储提交成本；active=0 尾延迟并不更好，不能宣称所有负载无退化。checkpoint 复制/比较/头部仍随工作集增长，不是 O(1)。本表只测 store.commit，不包含准入排队、分词、HTTP、真实终态或 Router TTFT，不能推导服务容量提升。暂保留显式候选，先做完整接线开销；group commit 后置，避免同时增加队列/取消协议。

复现形式：`sqlite_runtime_store_probe <new-path> 1000 <0|64|512> <snapshot|rows>`，数据库路径不得已存在。

## 真实公开链

先以既有 volatile 二进制完成 `artifacts/t8` 复验：A/A/B、cached_tokens 0/384/384、核心 ABORTED、active=0，正常清理；它不验证新持久化代码。

新候选命令（项目目录下）：

```bash
env -u PYTHONHOME -u VIRTUAL_ENV PYTHONPATH=/root/work/prefix-routing-mod \
  artifacts/hust-cedb477-rebuild-20261013/service-venv/bin/python \
  -m tools.hust_runtime_chain --execute \
  --model /data/shared_models/Qwen2.5-14B-Instruct \
  --binary artifacts/router-admission-20261011/target/release/examples/hust_runtime_probe \
  --output artifacts/t9 --port 18170 --durable-store
```

实际 `artifacts/t9` 运行返回 0、status=pass：真实公开入口 A 冷/A 热/B 热选择 A/A/B，cached_tokens=0/384/384；真实流式生成后断开，核心 FINISHED_ABORTED=1，最终 active=0。原生订阅继续写入同一个 M2。Router 和两个自有后端均 parent_returncode=0、forced_group_cleanup=false，结束后两卡无残留进程。

新增持久资格逐请求断言：响应后磁盘仍有对应 active；新读者重开为暂停、保留相同 attempt 的 uncertain 负债。完整终态与 cursor 提交后，使用旧水位无 ACK 仍能取回同一批；重复应用旧 cursor 被拒绝且不重复结清；随后才发送实际 ACK。HTTP 停止后重开验证 active=0、两源 cursor 与已消费位置一致、恢复准入仍关闭。

报告 `durable_commit_ack_and_paused_reopen_verified=true`；`durable_recovery_verified/production_lifecycle_verified/performance_verified` 仍为 false。这里的“重开”是有限装配中的额外读者，不是杀死 Router 后接管实际请求；没有执行 takeover 或重开准入。静态 profile 检查器仍保守保留 routing_ready 等资格标志为 false，不能将本轮子集通过改成完整产品能力。

总报告 SHA256：`083291038166e289cdd827448f0d0764ea027e65612b2016aea79f60296f48c4`；Router 结果日志 SHA256：`7c2ced6c557d5f2eee213168af35aeed59499897e115e540c6ed2582eff92062`。原始私有配置/日志/数据库仅留项目 artifacts；Git 不提交控制 token。

## 明确未完成

Frontend family/child 安全退役与长期资源趋势、外部直连流量共存、控制新鲜度、生产旧派发者围栏、真实 Router 强制退出及重新开放准入、图模式、Manager 完整启停、强基线/多阈值容量曲线均未通过。SQLite 子进程故障验证不等于真实 HUST 全链强制退出恢复。

重试记录：初始默认 feature 的宿主锁发生无关依赖变化，审计后停止并收缩 feature；另有源文件定稿前构建主动停止，均不作为最终制品。网络隔离构建前置缺包/本地 build 隔离下载 TLS 失败，分别用已审离线缓存解决；没有靠全局安装或忽略依赖检查放行。
