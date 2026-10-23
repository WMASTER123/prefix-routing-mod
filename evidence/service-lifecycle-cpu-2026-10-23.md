# 服务生命周期与固定 Manager 的 CPU 资格

日期：2026-10-23；续接后台终态双 NPU 资格，不继承其硬件通过范围。

## 实现与审查

`service_lifecycle.rs` 组合原 DurableRuntime、ControlSet、TerminalTask 与 Axum HTTP 宿主，不创建第二代理/账本。完整来源、同一控制门/runtime、实例集合及单一 service claim 在消费前校验；恢复未授权时拒绝启动。readiness 要求监听、恢复授权、所有终态来源新鲜和至少一个合法控制门。暂用保守的全来源终态就绪门，单来源失联可能关闭整个业务入口，不宣称最优可用性；M3 发送围栏仍保留，M2 unknown 可基础回退。

HTTP 在途覆盖 handler、上传及响应体直到 EOF/error/drop；保留 trailer、size hint 和帧，不产生后端 ENDED。HTTP 有独立上限，readiness 探针不计入。第一次停止固定统一期限；关闭新准入，等待 HTTP、真实负债和终态确认，stop-all/join 后进入 quiesced，监听退出才进入 stopped。超时或异常关闭 owner，保留持久状态；不取消持久提交来伪造成功。

自审补充了重复 service 拒绝、恢复未授权拒绝启动、控制失联/新验证/排空不可逆、pending 确认未完成不得成功退出，以及 poisoned lock 失败路径不重复加锁。收尾发现仅核对 worker/instance 不足，已补控制门和终态驱动的 observer/ownership 交叉绑定；三个反例均在 RPC 前拒绝。Manager 使用固定 `extension-manager@00d1b940` 原 process_supervisor，无新 daemon；Manager SIGTERM 退出码 143 与 Router 的实际排空结果分别判定。

## 验证

- Linux x86_64 helper **108 项通过**，新增 13 项服务测试；严格 Clippy 通过。含真实 loopback 流式 HTTP、响应 trailer/error/EOF/drop、容量、超时保债、取消监督、来源校验与任务 panic。
- `tools.probe_service_manager` 的四个真实 Linux 子进程场景通过：正常排空、Router 排空期限、第二信号强退、Manager grace 到达。后三项重开同一 SQLite 后 active=1、paused=true、cursor=0；正常退出后 active=0、paused=true。验证 Manager 已回收子进程，不仅检查日志。后端明确是 CPU 替身。
- Python CPU **608 passed / 7 skipped / 3 deselected**、integration **3 passed**；Ruff 检查/格式、wheel 构建/干净安装验证通过。新增日志半行和损坏行两个反例。实现提交 `30de3d9` 六项 CI 均成功，包含新增 Manager 场景；[helper CI](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/39928596767)、[CPU/source CI](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/39928596817)。后续文档提交按其自身 HEAD 核验。
- 本地完整 Router 构建缺少离线缓存 anyhow，不是 OpenSSL/HUST 版本冲突。服务器初版 `30de3d9` 已过 aarch64 helper 107/严格 Clippy、HTTP 16、Manager 四场景、完整固定宿主 39 项和 release 构建；observer/ownership 审查修复后还需按新输入复验。没有修改全局环境：Manager 使用原项目 `.venv`，不向缺少 Manager 的推理 venv 临时加包。

测试 crate 新增 HTTP server/signal 特性和 `http-body=1.1.0` 直接引用。锁文件只增加原宿主已使用的 httpdate 1.0.3、errno 0.3.14、signal-hook-registry 1.4.8，不升级现有依赖；临时下载核对 crates.io checksum，仅写项目缓存。宿主候选直接引用同一 http-body，仍在 durable-candidate 关闭开关内。

## 范围与下一步

这是服务生命周期实现与 CPU 资格；固定宿主增加编译接线，原有限 NPU probe 未切换到该服务壳。尚未完成真实 HUST 常驻装配、自动化启动身份/旧 dispatcher 停止证明、控制/KV 工作线程统一关闭、公开配置和 Manager 用户入口。MOD 保持 `import_only`；没有生产生命周期或性能通过结论，不安排人员分工。

先完成固定宿主装配和线程关闭，再用 **1 张 NPU** 验真实请求/终态/信号排空，随后 **2 张 NPU** 验来源隔离及恢复；不运行空载保活。

复现：`cargo test --locked --manifest-path experiments/router-extension/checks/Cargo.toml`；构建同 crate 的 `service_lifecycle_probe`，在安装固定 Manager 的 Linux Python 环境运行 `python -m tools.probe_service_manager --binary <absolute-probe-path>`。Service 正常/超时/异常退出码为 0/75/70；示例 `/readyz` 和 fixture CLI 不构成已冻结公开配置。
