# 固定 HUST 终态运输到新驱动的 CPU 接线

后续：本文保留 CPU 阶段当时的范围；其中完整宿主缓存/NPU 待办已由[新服务器实机记录](hust-terminal-driver-npu-2026-10-22.md)补齐，不再表示当前仍未验证。常驻服务和性能依然待验收。

日期：2026-10-22。输入提交 `7110d16`；本轮增量见对应 Git 提交。固定 HUST Core ced6857 / Ascend b47795c、Router 91b65ee 不变。这里记录协议和 CPU 接线，不提供新 NPU、常驻服务或性能成绩。

## 实现和边界

唯一运输源码为 `experiments/router-extension/hust_terminal_transport.rs`，同一 checks crate 与固定宿主 staging 直接包含；复用原 reqwest 0.13.5，不增加宿主依赖。checks 为真实 HTTP 回归新增同版本 reqwest（关闭默认 TLS），Cargo 自动更新其测试锁；宿主 Cargo/锁补丁未改。

连接来源仍由已有 HustSourceControl 在启用前核验；适配器绑定完整 cursor、Frontend/Core PID 和 ownership。只接受回环 HTTP canonical origin；禁止代理与重定向，不打印 token。poll/ACK 响应≤1 MiB，retire≤4 KiB，单批/来源 family≤64、总 child 预留≤256；完整响应体有 RPC 期限，反序列化前按累计字节限额。每批验证 PID、binding、mode、连续序号和全部 family 的资源/状态，记录中的 family 还必须完整封口。HTTP/取消本身不清债。

ACK 请求复用固定处理器的 `after=through&acknowledge=through`。处理器先 ACK 请求水位，再返回该水位后的记录；这些更新记录可以非空，适配器完整校验后由后续 poll 消费。没有把 reply.boundary 当成 ACK 水位，也没有为了确认一次 ACK 而提前确认新记录。retire 只允许显式 exclusive_router_v1，校验相同 binding 与精确 retired_through；observe_only 不回收。

真实有限工具 `tools.hust_runtime_chain --use-terminal-driver` 只接受独立 managed durable 模式；与旧 reconcile 互斥，暂不组合原强退/控制/大量 churn/代理故障资格，防止继承旧路径成绩。该路径调用新 Driver.step，仍不是后台 run/常驻服务。原模式和默认关闭保留；信号、readiness、Manager 均未完成。

## 已执行测试

本地 Windows 项目 Python venv；Linux x86_64 使用既有项目私有 Rust 1.99.0，在 WSL Ubuntu 24.04 运行。未安装全局 Rust/Python/驱动，没有连接受限 SSH alias。

- Rust helper 86 项通过（原 80 项 + 新 6 项 socket 回归）；覆盖身份/PID/mode、序号、部分/重复 family、总 child 预算、重复 attempt、溢出、未知字段、payload 长度/累计超限、响应体超时、409/503、禁止重定向和受管水位。
- 同一 crate `clippy --all-targets -- -D warnings` 通过；CPU harness example 编译通过。
- `tools.probe_hust_terminal_driver` 八场景通过：正常、ACK 内并发新终态、ACK 成功回复丢失、retire 成功回复丢失、503、observe_only、PID 变化、未知 attempt。Rust Driver + 原 DurableRuntime + 原 SQLite 行布局通过真实 socket 调用现有 FrontendEnds.read/retire；CoreEnds utility 为明确替身，没有真实 HUST 调度或 NPU。源端每次 ACK/retire 前独立读 SQLite，确认请求水位不超过持久 cursor。
- 两种回复丢失均在源端操作成功后关闭连接，Driver 返回 retry，active=0 时仍继续确认，重试未新增账本 generation 或重复退役。未知 attempt 整批拒绝，active 保留且 cursor=0；提交后 ACK 的 PID 变化失败关闭，已持久 cursor=1 保留。
- Python 模式守卫/已有有限工具测试 13 项通过，拒绝非法组合发生在后端检查/启动之前。
- Windows 统一 CPU 596 通过、7 跳过、3 deselected；integration 3 通过；Ruff/格式、隔离 sdist/wheel 构建及干净安装通过。MOD 仍为 import_only，未改后端 hook 或引擎版本。

