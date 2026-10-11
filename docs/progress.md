# 进度与下一步

最新执行：W1 统一环境恢复入口在新目录通过，13 个步骤及 Qwen14B 基础推理/IPC 事件复现成功。本机研发 G1 技术证据齐备，正式冻结待人工复核，可继续 G2 测试。四张卡均已逐卡完成真实模型检查；[恢复、四卡、测试和审查证据](../evidence/w1-runtime-restore-2026-10-11.md)已记录。完整事件恢复、图/并发、Router 和性能仍未通过。下方早期 NumPy 冲突不适用于当前 HUST 候选。

上一轮运行验证代码已同步服务器；当时两端各 CPU 79、mock 3、构建及干净 wheel 检查通过，代码提交 74ca2cf 的 Actions success。[PR #4](https://github.com/WMASTER123/prefix-routing-mod/pull/4) 为待人工审查草稿，未合并，未启动生产 Router。下一工作包是把隔离恢复/制品证据收尾后进入完整原生事件回归，不重启版本试错或提前加算法。

更新：2026-10-11。状态：P0 骨架已建立，P1b 的 HUST 单卡 eager、基础 IPC 存储和重建复验已有证据；尚无真实 Router 或性能提升结论。近期工作以[下一阶段执行计划](planning/next-execution-plan.md)为准。

当前进入 W2 原生事件回归；W1 技术验证完成、正式冻结待人工复核。随后 W3 单后端目标执行配置与稳定性 → W4 成熟 Router 和两副本。HUST 快照/发布水位存在需验证的一致性边界，见[事件合同](kv-event-contract.md)；不能收到快照就恢复可信目录。主回归单卡，G4 两卡；用户追加四卡检查已真实完成，现有四卡足够。

## 本次计划整理（文档变更）

新增 W1–W4 执行计划，统一三份活规划及工作区副本，纠正旧 NumPy 阻塞、预装优先、旧物理卡映射及 P1a/P1b 并行等过期安排。更新事件可信性、目标性能配置、用卡和审查条件；未增加组件或算法。本次本地文档链接/围栏 2 项通过，差异检查通过；未运行新 NPU 实验，旧 CPU 79/mock 3 不计作本次复测。

## 已完成的工程基础

- 已创建 WMASTER123/prefix-routing-mod 私有仓库。
- 已用固定 MOD 生成器建立包并修正规范版本、责任信息、惰性描述和不可启用边界。
- 已建立静态配置预检、统一测试入口、CPU mock、CI 配置与干净 wheel 检查工具。
- 文档目录覆盖架构、契约、复用、实施、规范、测试、运维、兼容、决策、实验与协作。
- 本地、GitHub Actions 与远端基础验证均已执行，结果见下方；功能里程碑仍按实际能力推进。

## 仍未完成的阶段门槛

| 阶段 | 下一任务与通过条件 |
|---|---|
| P0 | 基础闭环所需对象代码化样例与契约测试、外部 Router contract 核查、第二成员审查；ExecutionProfile 随消费它的增强功能后置 |
| P1a | 在 G1–G3 通过后固定 Router 提交，核查公开接线、aarch64/无 CUDA 最小依赖、事件索引兼容与 Host 成本；通过 ADR 冻结底座 |
| P1b | G1 自动恢复技术验证通过；继续 G2 真实淘汰/清空/重放/断连/重启及可信恢复；G3 验证目标图/构建配置、聊天接口、并发取消、启停和持续运行 |
| P2/P3 | 两个 TP=1 副本基础闭环、可信状态和账本；N1–N4 按优化与诊断需要开展，不全部设为接线前置；推进 N5/N6 |
| P4/P5 | 强基线和负对照后选择方法；后台软计划单独消融，净收益成立才启用 |
| P6/P7 | 持续运行、跨节点、官方组合、发布制品和第二成员复现 |

实施边界：不修改全局环境、公共模型或旧 prefix-router，不启动对公网服务；没有证据不扩展兼容或性能声明。当前 PR 待另一成员审查，不自动合并。下方为分阶段历史记录，旧版本选择和旧机器状态不作为当前执行指令。

## 2026-10-11 早期规划修订（真机执行前）

- 读取新增 NPU 知识资料后，保留 M1–M8 与通用组件复用；加入构建/语义/性能三道验收门槛。
- 增补 BackendCapability/ExecutionProfile 设计合同及语义、缓存布局/协议、执行性能三类身份；未改变当前 config schema 或实现能力。
- 明确图/KV 分开预热、实际 KV blocks、异步观测语义、Host CPU 配额竞争与 N0–N6 最小实验；不新增算子、分层或 P/D 范围。
- docs/planning 保存三份当前详细规划；docs/reference 原始快照不变。变更理由见 [ADR](decisions/2026-10-11-ascend-first.md)，知识应用见 [Ascend 设计约束](ascend-design.md)。
- 本次不连接服务器、不运行知识库脚本；没有新增 NPU 或性能结果。七对象与两类档案的可执行 schema/样例仍是 P0 待办，不能把文档测试等同合同实现。

## 已执行结果

2026-10-11 文档修订：本地 CPU 29 项、合成集成 3 项、sdist/wheel 构建及干净 wheel 验证通过。新增测试只校验文档，没有新增真实推理覆盖；见 [本次记录](../evidence/ascend-planning-2026-10-11.md)。以下服务器和首轮 CI 结果属于此前提交，不覆盖本次修订。

本地 Windows / Python 3.12.13：配置/CLI/MOD/工具单元测试通过，CPU mock 三项通过，sdist 和 wheel 构建成功，源码目录外干净 venv 安装/entry point/manifest/doctor 检查通过。

真实 Manager inspect 返回 activation_ready=false；enable 被 descriptor-only 拒绝且没有写入状态。NPU/性能/发布入口返回 not-run，未运行真实推理。

初始实现提交 8e9e559c5a9f16f77dbad9558890bc00f1107ee2：本地和服务器各通过 28 项单元/规范检查及 3 项模拟检查；两端均构建 sdist/wheel，并通过干净安装验证。

[首轮 GitHub Actions](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38054039836) 的 Linux / Python 3.11 与 3.12 两组均 success，含 CPU、mock、构建和干净 wheel 检查。

远端已克隆到 /root/work/prefix-routing-mod，独立 .venv 不继承系统推理包。服务器访问 GitHub 超时，测试依赖通过固定提交的完整 Git bundle 导入；临时进程级 Git URL 映射未修改全局配置、锁文件或依赖版本。参见 [验证摘要](../evidence/bootstrap-2026-10-10.md)。

早期系统只读清单：CANN 9.1.0、torch 2.10.0+cpu、torch-npu 2.10.0.post4、vllm 0.23.0+empty、vllm-ascend 0.23.0。当时仅为元数据；后续用户确认优先使用验证通过的预装组合，当前局部实测范围见下。

GitHub 分支保护/强制审查尚未配置；协作规范已写入 CONTRIBUTING.md，不能把文档约定当作平台强制规则。

## 2026-10-11 P1b 实施

- 新机器 4×910B2/16 核配额/128 GiB RAM，已同步已有规划开发提交；先使用逻辑卡 0 单副本，不占满四卡。
- 新增显式触发、有限超时、仅清理自有进程组的探测工具及 CPU 回归测试；未安装全局依赖，未修改引擎和公共模型。
- 公共 Qwen2.5-14B TP=1 BF16 eager 已完成非流式、重复和 SSE 请求；APC 计数器有命中。ZMQ 接线与关闭问题的诊断、修正和复测逐次保留证据，不覆盖失败记录。
- 三份当前详细规划和根目录工作稿同步修订；docs/reference 不变。新增[基线决策](decisions/2026-10-11-qwen14b-first.md)。
- 原先安排的“P1a Router 审计同时补 P1b”已由页首 G1–G4 顺序替代。当前先消除环境阻塞，再补事件、单后端兼容，之后才接成熟 Router；暂不堆叠预测档案或新算法。
- 当前为自检与待人工审查分支；不自动合并，不称为独立审查通过。完整 npu-smoke/npu-routing/performance/release 仍 not-run。
