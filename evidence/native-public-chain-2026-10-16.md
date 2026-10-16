# 原生 Rust KV 与真实公开 HTTP 链

日期：2026-10-16。结论：固定 HUST/Qwen14B 的有限真实公开请求链通过；M3 持久恢复、长期回收、生产启停与容量尚未通过。

## 环境与制品

两张 910B2，物理 0/7、各 65536MiB，8 核 CPU 配额/64GiB Host。HUST Core ced6857 / Ascend b47795c，Qwen2.5-14B-Instruct FP16/eager、两个 TP1/DP1 服务、block128。Router 为原 91b65ee、原 Cargo.toml/lock 与三文件补丁；没有修改 HUST 引擎源码或更换设备栈。

本轮 MOD wheel SHA256 为 `bff990e8385fc922307a645a5b8001fd592b1fc581093257c066099e535e3ea6`，在旧服务退出后安装到项目 rebuild 服务隔离环境，pip check 通过。新增独立身份接口沿用默认关闭、显式 allowlist、token 和固定源码守卫。

`hust_runtime_probe.rs` 源码 SHA256 为 `b6c29df538300e587244a347e7bc47825efa1836066793af6a7ed4ff93c82873`，与实际构建输入核对相同；代码提交 89ffaf1。二进制 SHA256 为 `280d663653a321115653da57aea0bb3304eb5065da1f6e84535be01b4b8d3bd5`。

## 实际链路与结果

原 `build_app`/HTTP 入口 → 真实本地 Qwen tokenizer/M1 → 同进程原索引/M2 和 M3 → 基础 M4 → M3 reserve → 原 M5 HTTP/SSE → 固定 HUST → 真实完整核心终态 → 同一 M3。每副本一个 Rust SUB/DEALER 与身份控制 owner；Python 只启动/检查/收尾，不转发模型请求。

副本 B 的公开合成前缀由直接请求预热，真实事件确认 B 三块驻留、A 无该驻留。该预热单独记录，不计为 Router 请求或性能收益。

| 公开入口请求 | M3 实际目标 | 后端实际 cached_tokens |
|---|---|---:|
| A 前缀冷请求 | 副本 0 | 0 |
| A 前缀重复请求 | 副本 0 | 384 |
| B 前缀请求 | 副本 1 | 384 |

每次 HTTP 返回后保留在途负债；核验整个终态批次、实例和完整 family 后结清，重复应用返回 Duplicate。数组 prompt 明确拒绝且未新增负债。流式收到实际生成内容后断开，账本仍有 1 笔负债，真实核心随后报告 `FINISHED_ABORTED: 1` 才归零。HTTP 停止后关闭目录 owner，两个视图均为 unknown。

## 构建、审查与清理

- Linux x86_64 与服务器 aarch64 的共享原索引/owner/IPC/身份控制 release 回归各 90 项通过；固定完整 Router example test 36 项通过。
- 最终固定宿主的既有 mock HTTP 组合探针通过：两个实际转发目标、实例替换、断连和全池额度回收没有退化。
- 首轮隔离宿主漏复制 Directory/Pool，保留失败日志；补齐后重跑通过，随后增加统一 staging 脚本。源码 guard 和 Cargo 锁未放宽。
- 自审补了重放发送期限、部分发送歧义失信、控制身份认证/大小/重定向限制和完整终态批次数值上限；自动测试不等于独立人工审查，PR23 仍待人工复核。
- Router 和两个服务父进程退出均为 0，无强制进程组清理；结束后两卡无本项目残留设备进程。

副本 1 在显式 drain、EngineCore 请求处理结束并完成资源拆除后出现一次 AsyncLLM output_handler/EngineDeadError，随后 API 正常退出。已核对时序并保留原日志；不能将运行期同名错误一概当成关闭噪声。

| 原始文件（项目私有 artifacts） | SHA256 |
|---|---|
| `t6/report.json` | `ba9be9c007d67c9cefd90471cc180971ec4f7b83e5a31cd678f041c6b69aa059` |
| `t6/router.log` | `1d00f98d89af75d2e30591a0a1a5a9200dc0431f2482f09562fc2b7a3e448303` |
| `native-transport-final-20261016-retry1/index.log` | `7376ba16877617e52c3567db94f3ecef6bbfab2c180581875e714c9c1dee6cc8` |
| 同目录 `host-test.log` | `043bd237129964dee6884aa64ad41e54ba98d06ff2afd3fa9a1f6b011ab98e81` |
| 同目录 `host-http/report.json` | `ce2dcbf462e0300ee22b3c19bad9d6da31af633d8338842efb847d40febb74d5` |

## 未通过的门槛

此次只支持受控空起点原生已发布事件和有界字符串 completion。终态测试有意不 ACK，因为 M3 尚无生产持久提交；不能把测试结束后清理进程当作长期历史回收。控制健康新鲜度、任意 Router 崩溃/恢复、源端 GC/ACK 协议、正式制品/Manager、chat/template、图与性能仍待验收。后续集中 [持久化与回收](../docs/planning/durable-runtime-and-retention.md)，不再把各已过数据面资格反复当下一阶段目标。