完整宿主构建前置检查：在 artifacts 下新私有宿主完成 staging，原 Cargo/锁输入 hash 通过，脚本未改变宿主依赖补丁。本地 WSL 缺 SDK，已将 Ubuntu OpenSSL 3.0.13 构建包按 apt 元数据 SHA256 核验并仅解压到项目私有目录，没有安装或升级系统库；过期 apt 索引候选返回 404，保留失败记录。固定 vllm-chat 提交也已核对并补进私有缓存，但离线构建随后因本机缺少 anyhow 等原宿主 registry 缓存停止。故本轮尚无完整宿主编译/39 项回归通过结论；上一轮 39 项不能继承。该项应在具备既有构建缓存的新服务器或独立 Linux 构建环境完成，不是更换 HUST 运行版本的依据。

复现（使用既有私有 Rust 环境或 CI 的固定 toolchain）：

```bash
cargo +1.99.0 fmt --manifest-path experiments/router-extension/checks/Cargo.toml --check
cargo +1.99.0 clippy --locked --manifest-path experiments/router-extension/checks/Cargo.toml --all-targets -- -D warnings
cargo +1.99.0 test --locked --manifest-path experiments/router-extension/checks/Cargo.toml
cargo +1.99.0 build --locked --manifest-path experiments/router-extension/checks/Cargo.toml --example hust_terminal_driver_probe
PYTHONPATH=src python -m tools.probe_hust_terminal_driver --binary experiments/router-extension/checks/target/debug/examples/hust_terminal_driver_probe
```

CI helper 工作流增加上述跨进程协议联调并补触发路径。实现提交 `4b867d9df6ecc34d9f4eb9dc9c55b752655450ea` 的六项检查已独立成功：validate / hust-source-methods 的 Python 3.11、3.12 四项，加 validate-helper 和 validate-index。新增联调在 [helper 工作流](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/39781226208)成功执行。该 CI 不覆盖完整宿主或 NPU；自审和自动化不代表独立人工审查完成，不自动合并。

自审修正：有限新路径按持久 cursor 推进量记一次统计，不因“已提交但确认回复丢失→Retry”漏计；该统计仅在所有 pending 确认完成后作为有限资格结果。较大的响应限额测试使用独立宽期限，短期限只用于明确延迟响应体的用例，减少 CPU 争用造成的偶发失败。默认工具不向旧二进制配置增加 false 字段；显式新模式必须回报独立 marker，旧程序不能冒充新路径。

最终源码 SHA256：运输 `3ffa4611be550d2d3abdda090d723bc32e8fee6d79ada4f27dff878c00a7fb7c`；驱动 `a58af19fc7780a68476400c27490ebbd5e7bd636e85fb994b1241f69fd9614b2`；有限宿主 probe `73270d845320827496c831e204b691e77c07406486cfa5e38cd2b6bada1f6ce0`。主机 SDK/编译缓存、私有配置和原始制品均位于忽略区，不入 Git。

## 未完成与用卡

上一分配 32054 的严格指纹 SSH 连接已关闭，本轮没有连接成功或运行推理。完整宿主构建不需要 NPU；单副本实机资格需要 1 张 NPU；沿用既有公开链需要 2 张 TP1 NPU、公共 Qwen2.5-14B-Instruct。收到新分配及指纹后，应先完成完整宿主构建和回归、重建最终二进制，再执行：

```bash
python -m tools.hust_runtime_chain --execute --model /data/shared_models/Qwen2.5-14B-Instruct \
  --binary <本次最终构建的hust_runtime_probe> --output <新的项目artifacts目录> \
  --pycache-root <已验证的项目私有缓存> --durable-store \
  --managed-terminal-retirement --use-terminal-driver
```

检查新报告 terminal_driver_transport_verified=true、真实取消状态、目标/命中和 active/回收；报告仍必须标记 production_lifecycle_verified=false 和 performance_verified=false。随后才接 run/readiness/信号排空和 Manager。旧二进制、旧两卡结果、CPU Core 替身都不能代替此项。
