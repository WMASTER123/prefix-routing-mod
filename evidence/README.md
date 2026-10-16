# 证据约定

[双 TP1 原生 KV 目录复验（2026-10-16）](two-replica-native-directory-2026-10-16.md)记录固定 HUST/Qwen14B 两卡真实 KV 事件、隔离、断连降级与制品漂移前置检查。

[固定 HUST 原生 KV wire 的 Rust 适配（2026-10-16）](native-kv-wire-rust-2026-10-16.md)记录有界 MessagePack 解码、指纹连续性窗口、原索引回归和固定 Router 宿主构建。

artifacts/ 为 Git 忽略的原始结果区，GitHub CI 自动归档短期产物。正式验收应另行保存有校验和的持久记录；CI 14 天保留不等于永久实验档案。

每次记录项目/依赖提交与 dirty 状态、命令、硬件/软件、模型/模板指纹、配置、负载版本/种子、时间、原始结果、结果类型和已知局限。

结果类型必须区分：静态检查、CPU 单测、mock 集成、微基准、仿真、回放、真实端到端。pass 只能指向明确套件；not-run/skipped/inconclusive 不能被汇总为性能或发布通过。

只提交脱敏摘要。不得提交个人 token、私钥、用户 prompt/输出或公开数据集之外的敏感样本。性能声明需要链接对应已验收结果；当前 MOD_METADATA.performance_claims 为空。
