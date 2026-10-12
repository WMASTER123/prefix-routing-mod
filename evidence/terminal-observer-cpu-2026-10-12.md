# 终态来源与有限在线对账 CPU 证据（2026-10-12）

代码提交：`801b628`，基于 `ecacc39`。分支 `codex/backend-terminal-audit`。本记录只覆盖注明代码和实验，不自动继承到后续修改。自审完成；第二成员独立审查待完成，无自动合并。

## 运行范围与结果

| 检查 | 结果 | 限制 |
|---|---|---|
| Windows CPU / integration | 317 pass + 4 平台 skips / 3 pass | 非 NPU |
| Linux aarch64 CPU / integration | 321 pass / 3 pass | 当前 CPU-only 分配 |
| 四文件固定源码方法审计 | 两端 25 项全部 true | 合成协作者，不启动完整引擎 |
| 新增工具与在线原型测试 | 11 + 24 项，包含在 CPU 总数中 | 不重复累计 |
| 两端新 sdist→wheel / 干净安装 | pass，MOD import_only、关闭入口无引擎导入 | 不是 Manager 激活 |
| HUST 源码保护 | 固定 commit 与四文件 SHA256 校验；远端工作树仍干净 | 未安装终态 observer |
| 原生 Rust Router / NPU | 本轮未重新运行 | 未改 Rust 或生产 Python 包，不继承为本轮硬件结论 |

本地默认 `python -m build` 首次因 PyPI TLS EOF 获取 hatchling 失败；未关 TLS、未改依赖或全局环境。改用 `uv build --offline` 从已有缓存完成新 sdist→wheel，干净安装通过。远端隔离构建获取 hatchling 1.32.4 成功。这是构建依赖下载失败，不是 HUST 运行环境冲突。

## 可复现入口

两端分别运行：

```text
python tools/check.py --suite cpu --output artifacts/terminal-audit-20261012/checks
python tools/check.py --suite integration --output artifacts/terminal-audit-20261012/checks
python -m tools.audit_backend_terminal --source-directory artifacts/terminal-audit-20261012/source --output <新的报告路径>
python tools/verify_wheel.py --wheel artifacts/terminal-audit-20261012/dist/prefix_routing_mod-0.1.0.dev0-py3-none-any.whl
```

源码目录需要放入固定 HUST 提交 `13f11da44455d2f85f00f810a04e1bd2eaf30ba6` 的 scheduler.py、core.py、output_processor.py、core_client.py；原仓库路径及完整 SHA256 见工具常量与审计页。报告已存在时拒绝覆盖。没有把原始 HUST 源码、模型、用户请求或原始私有日志加入 Git。

报告保存在两端项目 `artifacts/terminal-audit-20261012/`。Windows report-01/02 是逐步增量的 18/22 项方法检查，最终 report-03 为 25 项；Linux report-01 为最终 25 项。不要只按同名文件比较版本。

## 关键观察与反例

- 普通 abort 输入发送返回时核心请求仍存在；核心 abort 执行后不直接回传结束列表。管理性暂停的发送 helper 是另一条路径。
- stop string 前端先完成并删除关联，返回需要核心 abort 的 ID。随后相同 ID 输出被忽略。真实适配必须提前保留 family。
- 核心逻辑结束与 blocks 释放可以分离；假设的 fence 未推进时不可释放，推进后释放。没有使用或验证 NPU synchronize。
- 源码 cleanup→journal→在线回调只在全部合成 child 完成后触发；cleanup 抛异常则不发成功终态。
- journal→旧参考账本在运行中结算；测试明确使用已取消 family，不把通用 ended 推断为成功或取消原因。丢 ACK、重复、断连、缺口、外来代次/不完整快照保持幂等或拒绝。
- consumer 重建复用现有账本，不是 Router 进程崩溃后持久化恢复。observer 新代次拒绝旧消费者，旧负债保留。
- 有界事件溢出关闭观测，不能抛入模拟调度器或默默丢历史；留存满拒绝新登记。tombstones 不 GC，有限实验不是可持续生产实现。

## 初步 CPU 资源诊断

2000 个单 child family，5 次计时，接收端为 no-op：Windows 每 family 中位约 6.26 μs（6.07–6.44），aarch64 约 18.44 μs（18.27–19.58）。两端 tracemalloc 在另一次执行结束留存约 1,649,650 bytes，峰值约 1,682,762 bytes。带 tracemalloc 的完整快照约 0.518/0.806 ms，不能与未采样延迟混算。

只衡量当前 Python 原型的登记、终态集合更新、空回调和 ACK；**不含真实账本、IPC、Host 与推理竞争、模型/NPU，也不代表相对引擎增加的延迟或性能门槛通过**。数值是一次 CPU 诊断，不是生产默认参数。留存规模增长意味着必须补 GC/恢复协议。

复现脚本（在仓库根目录使用项目 Python 执行；只输出 JSON）：

```python
import json, statistics, time, tracemalloc
from tools.terminal_observer_prototype import Binding, TerminalJournal, OnlineConsumer
def trial(count):
    b=Binding("worker","instance","observer")
    j=TerminalJournal(b,max_families=count,max_children=count,max_events=1)
    c=OnlineConsumer(b,lambda record: None)
    c.recover(j)
    start=time.perf_counter_ns()
    for i in range(count):
        j.register("a"+str(i),("c"+str(i),))
        assert j.observe_core_end("c"+str(i))
        c.pump(j)
    elapsed=time.perf_counter_ns()-start
    snap_start=time.perf_counter_ns()
    cut, records=j.snapshot(b)
    snap_ns=time.perf_counter_ns()-snap_start
    assert cut==count and len(records)==count and not j.events
    return elapsed/count, snap_ns, j
samples=[trial(2000)[0] for _ in range(5)]
tracemalloc.start()
_,snapshot_ns,retained=trial(2000)
current,peak=tracemalloc.get_traced_memory()
tracemalloc.stop()
print(json.dumps({"scope":"synthetic register+observe+no-op sink+ack; no engine/network/ledger","families":2000,"samples":5,"ns_per_family_median":statistics.median(samples),"ns_per_family_min":min(samples),"ns_per_family_max":max(samples),"tracemalloc_retained_bytes":current,"tracemalloc_peak_bytes":peak,"snapshot_ns_with_tracemalloc":snapshot_ns,"production_overhead_validated":False}))
```

## 审查与下一步

已自审 guard-before-exec、原方法成功后观测、错误证据不释放、family 全量确认、快照完整校验、ACK 在应用后、预算满拒绝，以及文档不夸大恢复范围。用户同意 CPU 原型，不代表批准生产安装。

[范围与维护成本](../docs/planning/terminal-observer-scope.md)和[ADR](../docs/decisions/2026-10-12-terminal-observer-cpu.md)是后续依据。真实 family 封口、M5 统一序号、跨进程投递、持久化/GC 未完成；M7→M2 wire、新鲜度和多分区预算尚未开始新实现。继续 CPU 接线；需要真实后端时再申请 1 张 NPU，之后双 TP=1 用 2 张。
