# 单后端真实终态闭环与下一阶段

更新：2026-10-14。以本页和[本轮证据](../../evidence/cedb477-live-terminal-2026-10-14.md)为当前状态；此前 CPU 方案保留为历史。生产 ABI、MOD 激活和性能资格未冻结。

## 本轮实际接通什么

```text
实验请求端：原 M3 预记账 → 实际 HTTP 请求（自有 attempt header）
HUST 前端：render 完整 family → 捕获实际 external/internal child ID
HUST 核心：原清理成功返回 → 有界逻辑终态记录
HUST 原 utility 通道 → 原 API 服务的可鉴权控制入口
实验消费者：通用 Ended + 独立业务结果 → 同一 DurableLedger → 提交后确认
```

这不是“原生 Router 已接真实后端”：本轮由有限实验请求端代替原生 Router 发请求。没有另造代理、索引或第二套账本；EntryCapture/TerminalJournal 移入包内单一实现，旧 tools 仅兼容导出。既有 Rust Router 的动态准入/生命周期接缝和本次真实后端观察仍须组合验证。

实际正常、多 prompt × n、chat、停止字符串、流式断连已运行。HTTP 返回或断连本身不释放负债；完整 family 封口且全部 child 获得核心正终态后，账本 owner 才结合业务结果结清。核心 FINISHED_ABORTED 不等于业务取消：停止字符串在业务侧是完成；主动断连的取消意图另行提供。逻辑结束不证明 KV/HBM 物理释放，也不能作为驻留目录证据。

## 安装面与维护成本

- 固定 HUST Core `ced6857afa0ea7b2e3f0846a62e1394e90f15607` / Ascend `b47795c63a8e63e7aed4d311f951428d94e045ab`。不是依据上游 vLLM 文档替代 HUST 源码。
- 复用现有 general plugin、endpoint plugin 和 EngineCore utility；不改 HUST 引擎源码、IPC 消息类型或设备执行。
- 8 处推理方法包装：前端实例 3、serving 类 4、实际 scheduler 实例清理 1；另注册 1 个 utility 方法及既有 API 上 2 个控制路由。没有新监听进程。只恢复本组件仍持有的包装，其他插件改写后拒绝覆盖。
- 原 runtime profile 加 13 个接口文件 SHA 守卫；启动后另核实际 callable 来源，避免 Ascend 加载后替换造成误判。版本升级既要改映射，也要重新审查行为/回归，不能承诺只改版本字符串。
- 请求路径不插入设备同步、npu-smi、profiler、远程确认或文件写入；流式关联上下文只覆盖首次提交与关闭，不在每个输出块重复设置。仍有 Python 包装和有界内存成本，尚未测得其性能可忽略。

默认关闭。实验要求显式 `PREFIX_ROUTING_HUST_TERMINAL=1`、`VLLM_PLUGINS` 包含 `prefix_routing_terminal`，并保留原 Ascend 插件 allowlist。只验证 loopback、单前端 AsyncMPClient、TP1/DP1、普通文本采样和 eager；不外推图模式、beam、多模态、流式输入或多前端。

控制读取另需本次随机私密凭据；不写入报告、命令参数或 Git。请求管理前缀不是鉴权：未来 Router 必须覆盖客户端同名 header，真实部署的网络隔离与访问控制尚需产品化。当前不要把实验 API 直接暴露给外网。

## 有界性、降级和关闭

核心最多保留 256 个唯一 child 身份（包含非受管流量），前端最多 64 个 family、256 个 child、64 个终态事件。ACK 可回收重放队列，但不回收本轮 tombstone。满额/序号缺口/身份变化/断连或轮询取消使增强观察失效，保留原负债；不是稳定无限运行方案。包装恢复后原生服务仍可用，重新启用需新的受控启动。

保留 observer 的重复读取和账本关闭重开已验证；未覆盖 observer/前端整体重启、任意进程崩溃、自动服务重绑或长期 GC。控制协议是实验协议，不冻结为生产 ABI。基础 runtime profile 的 terminal 能力仍为 false，独立报告中的 `terminal_observer_verified=true` 只表示上述可选子集通过。

关闭入口要求调用方确已停止受管派发且没有未决 family。`dispatcher_stopped=true` 是调用方前置条件，不是自动停止 Router 的证明。关闭先 fence 受管入口，再请求核心恢复自有包装，最后恢复前端；失败不重新开放增强准入。utility 方法和控制路由留到进程退出，这不是 Manager 完整卸载。MOD 继续 `import_only`。

## 复现实机检查

在项目根目录、已核验的隔离服务解释器下运行；output 必须全新，先确认该卡未被他人使用：

```bash
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV \
  artifacts/hust-cedb477-rebuild-20261013/service-venv/bin/python \
  -m tools.hust_native_probe --execute --terminal-observer \
  --model /data/shared_models/Qwen2.5-14B-Instruct \
  --device 0 --port 18161 --output artifacts/terminal-new-run
```

工具显式保留必要的 ACL 命名空间，清除继承的 Python/旧 hook 开关，自动生成本次控制凭据并仅清理其自有进程组。`--pycache-root <已核验的项目私有路径>` 只用于显式复用 bytecode，不作为冷启动/性能对比。依赖检查也须清除 SDK PYTHONPATH；默认 SDK 元数据不能混入隔离服务检查。不要改全局 Python、CANN 或公共模型。

## 接下来按什么顺序推进

1. 先把现有成熟 Router 的自有 attempt 载体、真实健康/身份、动态预记账、传输观察和本次终态消费接到同一个 owner。验证取消与终态竞态、观察故障后的拒绝派发、提交后丢 ACK、Router 重启保债；不新增一套网关或持久化服务。先 CPU 合同，再单 NPU 实际联调。
2. 接真实 M7 KV 订阅到原索引：先受控空起点与连续覆盖；消息限额、身份、序号、lease、断连/缺口立即撤信。现有原生采集/规范化不是在线 M2。已热接入/超窗恢复另需新版共同边界协议，旧 publisher hook 不直接启用；若扩大修改先讨论。
3. 受控完整链路成立即用 2×TP1 做直连、原 Router 基础策略、仅代理、加观察/目录的配对开销及强基线。热点/低复用/混合长度都要测，按实际瓶颈修改计划；不等长期交付全部完成才第一次测性能。M8 暂后置。
4. 交付前补长期留存/GC、持久化方案、图与并发、完整启停/恢复和 MOD 生产包装，再冻结接口及三人任务。SQLite FULL 事务仍是语义工具，不未经测量放入性能热路径。

当前 2 张卡足够：单后端阶段用 1 张，双副本阶段用 2 张；不申请额外卡，不运行保活假负载。32068 已完成两张卡顺序单实例复验，不代表同时双副本通过。

主动正常停服后的 HUST EngineDeadError 日志仍保留；父进程正常退出且无 NPU 残留不等于完整排空/恢复验收。G3 和性能门槛继续开放。独立人工审查未完成，不自动合并。
