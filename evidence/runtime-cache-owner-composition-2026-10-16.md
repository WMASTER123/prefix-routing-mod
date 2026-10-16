# 原M2索引与同进程M3 owner组合证据

日期：2026-10-16。结论：CPU候选组合通过，不是NPU性能或真实HUST全链资格。

## 验证对象与边界

- 外部宿主仍为固定 `vllm-project/router@91b65ee` 的隔离副本；没有修改vLLM-HUST/vLLM-Ascend-HUST引擎源码。
- `runtime_owner.rs`持有M3活动attempt、配置/实例围栏、准入、可靠终态一次释放、有限墓碑与显式checkpoint。
- `runtime_cache_port.rs`把同一进程的原`KvBlockIndexer`、`Directory`和`Pool`映射成M2只读端口；没有复制第二棵KV树，也没有逐请求IPC、磁盘或设备调用。
- `runtime_cache_owner_probe.rs`通过固定Router typed公开completion入口和两个loopback后端验证组合路径。M1特征与归一化KV事件仍为有界fixture；真实HUST订阅、Qwen分词和后端终态未在本探针同时接入。

## 审查中修正的问题

1. 原`Pool`只有断连，没有同URL新实例替换/精确注销；这会使重启worker无法安全重新注册。新增替换要求旧Binding完全匹配，新分区从untrusted开始；旧身份不能驱逐新实例。
2. 每个分区记录逻辑配额，精确注销和替换会回收/重新计算全池reserved；该数值仍是逻辑上限，不冒充allocator/RSS硬上限。
3. 首次宿主编译暴露`&String`被推断为`Arc<&String>`，已改成显式`Arc<str>`转换；随后release构建通过。
4. 探针模块只使用生产API子集而产生的dead-code warning局部抑制；上游`worker.rs`的`fetch_update`弃用warning保留且未在本项目中修改。

## 结果

- 原索引/Directory/Pool及同源适配测试：66 passed，0 failed；新增覆盖精确实例替换、旧身份拒绝、恢复前unknown、租约过期、迟到控制响应及注销配额回收。
- RuntimeOwner隔离检查：37 passed，0 failed（同一候选改动集）。
- 固定Router aarch64 release构建成功。
- 组合探针：pass。缓存命中请求只到worker B；HTTP关闭后B保留一笔未决负债，随后无可信正命中的请求只到worker A；两份可靠终态把active从2降到0。
- 目录语义：A为可信miss `Some(0)`，B为一个完整块命中 `Some(1)`；B断连后为unknown；同URL新instance恢复后旧epoch仍unknown、新epoch为可信miss；注销后全池reserved从32768降到16384字节。
- 进程清理：父进程返回0，无强制进程组清理。

服务器证据目录：`/root/work/prefix-routing-mod/artifacts/runtime-cache-owner-current-20261016b`。

| 制品 | SHA256 |
|---|---|
| `runtime_cache_owner_probe` | `7aa2bced6eced360d5bd79503f7d886b6228141120fd1ad4fb9f00e62f9d0e0a` |
| `report.json` | `1b7c5eb0c6a8940e21868511ae74eecb234a55c25eb78b1b2f49fd53518bbb4f` |
| `probe.log` | `4e63ce553bdcd7a6cc96423bd44dfb703bb1408f9d5b5e1f7aa25189fcd2f242` |

## 未通过/不得外推

- 未使用NPU 3/4；二者在探针结束时健康、无本项目设备进程。本结果不能说明Qwen14B吞吐、TTFT、生成停顿或容量提升。
- 尚未把真实M7事件subscriber接入`InProcessCache`，未验证运行时连续事件/缺口/恢复与公开HTTP并发发生时的锁开销。
- 尚未把真实Qwen M1与HUST可靠终态同时装入；completion/chat支持范围和默认关闭启动方式未冻结。
- checkpoint只有内存数据契约，持久介质、提交边界和崩溃启动恢复尚未确定。
- 当前逻辑额度不是Host RSS硬边界；长期工作集、锁竞争和重建峰值仍需实测。

因此下一步是把已有真实HUST subscriber映射到该适配层，先做正确性与Host开销，再连接真实M1/终态。完成这些故障测试之前不正式拆分核心组件开发，也不开始策略优劣结论。

提交同步后原计划立即在当前服务器重建索引并复跑双TP1原生M7资格；执行前SSH端口已返回`Connection refused`，短暂连通复核仍失败，故没有启动模型或产生新的NPU记录。该资源事件不改变上述CPU结论，也不能算实机验证失败；下次新分配必须重新核验环境与两张可见卡后再运行。
