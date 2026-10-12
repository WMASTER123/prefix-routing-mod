# 关联封口、事务恢复与 KV wire 接线证据

日期：2026-10-12。分支 `codex/terminal-capture-recovery`，基础 `3f0fcb4`（PR #13）。代码/文档增量为 `72952ed`、`b4caf71`、`7cef70c`；结果按下方范围标注，不把 CPU 实验算成 NPU 验收。

## 结果与范围

| 项目 | 结果 | 精确范围 |
|---|---|---|
| Windows Python | 376 pass / 4 平台 skips，integration 3 pass | b4caf71 的 Python 内容；后续 7cef70c 只改 Rust |
| Linux aarch64 Python | 380 pass，integration 3 pass | b4caf71 |
| Rust 原生目录 | 58 pass | 7cef70c 对应 Rust 源码，锁定 toolchain 1.99.0 |
| 请求关联方法 | 10 项全部 true | 固定 HUST 七文件，n=1/3、提前结束/部分失败/取消；直接 CoreRequest 分支和 CPU 协作者 |
| 旧终态方法 | 25 项全部 true | 既有四文件方法审计，未启动完整引擎 |
| 原生 codec→原索引 | matched_blocks=[2,1,0]，确认前/过期/断连均 null | 固定 HUST KV Struct/msgspec 0.22.0 的合成四帧，真实 CPU Rust index；非 NPU KV |
| 新 sdist/wheel/干净安装 | 两端 pass | b4caf71 Python 内容；MOD import_only、默认关闭入口不导入引擎 |
| CI | 创建 PR 后检查确切 head | 不提前宣告未完成状态 |

当前远端只有 CPU。HUST 工作树固定 `13f11da...` 且未修改；原 Router 两个源文件和宿主 lock 不变。实验 crate 添加固定 serde/serde_json，lock 仅增加依赖，没有升级原 dashmap/parking_lot 依赖。构建格式化产生的工作副本已逐文件比较，保存在可恢复 stash 后 fast-forward 到本地提交；没有覆盖不明修改。

## 正确性和自审发现

- 真实 AsyncLLM 是逐 child await 提交；first child 可能提前结束。现在先预留期望数量，捕获实际 internal/external ID，在全部成功提交后 seal；部分失败/取消 abandoned 不结清。
- 新接收 owner 保留同一 ReferenceLedger 语义；SQLite FULL commit 后返回 applied。一个接收 owner 给 transport 与后端确认分配序号；重复/迟到回调收敛，不额外消耗序号。已提交 observer receipt 保存原始 lifecycle 载荷，重启重试不重造时间或事件。
- 子进程用 os._exit 在提交前或提交后/ACK 前退出；逐条与 group commit 都实际重开数据库。未提交事务保留原负债，已提交终态重复幂等。日志缺失、序号/身份冲突、超预算均不清零；恢复后新派发暂停。
- wire 自审发现 mandatory parent/LoRA 缺项不能等同 None，Python 与 Rust 入口已分别拒绝。
- pool 自审发现迟到恢复结果绕过原 Ticket 的风险，已将 begin/install/queue/live/confirm 的 current ticket 检查贯穿封装；旧任务不撤销新视图。control lease 从请求发送时点计算，迟到回复不能以收到时点续期。当前恢复阶段队列异常会撤销信任。
- 控制新鲜度过期返回 unknown，不是 miss；PUB 到达不续期。所有分区声明额度之和受注册预算限制，是逻辑额度约束，非 RSS/硬实时保证。

首次 codec 联调在服务 venv 找不到项目包而失败（未安装该包）；使用本次进程 `PYTHONPATH=$PWD/src` 明确从当前源码加载后通过。没有改服务 venv 依赖或绕过 hash 守卫。

## 可复现入口

Python 全量与制品仍用统一 tools/check.py / tools/verify_wheel.py。固定源的本地副本在项目 artifacts，不能加入 Git：

```text
python -m tools.audit_request_capture --source-directory artifacts/terminal-audit-20261012/source --output <新报告路径>
python -m tools.audit_backend_terminal --source-directory artifacts/terminal-audit-20261012/source --output <新报告路径>
```

请求审计多需 async_llm.py/input_processor.py/parallel_sampling.py，来自同一固定 HUST；完整 SHA256 在工具常量。原生 codec 仅使用既有隔离服务 venv 的 msgspec，工具不导入 torch/vllm：

```bash
PYTHONPATH="$PWD/src" artifacts/hust-runtime-20261011/service-venv-rebuild/bin/python -m tools.probe_kv_wire_normalizer --source-file artifacts/hust-runtime-20261011/vllm-hust/vllm/distributed/kv_events.py --output <新的项目 artifacts 子目录>
cargo test --offline --locked --release --manifest-path experiments/router-index/Cargo.toml
cargo run --offline --locked --release --manifest-path experiments/router-index/Cargo.toml -- wire experiments/router-index/fixtures/wire-v1.json
```

Rust toolchain/source/target 的隔离变量按 experiments/router-index/README.md 设置。原始 codec 手动验证；CI 使用同源规范化 fixture，不启动 HUST。CPU 输出位于 artifacts/capture-recovery-20261012；旧报告保留，不覆盖失败。

## 开销与计划修正

同一 aarch64 CPU 分配上，20 个合成 family 逐请求各三次 FULL SQLite 事务，中位 11.31 ms，p95 13.09 ms，最大 17.44 ms；60 条日志重开约 8.77 ms，文件 40960 bytes。

有界批量候选以三次事务接收同量 60 条元数据，共 28.85 ms，摊销 1.44 ms/family。它不包含 batching timer、真实网络、完整引擎或 NPU；不是个体请求延迟，也没有相同到达模型的性能对照。不得用摊销值声称 Router/模型加速。

结论：同步逐请求 SQLite 不能直接进入生产热路径。保留故障语义参考，继续比较有限 group commit 的等待/队列成本和可靠后端对账支持的恢复；存储与语言没有冻结。当前 reference ledger 有扫描成本、SQL 日志/observer tombstones 没有 GC，预算满停收，不是长期服务实现。

## 交付边界

详细[接线范围](../docs/planning/capture-recovery-wire-boundaries.md)已同步到三份活计划和 workspace 根副本。尚未完成真实 HTTP 总组关联、原生 M5 跨进程可靠传输/鉴权、重启时钟交接、全池生命周期/资源成本、真实 KV 订阅、引擎安装/关闭以及 NPU/性能验收。自审完成，另一成员独立审查仍待完成；不自动合并。
