# 原索引持续更替审计

2026-10-12 增加 `wire` CPU 模式，消费 M7 规范化的合成 snapshot/delta 到同一 Directory/KvBlockIndexer；fixtures/wire-v1.json 由固定 HUST Struct/msgspec 0.22.0 构造，不含 token 序列。Pool 保留原恢复 ticket，确认时间是控制请求发送时间；分区声明逻辑额度之和受全池注册预算限制。当前不包含真实订阅、完整多分区生命周期或 RSS 硬限制。

```bash
cargo run --offline --locked --release --manifest-path experiments/router-index/Cargo.toml -- wire experiments/router-index/fixtures/wire-v1.json
```

预期 matched_blocks=[2,1,0]；控制确认前/lease 过期/断连为 null。原索引 CI 校验同一 fixture；固定 HUST codec 的原始构造工具手动运行，不由此宣称 CI 启动了 HUST。

2026-10-12 增加 `adapter` 模式及目录故障/并发测试，直接调用原 `KvBlockIndexer`。`src/directory.rs` 是受限 CPU 可行性 adapter：完整快照加连续增量、恢复任务/连接代次隔离、身份匹配、缺失父链/别名拒绝、clear 和预算降级。不是第二套匹配索引，也未接实际 HUST wire。

```bash
cargo test --offline --locked --release --manifest-path experiments/router-index/Cargo.toml
cargo run --offline --locked --release --manifest-path experiments/router-index/Cargo.toml -- adapter 800000 4096
```

普通事件批次按有界操作数做增量维护，不复制整个目录或遍历全树。视图在 owner 写锁内变化；读者返回匹配值，不持有旧索引 Arc。到历史限额时删除不再需要的历史元数据，保留所有驻留项和必要祖先后重建。恢复期间不开放旧视图；完整排他水位和连续覆盖满足后才安装。旧任务不能撤销或安装新任务的目录。

仅一个分区、group 0/device/DP 0；身份来自调用者已验证的摘要。预算涵盖记录、单批操作、缓冲批次数/字节、查询长度、父链深度与重建 overlap 的逻辑计费；批次暂态最多记录上限加单批操作数，最多两个索引对象。实验 parent_depth 仅接受 1–1024，超限明确退出精确模式，避免原树销毁深度无界；不是已经验证任意模型最大上下文。上限是实验配置，不是生产默认或 allocator 的硬 RSS 保证。生产还需注册分区总预算、解码前限制、控制面新鲜度及重建的长前缀/大工作集延迟实验。相同位置但不同引擎锚点的别名拒绝并降级，不让原索引默默混合它们。

新增 `Native index adapter checks` 对上述固定原文件做 hash 守卫、Rust 格式/原生语义及短更替检查；独立于 Python CI。aarch64 HTTP 原生编译和 NPU 仍手动执行，不能从这个 CI 推导整套 Router 支持。

实验直接编译固定 Router `91b65ee` 的 indexer.rs/types.rs，按 SHA256 守卫；不复制或修改索引算法。上游为 Apache-2.0，仓库根 LICENSE 保留相同许可。最小依赖版本与该候选锁一致。

源码守卫只接受同一固定 Git blob 的 LF 与已审计归档的 CRLF 两种精确 hash；内容逐字归一化后相同，已在 GitHub 固定提交重新核实。任意其他源码修改仍拒绝。CI 初次下载 LF 触发旧 CRLF-only 守卫的失败已保留，不能通过关闭守卫或扩大版本范围解决。

```bash
export ROUTER_SOURCE_DIR="$PWD/artifacts/router-admission-20261011/source"
export CARGO_HOME="$PWD/artifacts/router-admission-20261011/toolchain/cargo"
export RUSTUP_HOME="$PWD/artifacts/router-admission-20261011/toolchain/rustup"
export PATH="$CARGO_HOME/bin:$PATH"
export CARGO_TARGET_DIR="$PWD/artifacts/router-admission-20261011/index-target"
cargo run --offline --locked --release --manifest-path experiments/router-index/Cargo.toml -- original 200000 4096
cargo run --offline --locked --release --manifest-path experiments/router-index/Cargo.toml -- rebuild 200000 4096
```

固定保留 64 个独立的实际索引驻留条目（非 NPU KV），持续 store/remove 其他唯一前缀。对照原索引与每个预算周期从合成完整快照重新构建的索引，检查保留条目匹配不丢失，报告进程 RSS、重建前后采样峰值及 Linux VmHWM。进程峰值不是索引独占分配量；重建计时不是线上开销承诺。

`cargo test --offline --locked --release --manifest-path experiments/router-index/Cargo.toml` 同时执行原索引的 24 项测试及本项目的缺失 parent 反例。反例证明不能直接把不完整快照的 child 当 root 导入；它没有修复生产 adapter。

这是资源与可行性实验，未接生产事件、并发读者或真实 HUST 快照。预算是实验变量，不是生产默认配置。生产重建还必须处理共同水位、原子安装、恢复队列、读者留存、父链完整性、域/epoch 及总字节上限；超预算保持 unknown 并回退基础策略。不得简单删除旧 anchors 后仍宣称整个目录可信。
