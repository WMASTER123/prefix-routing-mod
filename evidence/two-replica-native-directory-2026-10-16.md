# 双 TP1 原生 KV 目录复验

日期：2026-10-16。结论：固定 HUST/Qwen14B 的双副本数据面资格通过；这不是公开 Router 全链或性能收益证明。

## 环境与对象

- 两张物理 910B2，各 65536 MiB HBM；8 核 CPU 配额、64 GiB Host 内存。
- HUST Core `ced6857afa0ea7b2e3f0846a62e1394e90f15607`，`vllm 0.30.0+empty`。
- Ascend `b47795c63a8e63e7aed4d311f951428d94e045ab`，`vllm-ascend 0.25.1rc2.dev766+hust.20260903.4.gb47795c63`。
- 公共区 Qwen2.5-14B-Instruct，FP16/eager、TP1/DP1、block size 128；每个副本独占一张卡。
- 原 Router 索引探针 SHA256：`6b771d44a16b7b153c39a7209720db77e1a2eef40750cfe12ecd23fbeac54880`。

## 首轮失败与修正

`artifacts/r3` 在两个模型均可启动后失败于 terminal 控制端点 404。根因不是引擎依赖冲突：所选项目隔离环境安装的是同版本号的旧 MOD wheel，只有 publisher entry point，没有后续新增的 terminal general/endpoint entry points。源码版本和已安装制品发生漂移，而静态版本号没有暴露它。

修正没有更换 HUST、CANN 或系统 Python：使用依赖检查通过的 rebuild 项目隔离环境，安装从提交 `c484e22` 构建并经干净环境验证的 wheel（SHA256 `791d07fd7b7c8f932e188ac329b89bb521160d0370c991cb585865da211209b3`）。提交 `dea9b00` 又增加模型启动前的固定 entry point 完整性检查，缺失、重复或目标错误都会立即拒绝。

## 通过结果

`artifacts/r4/report.json` 状态为 pass：

- 两个 TP1 副本同时存活，服务路由表均包含 terminal 读取和禁用端点；
- 副本 0 的同一 449-token 请求实际 `cached_tokens` 为 `0 → 384`；
- 副本 1 的同一 450-token 请求实际 `cached_tokens` 为 `0 → 384`；
- 每个目录接收 1 个原生 batch、3 个 store 操作，分别写入同一个原索引 owner 的独立分区；
- 单边驻留没有污染另一副本；关闭副本 0 的目录后，该分区变为 unknown，副本 1 仍保持完整匹配并继续产生实际缓存复用；
- 两个服务父进程返回 0，未强制清理进程组；实验结束后两卡无本项目残留进程。

| 制品 | SHA256 |
|---|---|
| `r4/report.json` | `5f9fd885c279a34f2a29c434e534ed284d2e1036e0547037abf26a151357c8ba` |
| `r4/replica-0/backend/backend.log` | `1d5756daf6f19b1e0ccf1ac26a0dc0c370a61d16d21f6d013caa77512a1f0850` |
| `r4/replica-1/backend/backend.log` | `afc80f8b077673baa46bd7a3859676b23a4e0e87314887023640613b8af6fed9` |

## 日志审查和边界

两个服务都在显式 drain/SIGTERM、EngineCore 完成资源拆除之后记录了一次 `AsyncLLM output_handler failed / EngineDeadError`，随后 API application 正常 shutdown，父进程返回 0。它是当前 HUST 关闭路径的日志噪声，不是请求或模型运行失败；仍保留原日志，后续持续运行测试不得用该说明忽略运行期的同名错误。

本轮 Python subscriber/stdin 索引 owner 只用于资格验证。原生流仍没有 publisher epoch 和完整共同水位 token 快照，因此只支持受控空起点，不支持宣称任意已热服务接入或超窗恢复。`full_routing_verified=false`、`performance_verified=false` 保持不变。下一步把同一 wire 语义和连续性围栏移入固定 Router 的 Rust 同进程适配层，先做 CPU 反例和真实订阅接线，再接公开 M1/M3/M5 链。
