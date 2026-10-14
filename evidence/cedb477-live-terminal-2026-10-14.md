# HUST 真实入口、核心终态与原账本：有限实机验收

日期：2026-10-14（Asia/Shanghai；原报告 UTC）。基于 42d7dbb，分支 `codex/cedb477-live-terminal`；最终实机功能代码 **92cc06c**，无未提交功能改动。后续文档提交不自动产生新的硬件资格。完整范围及下一步见[执行说明](../docs/planning/cedb477-live-terminal.md)。

## 环境与制品

新分配 SSH 32068；仅 `/root/work/prefix-routing-mod`。aarch64、8 核 CPU quota、64 GiB RAM、2×910B2，物理 5/6 各 65536 MiB HBM。逻辑 0→5、1→6；顺序单实例，未并行双副本。公共 Qwen2.5-14B-Instruct，FP16/eager/TP1/DP1，max-model-len=1024、max-num-seqs=2、70% HBM、APC/chunked-prefill、block_size=128、sha256；完整 argv 和模型三项配置指纹在原报告。

继续原隔离 wheel 环境：Core ced6857afa0ea7b2e3f0846a62e1394e90f15607 / Ascend b47795c63a8e63e7aed4d311f951428d94e045ab；包分别为 0.30.0+empty、0.25.1rc2.dev766+hust.20260903.4.gb47795c63。Python 3.12.13、torch 2.13.0+cpu、torch_npu 2.13.0rc1、CANN 9.1.0。没有更换引擎、全局依赖或设备栈，只在项目服务 venv 重装本 MOD wheel。

默认 shell 的 SDK PYTHONPATH 暴露 profiler/工具包 dist-info，引入缺失 argparse/plotly 及 pandas/OTel 等诊断；清除 PYTHONPATH/PYTHONHOME/VIRTUAL_ENV 后，用项目 uv 对服务解释器检查 **189 个包全部兼容**。不是把无关 SDK 工具依赖补进模型服务。probe 只保留固定 ACL 命名空间，实际启动/安装接口守卫通过。

最终 dist-05：

| 制品 | SHA256 |
|---|---|
| wheel | cbe7bd8649edda4d1be723dfd7b0fd7e22e590043f098c1c3478bcda3fabe2ba |
| sdist | 1ca702888d688a2404d2780bb734fc3e6ee78f5d117aee49389dd6847272650b |
| 最终 report.json | aa1b318ae279b6eda632f85a2ea0994ca0325bd53f41bcca3c7f67ec825dee63 |
| 最终 terminal-report.json | 1547dce69d3391fc42efdd98f77de36f31d962584c506ccad3379f8b4c866adf |

## 运行过程（失败不覆盖）

服务器 artifacts 下各目录独立，包含原报告、日志与有限账本；未把 raw logs/请求/凭据提交 Git。

| 目录（20261014 后缀） | 卡/代码 | 结果与含义 |
|---|---|---|
| cedb477-new-host-native | 0 / 42d7dbb | 原生请求/APC/KV live+replay 基线通过 |
| cedb477-live-terminal-01 | 0 / b9518fa | observer 初始化和原生子集通过；多采样请求 HTTP400，首版诊断不完整，整体失败 |
| cedb477-live-terminal-02 | 0 / runtime 4a4e972、工具 90baf7f | 明确得到 n must be 1 when using greedy sampling, got 2；实验输入 n=2/temperature=0 无效，不是环境或 hook 不兼容 |
| cedb477-live-terminal-03 | 0 / runtime 4a4e972、工具 52eb1de | 改为有效非贪心多采样，4 family/8 child、同账本、重放和恢复包装通过；尚未断言核心 abort 原因 |
| cedb477-live-terminal-04-reviewed | 1 / 716ef3b | 记录真实核心状态，stop string/断连均 FINISHED_ABORTED；全部通过 |
| cedb477-live-terminal-05-final | 0 / 92cc06c | 流式首次提交关联优化后的最终全套复验通过 |

最终 core PID=76401，frontend PID=74980，与实验请求/账本进程独立。core epoch=74324d8f832f4e6b9a6193ec8710d33b，observer epoch=b0f73634987d4a768dba6bfe5a99ecd2；这是本次受控启动身份，不是跨重启持久服务 UUID。

