# HUST 版本映射、环境重建与原生 KV 实机子集

任务自 2026-10-13 启动，2026-10-14 收尾（Asia/Shanghai）。分支 codex/hust-cedb477-adapter 基于 41117eb；初始代码 c8cafc2，探针修正 0c0798b，审查代码 28b81a142d9958cff43ccb6729b9e28150834049。后续文档提交不扩大运行资格。

## 改动与输入

M7 增加集中 profile、安装接口检查及 backend-doctor；旧 publisher 常量/旧 wire 和 identity 工具复用同一映射/规范化与 digest。新原生三帧 PUB、四帧 DEALER replay 和 cache-spec/locality/ownership/session 显式适配，没有另建索引/代理。旧 hook 不在新版本启用。

只读复制用户指定另一项目的五个固定 wheel/两份 hash 锁，在本项目 artifacts 独立安装和重建；不修改其源码、环境或服务。来源与允许范围见 [ADR](../docs/decisions/2026-10-13-hust-version-adapters.md)，操作见 [迁移指南](../docs/planning/hust-version-adapters.md)。

| 输入 | 身份 |
|---|---|
| 当前分配 | aarch64、8核配额/64GiB/16GiB shm；两张910B2，枚举0/1，每设备65536MiB HBM；npu-smi26.0.rc1/CANN9.1.0 |
| Core | HUST ced6857afa0ea7b2e3f0846a62e1394e90f15607；vllm0.30.0+empty |
| Ascend | HUST b47795c63a8e63e7aed4d311f951428d94e045ab；vllm-ascend0.25.1rc2.dev766+hust.20260903.4.gb47795c63 |
| 软件 | Python3.12.13、Torch2.13.0+cpu、TorchNPU2.13.0rc1、Transformers5.14.1、Triton-Ascend3.6.0+git27efffea |
| 模型/配置 | 公共Qwen2.5-14B-Instruct；FP16/eager、TP1/DP1、APC、sha256/full-byte hash、seed0、128-token block、1024 context |
| 制品 | 五个wheel与两份锁SHA固定在 [manifest](../runtime/candidates/hust-cedb477.json)；含本地Arctic/Triton产物，不冒称公开release wheel |

184个runtime依赖，加FLA/expecttest和两引擎共188包；装本MOD后额外增加1包。profile验证新版本六个接口文件/元数据，并拒绝错版本、混装、错字节/路径逃逸；旧editable环境也显式通过新backend-doctor。顶层spec定位不执行engine；这不证明整个构建来源，doctor的source_provenance_verified/runtime_verified/full_recovery_verified保持false，另用环境/运行证据判断。

## 最终结果

| 检查 | 结果与边界 |
|---|---|
| Windows CPU | 28b81a1：490 passed、4 skips；ruff/format通过，4个既有平台/固定源码范围跳过仍保留 |
| Linux aarch64 CPU | 28b81a1：494 passed；integration3 passed |
| Windows integration | 3 passed |
| wheel/sdist | uv隔离构建；Windows外部干净wheel/default-off/packaged profile资源通过 |
| 隔离环境 | 首个新目录188包依赖检查通过，没有覆盖旧环境 |
| 全新脚本重建 | 新目录7步通过；hash锁/制品、依赖、补充包、Ascend Triton覆盖、两引擎、依赖与RECORD检查 |
| Triton覆盖 | 262个triton/文件、159个共用文件符合Ascend RECORD；不是全算子资格 |
| 设备0 | 0c0798b：3请求；completion449/chat478 tokens；重复completion cached tokens 0→384；2 live/2 replay batches、6 stored blocks规范化与空闲重放对齐 |
| 设备1 | 28b81a1：全新重建环境同一子集通过；model/tokenizer资产hash已记录；同样0→384、2/2 batches、6 blocks |
| 原索引接线 | 设备0捕获事件离线送入未改Rust索引，matched_blocks=[0,3,3]；控制确认前/lease过期/断连均unknown。身份/控制为离线fixture，不是在线目录或权威epoch资格 |

两卡为**顺序逐卡单实例**检查，未同时启动两个副本，不能报双副本路由/容量。通过目录的parent exit0、未强杀组，收尾npu-smi两卡无运行进程；这不认证优雅退出、零IPC泄漏或可靠业务终态。

