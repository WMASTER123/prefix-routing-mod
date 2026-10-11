# Qwen2.5-14B / Ascend 单后端探测记录

日期：2026-10-11。范围：P1b 局部功能验证，不是完整 NPU smoke、Router 或性能验收。用户确认优先验证预装组合，归档 HUST 组合后置。失败记录保留，不据后续修正覆盖历史结果。

## 环境与输入

- 新机器 aarch64，4×Ascend910B2，每卡 65536 MiB HBM；物理 1–4 对应逻辑 0–3。本轮只用逻辑 0。
- cgroup CPU 配额 16 核时间，cpuset 0–191 不等于独占 192 核；RAM 128 GiB，/dev/shm 32 GiB。根卷约 256 GiB 可用。未获得独占性能测量证明。
- Python 3.12.13，npu-smi/driver 26.0.rc1，CANN 9.1.0，torch 2.10.0+cpu，torch-npu 2.10.0.post4。
- vLLM 0.23.0+empty，源码 `0fc695fc6d1d82e9a5ac6835ac8e4e1c83703665`；Ascend 0.23.0，源码 `5cb98caaadeff42b5b62b996e34bb2aaa29d20fd`。两源码工作树干净；未修改或安装全局环境。
- 公共只读 `/data/shared_models/Qwen2.5-14B-Instruct`，8 个权重分片。配置：48 层、hidden 5120、40 Q heads / 8 KV heads、BF16、max positions 32768；本次限制服务 context 为 8192。
- 仅对元数据做身份记录，未校验完整权重 revision。config SHA256 `0f2085dbbe2ee251bd6a6a0797d84a6ce34436044d629aa3cba793b43d311a9e`；tokenizer config `5b5d4f65d0acd3b2d56a35b56d374a36cbc1c8fa5cf3b3febbbfabf22f359583`；tokenizer JSON `c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539`。

运行方式见[运维](../docs/operations.md)。固定 TP=1、BF16、APC、chunked prefill、eager、max sequences 8、batched tokens 2048、memory utilization 0.70。禁用 Ascend CPU binding，避免本机默认路径的 IRQ/irqbalance 操作；插件仅加载 Ascend 所需项。以上参数是功能探测配置，不是调优结果。

## 逐次结果

服务器原始资料位于项目 `artifacts/qwen14b-eager-probe-20261011-01`、`-02`、`-03`；本地副本位于忽略目录 `artifacts/remote-qwen14b-20261011`。每次包含 backend.log、metrics.prom、summary.json。原始日志不入 Git。

| 运行 | 检查与变化 | 结果及限制 |
|---|---|---|
| 01 | 未启用事件；初版探测脚本 | health、两次非流式、一次 SSE 通过；群组同时 SIGTERM 造成关闭错误，不能认定稳定启停通过 |
| 02 | 增加原生 ZMQ 订阅，关闭时先通知 API 父进程 | 生成/APC 正常；未收到事件，局部整体 fail；发现 PUB/SUB 都 connect 的探测工具错误；默认 abort 关闭仍出现 EngineDeadError/信号量告警 |
| 03 | 订阅端改为 loopback bind；原生 shutdown-timeout=10 使用 drain | 生成/APC 正常；仍未收到事件，整体 fail。父进程退出 0、无需探测工具强杀；日志不再出现前两次的 EngineDeadError/信号量告警，仍有后端 host_emptyCache 警告 |

每轮固定三条合成请求：输入 648 tokens，非流式各生成 24 tokens；SSE 返回 24 个内容块并有 finish 与 DONE。未保存输出文本，不构成模型质量、长上下文或生产请求正确性评测。

APC 指标：`prefix_cache_queries_total=1944`、`prefix_cache_hits_total=1280`。仅表明三条固定请求触发了缓存命中；不能作为真实负载命中率、路由收益或吞吐增幅。

日志报告模型权重占用约 27.535 GiB、可用 KV 约 14.82 GiB、KV token slots 80,896、eager 图占用 0。slots 是引擎聚合容量，不等于单请求上下文或可保证并发。设备异步/事件时点未验收。

## 原生事件：未通过，根因尚未完全确定

固定源码 `vllm/distributed/kv_events.py` 对非通配 TCP endpoint 使用 PUB connect，因此局部采样器必须 SUB bind；已修正并增加 CPU 回归测试。`-03` 后端日志确认 publisher 线程创建，但真实请求仍无接收样本；不能把一次接线修复当作功能修复完成。

额外 CPU 诊断使用同版本 EventPublisherFactory、loopback bind/connect 和合成 AllBlocksCleared，收到三帧（0、8、30 字节）。这只验证同机原生发布器的合成传输，不验证真实缓存事件产生。诊断脚本位于忽略 artifacts，不注入真实后端，不修改 scheduler。

下一检查应定位真实请求的事件产生、缓存管理器收集、发布和订阅时序；必要时在受控诊断分支增加观察再讨论，不直接加产品 hook。BlockStored/Removed/Cleared、replay、缺口/代次恢复、异步可复用语义均未通过；精确缓存目录尚不能启用。事件 medium 常量为 GPU 也不代表底层设备是 NVIDIA。

## 校验与放行边界

新增脚本不属于生产 MOD 的 import path。自检覆盖参数边界、SSE 完整性、事件摘要脱敏、关闭信号范围、ZMQ bind 失败清理；人工交叉审查仍待 PR，不声称独立审查已完成。本地 Windows/Python 3.12：CPU 49 项、mock 集成 3 项通过，sdist/wheel 构建及源码目录外干净 wheel 安装验证通过。服务器最终提交和 CI 验证状态见 PR，不将较早版本通过结果覆盖最新变更。

运行结束检查：自有 API/EngineCore PID 已退出，loopback 18140/18141 连接被拒绝；第二轮后 npu-smi 显示四卡均无运行进程。没有常驻服务，也没有为保活执行虚假负载。

初版原始 summary SHA256：01 `0f8afb237d9c13a9631c0c58edfc92f3868572175295144e55d302305979bb01`；02 `d336fde649f6fc0e995c704671e2b3bdad150c51b744c80f5eb60e0a526a63ba`；03 `b467bffe0ffcadc31cfde7390f5e0f70138a63ea9bd15bd365043ecbf0595879`。各轮脚本为开发迭代，不冒称都使用最终同一版本。

结论：公共 14B 可以作为本机单卡基础功能首验模型；预装组合获得有限可行性证据。没有增加 observer，没有更换推理栈；没有路由、graph、取消、持续运行、性能或最终发布通过结论。MOD 继续 import_only，统一 npu-smoke/npu-routing/performance/release 仍返回 not-run。
