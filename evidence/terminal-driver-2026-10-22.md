# 终态持续驱动 CPU 候选与新服务器复验

日期：2026-10-22。这是两类不同证据，不能合并为生产生命周期通过。设计见[ADR](../docs/decisions/2026-10-22-terminal-driver.md)。

## 两卡有限复验

用户核验新分配主机的 SSH 指纹后连接；项目专用 known_hosts 未进入 Git。远端旧修改逐文件/blob 核对与 `70c56a2` 相同，保存在 stash 后切换到该提交，未覆盖外来修改。只在 `/root/work/prefix-routing-mod` 操作，无全局环境、HUST 源码、驱动或 Manager 启用变更。

新分配为 aarch64、8 核 CPU 配额、64GiB Host，两张 910B2，现场物理设备编号 5/6；测试沿用两个可见逻辑设备 0/1、公共 Qwen2.5-14B-Instruct、两个 TP1、FP16/eager。实际来源仍为 HUST Core ced6857 / Ascend b47795c，不根据上游版本字符串推断兼容。

执行既有 `tools.hust_runtime_chain` 的 managed durable + exercise-control-gate 模式：

```bash
artifacts/hust-cedb477-rebuild-20261013/service-venv/bin/python -m tools.hust_runtime_chain \
  --execute --model /data/shared_models/Qwen2.5-14B-Instruct \
  --binary /root/work/prefix-routing-mod/artifacts/durable-host-minimal-20261017/target/release/examples/hust_runtime_probe \
  --output artifacts/c22-server32054-control \
  --pycache-root /root/work/prefix-routing-mod/artifacts/g2-recovery-20261011/new-server/pyc \
  --durable-store --managed-terminal-retirement --exercise-control-gate
```

- 冷/热/另一副本请求实际 cached_tokens 为 0/384/384。
- 生成后取消取得真实 `FINISHED_ABORTED`，不是以 HTTP 结束清债。
- 真实身份 HTTP 503 两次、停止刷新后 TTL 到期、同来源重新验证、注入 observer 冲突一次、另一副本继续服务、保债与 drain 拒绝全部通过。
- 共 retire 9 family，active=0；两后端均自然退出，returncode=0、forced_group_cleanup=false。最终 npu-smi 无自有推理进程。
- 本次没有注入 retire 回复丢失，没有新做 Router 强退；report 中相应布尔值不可解释为本轮重新覆盖了这些故障。

远端原始报告为 `artifacts/c22-server32054-control/report.json`；本地保留在 `artifacts/c22-evidence/server32054-control-report.json`，两者忽略入库。报告 SHA256 `e73b046c875556846056ffea306e801494b1dbbc30bdc40a45f443855fb8a8d6`。实际运行的旧资格二进制 SHA256 `f4fa5fe0400b63660a8b1730a9f1d2e17d94ac308f5b6f829c79c6098b40eda6`。报告无随机身份/时间字段，本轮与上一服务器的相同结果可以具有相同摘要；这不是新驱动实机运行记录。

## 新增驱动及审查

唯一源码 `experiments/router-extension/terminal_driver.rs`，checks 直接包含；不新增依赖或第二套账本。复用 DurableRuntime 与原 SQLite WAL/FULL，整批 commit 成功后才 ACK/受管 retire。active=0 时仍补确认，启动先重发持久水位确认，已提交批次不重复释放。运输错误明确区分可重试失联与来源/协议错误；单批≤64，循环间隔显式限制，不保存第二份历史。

自审发现仅靠上层监督不够：终态任务可能被丢弃而 HTTP 还在。增加 SupervisionFence，任务正常停止、监督断开、future abort 或 panic 展开都关闭 owner 新准入。请求停止等待当前操作，不取消持久提交。已有 MutationFence 继续处理提交未来被取消的不确定性；强退仍依赖原持久恢复资格。

新增 12 项 CPU 测试覆盖提交早于 ACK/retire、两种确认丢失、active=0 继续、恢复持久 cursor、observe_only、失联保债、错来源/序号/未知 attempt、存储提交前/后错误、来源映射、停止、超大批次及确认中 stop/abort。实际存储用独立临时 SQLite 行布局；运输为替身，不是 NPU/HUST 实机来源。

验证结果：

```bash
cargo +1.99.0 fmt --manifest-path experiments/router-extension/checks/Cargo.toml --check
cargo +1.99.0 test --locked --manifest-path experiments/router-extension/checks/Cargo.toml
cargo +1.99.0 clippy --locked --manifest-path experiments/router-extension/checks/Cargo.toml --all-targets -- -D warnings
cargo +1.99.0 test --release --locked --features durable-candidate --example hust_runtime_probe --manifest-path artifacts/durable-host-minimal-20261017/Cargo.toml -j 4
```

Rust、OpenSSL 使用项目既有私有 CARGO_HOME/RUSTUP_HOME/build-sysroot，见[此前构建记录](control-admission-2026-10-22.md)；没有创建新全局工具链或更换锁文件。

- Linux aarch64 helper 80 项通过；同一 crate 的 `clippy --all-targets -- -D warnings` 通过，无新增 lint 例外。
- Windows 统一 CPU 590 通过、7 跳过、3 deselected；integration 3 通过，Ruff/格式、隔离 sdist/wheel 构建与干净安装通过。这些 Python 测试不替代 Rust 测试。
- CI 对旧 `70c56a2` 的 6 项检查已成功；新增驱动需其自身提交 CI，不能继承旧成绩。
- 固定完整宿主显式包含同一最终 driver 源码，release example 编译及 39 项测试通过。仍有原宿主 fetch_update deprecated 和现有组合 dead-code 告警，不宣称整个宿主 Clippy 全清。有限 probe 仍走原 reconcile，不等于已接入新循环。

源码 SHA256：driver `3e279f71dfab4bac387ee5a6eed280481c3c55a8ffc2ffab137fa3878bcff28d`；驱动测试 `26cd72a044913d46bb6bab2dfc6e64fc0fff69a62786f27915db68126f17cc50`。

## 下一步与未解决项

1. 将固定 HUST 的来源/PID/mode/完整 family 校验提取为共享、限时、有界运输适配；ACK 可能携带更新记录，不照搬有限 probe 的“必须为空”断言。不得同时运行两个终态消费者。
2. 与公开请求入口、启动恢复、readiness 和信号排空装配；分别确认债务、HTTP 工作和 pending ACK/retire，超时保留未决状态。模块的 run() 不是完整服务。
3. 形成生产目录/公开配置/错误码/制品，再验证 Manager 监管；当前 manifest 仍 import_only。
4. 完成同底座开销，再做双副本强基线和多阈值容量曲线。没有新增性能提升结论，未扩大后端/模型/图模式支持范围。

本轮为自审及自动化回归，不冒称独立人工审查已经完成；草稿 PR25 已建立且旧提交 CI 通过，未自动合并。审查发现仓库 API 为 public，与最初 private 要求不一致，已询问用户；确认前新增驱动只提交本地和同步服务器，不推送公开远端，不继承旧 CI。未擅自改变可见性。
