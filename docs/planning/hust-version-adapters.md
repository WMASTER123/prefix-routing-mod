# 版本适配与 cedb477 迁移

日期：2026-10-13。本页是当前迁移入口；历史旧 HUST 成绩保留原范围。决策见 [ADR](../decisions/2026-10-13-hust-version-adapters.md)。

## 架构和当前代码

版本差异集中在 M7；M5 的 Router 宿主接缝仍独立。`src/prefix_routing_mod/adapters/profiles.json` 是包身份、源码接口指纹、事件枚举和协议能力的唯一映射输入，`profiles.py` 显式检查安装环境，`kv.py` 生成现有索引接受的规范化操作。导入不导入 torch/vllm、不探测设备或联网。旧 publisher 常量从映射读取，旧 tools normalizer 委托共用实现；没有另建缓存树或 HTTP 服务。

`backend-doctor --profile ...` 只证明所列安装版本和接口文件一致；wheel 来源、配置、服务身份、请求终态和 NPU 实验是独立证据。MOD 仍 import_only，描述中的宽泛发现区间不是运行兼容保证。

| Profile | 能力边界 |
|---|---|
| hust-legacy-publisher-v1 | 原归档组合；已存在可选共同水位 publisher 实验，默认关闭；旧原生快照缺 token/共同边界保证 |
| hust-cedb477-native-v1 | 新固定 HUST 组合；原生三帧 PUB / 四帧 DEALER replay，END_SEQ 不等于权威 snapshot；新版 publisher hook 与可靠终态适配尚未交付 |

规范化只转换完整、受限的 token/hash/父链消息，校验 rank0、group0、设备层、128-token full-attention 和 32-byte hash。locality 为 REMOTE、非空 ownership、额外 hash 身份等拒绝。session_id 仅作事件上下文，不转为 KV 所有者。缺身份、序号缺口、未知进程实例或超窗不能因消息可解码而恢复信任。

真实事件可转成索引操作，不代表 M7→在线 M2 已接通。三帧 native 协议的恢复只能在受控已知空起点和完整连续覆盖前提下讨论；不能给任意已热服务伪造起点或 publisher epoch。已热 Router 接入与超窗恢复需要下一阶段验收新版可选 publisher：从引擎启动时保留有界镜像、token 拓扑及共同水位。若不能小范围实现，再讨论降级路径，不能通过重启任意用户服务自动掩盖能力缺口。

## 环境迁移

目标固定 Core ced6857 / Ascend b47795c。实际包名为 vllm / vllm-ascend，版本及接口字节由 profile 检查；禁止同时装 vllm-ascend-hust。来源是 HUST 固定源码，不依据包名改写为官方制品认证。

`runtime/candidates/hust-cedb477.json` 记录 Python/uv、两份未改写的 hash 锁及五个必须准备的 owner-local wheel。184 个依赖，加 FLA/expecttest 两个补充依赖和两引擎为 188 包；安装本 MOD 后额外增加一个包。wheel 未发布公共下载，本入口要求预先取得并核验制品，不声称从任意新机器在线重建已通过。

```bash
cd /root/work/prefix-routing-mod
<development-python> -m tools.restore_wheel_runtime --execute \
  --wheelhouse <project-artifacts-wheelhouse> --uv <uv-0.12.23> \
  --python <python-3.12.13> --output <fresh-project-artifacts-run> \
  --cache-dir <project-private-uv-cache> --offline

<service-python> -m prefix_routing_mod.cli backend-doctor \
  --profile hust-cedb477-native-v1
```

首次准备可以从用户指定的另一项目只读复制固定制品和缓存，核对 wheel/锁 hash；不修改其环境或复用其 Router/账本。恢复脚本拒绝已有输出目录，在新 venv 顺序安装 runtime lock、extra lock、重装 Ascend Triton、安装两引擎，再检查依赖及 Ascend RECORD 覆盖。`--offline` 仍需要索引元数据缓存及全部特殊 wheel；不能把离线缺制品误判成版本不可用。清空 caches、升级社区 Triton 或改包版本后重新验收。

## 实机工具与后续任务

新 `tools/hust_native_probe.py` 复用已有 OwnedBackend、HTTP、IPC 与清理工具。只在显式执行时加载 codec/tokenizer，在项目私有输出启动单卡模型、验证 completion/chat 分词数、重复请求的后端缓存计数、真实 store 的规范化和空闲时 live/replay 对齐。数据规模及等待有界，失败保存报告；不会把 HTTP 完成认定为核心逻辑 ENDED。SDK 仅向模型进程提供固定 SHA 的 ACL namespace；动态库/驱动继续使用平台已有配置，环境变化另验。

```bash
<service-python> -m tools.hust_native_probe --execute \
  --model /data/shared_models/Qwen2.5-14B-Instruct \
  --device 0 --port 18146 --output <fresh-project-artifacts-run>
```

首验 FP16/eager、TP1/DP1、Qwen2.5 dense；BF16、图、并发取消、持续运行、另一设备和双实例均不能由这项通过外推。模型和报文不进入 Git；原始日志留 artifacts，Git 仅记录必要摘要、hash 与失败范围。

后续顺序：新版真实入口及完整 child 关联 → 新版可靠终态/账本闭环 → 可选共同水位 publisher 与实际 M7→M2 接线 → 双 TP1 与强基线比较 → 资源/GC/MOD 交付门。旧源码审计工具仍明确只接受旧 hash，不扩守卫冒充新版本通过。生产接口、目录及自动启用暂不冻结。

## 以后如何增加版本

1. 固定 HUST Core/Ascend 来源与配对，检查依赖、制品和实际 worker bootstrap。
2. 公共协议语义不变时复用同一适配器；私有 hook 继续固定源码守卫。改字段/枚举映射后运行相同正反合同，行为/时序变化集中修改对应适配器。
3. 为入口、KV 恢复、终态、指标各自记录能力与证据；不把语法可解析、HTTP 可用、完整恢复、性能收益混为一个支持布尔值。
4. 新旧映射回归、独立 wheel/sdist、关闭和未知版本拒绝通过后，再做受影响的 NPU 子集。宽泛 host 范围不替代上述门。

当前不会自动下载/升级引擎，不增加逐请求远程翻译、设备同步或全局环境修改。策略和索引只消费项目自己的合同。
