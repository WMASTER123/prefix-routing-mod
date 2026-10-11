# 进度与下一步

更新：2026-10-11。状态：P0 骨架已建立，P1b 已进入新四卡服务器的 Qwen2.5-14B 单卡 eager 功能探测；尚无真实 Router 或性能提升结论。最新事实见[执行证据](../evidence/qwen14b-preflight-2026-10-11.md)，以下早期记录保留历史范围。

最新复核：用户已确认按 **环境 → 事件回归 → 单后端兼容 → 两副本 Router** 顺序推进，见[当前门槛](planning/runtime-event-gates.md)。G1 暂未通过：Triton Ascend 3.2.2 / OpenCV 的 NumPy 约束冲突已可重复确认；允许评估官方候选，但已查版本仍保留冲突链，未替换原环境。新增只读依赖审计及 CPU 测试，见[环境证据](../evidence/runtime-gate-2026-10-11.md)。先前诊断已收到真实 IPC BlockStored 与 TCP 清空事件，不代表完整事件/恢复通过。下一步不再并行接 Router。

## 本轮范围

- 已创建 WMASTER123/prefix-routing-mod 私有仓库。
- 已用固定 MOD 生成器建立包并修正规范版本、责任信息、惰性描述和不可启用边界。
- 已建立静态配置预检、统一测试入口、CPU mock、CI 配置与干净 wheel 检查工具。
- 文档目录覆盖架构、契约、复用、实施、规范、测试、运维、兼容、决策、实验与协作。
- 本地、GitHub Actions 与远端基础验证均已执行，结果见下方；功能里程碑仍按实际能力推进。

## 仍未完成的阶段门槛

| 阶段 | 下一任务与通过条件 |
|---|---|
| P0 | 基础闭环所需对象代码化样例与契约测试、外部 Router contract 核查、第二成员审查；ExecutionProfile 随消费它的增强功能后置 |
| P1a | 固定 Router 候选提交，验证公开接线、无 CUDA 的最小依赖、CPU mock 链路、aarch64 制品、索引恢复与实际 CPU 配额成本；再冻结底座 |
| P1b | 已确认基础 eager 生成；继续事件语义/恢复、取消、图配置与稳定启停分项验收。性能测试前确认独占时段；缺口实证后再决定 observer |
| P2/P3 | 两个 TP=1 副本基础闭环、可信状态和账本；N1–N4 按优化与诊断需要开展，不全部设为接线前置；推进 N5/N6 |
| P4/P5 | 强基线和负对照后选择方法；后台软计划单独消融，净收益成立才启用 |
| P6/P7 | 持续运行、跨节点、官方组合、发布制品和第二成员复现 |

本次不擅自选择未经验证的引擎组合、替换系统环境、下载模型、启动对公网服务或把旧 prefix-router 搬入新仓库。功能算法和人员分工仍按后续门槛决定。

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