28b81a1 wheel SHA为 `7e392fc6cc637353fa7576e6ac2a0cd81bbe594958fc7db76865b2172d092f29`，sdist为 `76e3b73078b5bd9997bc679bd3b16b7331a1ee9d5884e7d74c135f583d618384`。文档收尾制品若重建，另记hash，不改写实机包。

## 失败与审查修正

- 初次离线准备缺本地Arctic wheel；补入与锁一致的既有产物。随后缺Torch CPU索引元数据入口，显式使用原来源后184包安装通过。均为制品定位失败，没有换版本、放宽依赖或绕过hash。
- Windows长临时目录触发原IPC100-byte上限，fixture改用短绝对参数；真实边界不放宽。
- 首次NPU目录cedb477-npu-01已有一条HTTP200，但probe将null prompt_tokens_details当字典，整体fail。核对固定HUST launcher/serving后显式开启enable_prompt_tokens_details，并保留unknown；补6项缺失/错误值反例。原失败目录保留。
- 自审修正END_SEQ字节、缺包CLI结构化失败、editable位置、finally关闭，以及接口验证不得宣称全部源码来源。路径反例fixture随定位调整后曾未触发，已修正并重新通过，没有削弱真实检查。
- 两个通过目录在主动停服后仍记录HUST EngineDeadError；正常请求和自有进程清理通过。该日志是下一阶段启停诊断的实际问题，不计请求失败，也不宣布G3完整通过。DeepSelect/Mamba warning不扩大dense/eager资格。
- 干净wheel检查中pip更新提示的TLS查询失败，但本地安装与离线检查exit0；没有关闭TLS或改环境掩盖错误。
- 首次递归SCP备份包含socket和过长的Windows bytecode路径，失败后停止本任务SCP；改为排除cache/socket的日志归档，双端SHA一致。原始NPU报告和服务器缓存未删除。
- 自审完成；独立人工审查/合并待完成。原生Router/helper源码未改，本轮未重复其历史全矩阵；新CI必须绑定实际head，不继承PR17的success。

草稿 [PR18](https://github.com/WMASTER123/prefix-routing-mod/pull/18) 已建立并附任务，基于未合并的Router恢复分支。CPU checks [run38429895042](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38429895042) 已核对head为1761f57864cee63c75c77266eb191d04c09a7953，Python3.11/3.12的CPU/integration/build/wheel均success。后续记录提交另按其head核验，不继承本段成绩，不自动合并。

## 证据和未完成

原始目录位于本项目artifacts：hust-cedb477-20261013、hust-cedb477-rebuild-20261013、cedb477-npu-01、cedb477-npu-02、cedb477-npu-reviewed。必要报告/日志备份到本机artifacts，原件和失败不进入Git。

双端日志归档 `cedb477-evidence-20261014.tar.gz` SHA256为 `c5a852ef953841e7e0ae128d5b214fab7d31bf1730f84e6ddcc7068a1067cb92`；排除可重建cache和socket，保留失败、报告、原始backend日志及规范化输入。不是公开下载链接。

| 报告 | SHA256 |
|---|---|
| 首次NPU fail | 3a42d79053cd250a11798222075d8a8b28f4aacbcea6997fbabbdaad380c381b |
| 设备0 pass | f9f3f8ab6ebb7e4fe6a346de4769f22a921f307b9310910375795b1290f27e17 |
| 设备1/审查代码 pass | 3d69963c72dc0ee5dbbc44a1421d4676b681c201754f2d0ccd96f1a52305fc02 |
| 新环境restore | a4abbdd4c59ed29d971d93d0a924108713673d14124cd5f9c69e569528d8ad00 |
| 离线原index结果 | 3cc032c3fa88c0d0ac047a17e0a4488119e658dc7e622fcbd56e3a799d604969 |

尚未完成：新版真实入口/child/可靠ENDED对账、native超窗/热接管共同水位hook、在线M7→M2、长期GC/生产持久化、并发取消/图/持续运行、MOD启停、双副本强基线和SLO容量。MOD仍import_only，没有性能提升声明。下一步按 [新版接线指南](../docs/planning/hust-version-adapters.md)推进；HTTP结束/END_SEQ不能释放未知负债。