| 实际 HTTP case | 预期/绑定/结束 child | 核心逻辑状态 |
|---|---|---|
| completion 两个 prompt × n=2 | 4/4/4 | LENGTH_CAPPED ×4 |
| chat n=2 | 2/2/2 | LENGTH_CAPPED ×1、STOPPED ×1 |
| stop string | 1/1/1 | ABORTED ×1，业务侧完成 |
| stream 首个非空输出后主动断连 | 1/1/1 | ABORTED ×1，结合取消意图对账 |

每个 case 在 HTTP 结束/断连后、消费核心证据前仍保留 1 笔占用；完整 family 正证据与业务结果输入原 DurableLedger 后才到 0。4 条终态、13 次 receipt 操作；未确认载荷重复获取/投递幂等，账本关闭重开后 observer 仍存活，保留正快照重投幂等。不把这些测试说成真实网络丢包或 observer 整进程恢复。

控制接口无凭据拒绝；无未决 family 后恢复自有前端/core 包装，原生请求仍可用且不再捕获新 family。停服父进程 exit0、未强制杀进程组；结束后两张卡 npu-smi 均无运行进程。

原生子集每次通过：449 completion / 478 chat prompt tokens，重复 cached_tokens 0→384；2 live/2 replay batches、6 stored blocks，规范化/空闲 replay 一致。只是后端实际缓存和事件采集证据，**未接在线 M2、未验证 Router 选点或吞吐收益**。

## 回归与审查

92cc06c：Windows CPU **534 pass / 7 skip / 3 deselect**；Linux aarch64 CPU **538 pass / 3 skip / 3 deselect**；integration 两端各 **3 pass**；ruff/format 和两端全新 wheel 安装/默认关闭入口、包资源检查通过。Windows 多出的 4 skip 是平台符号链接限制；3 个固定源码执行项普通 CPU 明确 skip，由专用源码门槛另跑，不等于 7 项均未验证。

最终 Windows 显式固定源码门槛 **3 pass**，新版终态和入口账本分别 **26/26 checks**；固定原方法/hash 未变。常规 Python 调错解释器及显式源码目录指错曾失败，修正命令后复验；未修改断言或降低守卫。GitHub CI 另按实际提交核验，不继承旧 PR 成绩。

自审覆盖模块移动单一实现、foreign patch 所有权、版本拒绝、完整批次校验、ACK/恢复、部分提交保债、预算溢出、轮询取消、流式跨 task 关闭。修正先 fence 再等待关闭、lazy generator 关联与 ContextVar 跨 yield 问题，补首次提交后无需逐输出块设置上下文测试；无采样装置进入热路径。失败请求改非贪心并保存有界错误/部分报告，stop string 与取消补真实核心原因断言。源码 API 仍属实验，另一成员独立人工审查待完成。

## 未通过与不能推导的结论

- 没有原生 Router→真实 HUST→同 owner 的完整组合；没有在线可信 KV 索引、热接入或新版共同水位恢复。
- finite retention：64 family / 256 child，核心 256 唯一 child 包括非受管流量；ACK 不回收 tombstone。观察满额保债并停止增强模式，不适合无界长期生产。
- 未做 observer/前端整体崩溃、自动重绑、长期 GC、图模式、多前端/TP>1 或 MOD Manager 启停。
- 主动停服后原生 HUST output_handler 仍报告 EngineDeadError，含无 hook 基线；记录 exit0/无残留不等于证明所有 shutdown 时序正常。保留原日志和 G3 未决项，不吞异常“修复”。
- 无可报告的观测开销/性能容量提升，runtime profile 基础 terminal 能力仍 false，MOD import_only；仅独立实机子集报告 terminal_observer_verified=true。

原始证据 archive（排除 cache/socket）双端 SHA256 一致：**ab032cbed01629da8dcb562bc7c542e552935815dc0850b51c114bc58a8a9913**。服务器 `artifacts/cedb477-live-terminal-20261014-evidence.tar.gz`；本机 `artifacts/live-terminal-20261014/cedb477-live-terminal-20261014-evidence.tar.gz`。归档含上述六次实机目录及 Linux 最终 CPU/integration 报告；固定源码单独成绩在本机 checks-reviewed-final。GitHub 交付/CI 以收尾记录为准。
