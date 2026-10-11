# 进度与下一步

更新：2026-10-11。状态：P0 骨架与本地验证已建立，P1b 已完成只读硬件盘点；Ascend 优先规划已修订，未进入真实 NPU 推理或路由性能实现。

## 本轮范围

- 已创建 WMASTER123/prefix-routing-mod 私有仓库。
- 已用固定 MOD 生成器建立包并修正规范版本、责任信息、惰性描述和不可启用边界。
- 已建立静态配置预检、统一测试入口、CPU mock、CI 配置与干净 wheel 检查工具。
- 文档目录覆盖架构、契约、复用、实施、规范、测试、运维、兼容、决策、实验与协作。
- 本地、GitHub Actions 与远端基础验证均已执行，结果见下方；功能里程碑仍按实际能力推进。

## 仍未完成的阶段门槛

| 阶段 | 下一任务与通过条件 |
|---|---|
| P0 | 七类对象代码化样例与契约测试、外部 Router contract 核查、第二成员审查；不把当前文档等同全部冻结 |
| P1a | 固定 Router 候选提交，验证公开接线、无 CUDA 的最小依赖、CPU mock 链路、aarch64 制品、索引恢复与实际 CPU 配额成本；再冻结底座 |
| P1b | 确认独占时段、整栈配套与模型身份；隔离验证 N0、目标图/APC/事件/流式/取消组合及事件异步语义；再决定 observer |
| P2/P3 | 两个 TP=1 副本基础闭环、N1–N4 校准、可信状态和账本；推进 N5/N6，完成第一个可用版本 |
| P4/P5 | 强基线和负对照后选择方法；后台软计划单独消融，净收益成立才启用 |
| P6/P7 | 持续运行、跨节点、官方组合、发布制品和第二成员复现 |

本次不擅自选择未经验证的引擎组合、替换系统环境、下载模型、启动对公网服务或把旧 prefix-router 搬入新仓库。功能算法和人员分工仍按后续门槛决定。

## 2026-10-11 规划修订

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

系统只读清单：CANN 9.1.0、torch 2.10.0+cpu、torch-npu 2.10.0.post4、vllm 0.23.0+empty、vllm-ascend 0.23.0。与计划候选不同；这只是已安装元数据，不是可用性/兼容性结论，未覆盖或卸载。

GitHub 分支保护/强制审查尚未配置；协作规范已写入 CONTRIBUTING.md，不能把文档约定当作平台强制规则。
