# 原索引持续更替审计

实验直接编译固定 Router `91b65ee` 的 indexer.rs/types.rs，按 SHA256 守卫；不复制或修改索引算法。上游为 Apache-2.0，仓库根 LICENSE 保留相同许可。最小依赖版本与该候选锁一致。

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
