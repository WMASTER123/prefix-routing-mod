# 进度与下一步

更新：2026-10-10。状态：P0 骨架与本地验证已建立，P1b 已完成只读硬件盘点；未进入路由性能实现。

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
| P1a | 固定 Router 候选提交，验证公开接线、CPU mock 链路、aarch64 制品、索引恢复与 CPU 成本；再冻结底座 |
| P1b | 确认独占时段、软件/容器与模型身份；隔离验证候选引擎，单副本 APC/事件/流式/取消；再决定 observer |
| P2/P3 | 两副本基础闭环、可信状态和账本，完成第一个可用版本 |
| P4/P5 | 强基线和负对照后选择方法；后台软计划单独消融，净收益成立才启用 |
| P6/P7 | 持续运行、跨节点、官方组合、发布制品和第二成员复现 |

本次不擅自选择未经验证的引擎组合、替换系统环境、下载模型、启动对公网服务或把旧 prefix-router 搬入新仓库。功能算法和人员分工仍按后续门槛决定。

## 已执行结果

本地 Windows / Python 3.12.13：配置/CLI/MOD/工具单元测试通过，CPU mock 三项通过，sdist 和 wheel 构建成功，源码目录外干净 venv 安装/entry point/manifest/doctor 检查通过。

真实 Manager inspect 返回 activation_ready=false；enable 被 descriptor-only 拒绝且没有写入状态。NPU/性能/发布入口返回 not-run，未运行真实推理。

初始实现提交 8e9e559c5a9f16f77dbad9558890bc00f1107ee2：本地和服务器各通过 28 项单元/规范检查及 3 项模拟检查；两端均构建 sdist/wheel，并通过干净安装验证。

[首轮 GitHub Actions](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38054039836) 的 Linux / Python 3.11 与 3.12 两组均 success，含 CPU、mock、构建和干净 wheel 检查。

远端已克隆到 /root/work/prefix-routing-mod，独立 .venv 不继承系统推理包。服务器访问 GitHub 超时，测试依赖通过固定提交的完整 Git bundle 导入；临时进程级 Git URL 映射未修改全局配置、锁文件或依赖版本。参见 [验证摘要](../evidence/bootstrap-2026-10-10.md)。

系统只读清单：CANN 9.1.0、torch 2.10.0+cpu、torch-npu 2.10.0.post4、vllm 0.23.0+empty、vllm-ascend 0.23.0。与计划候选不同；这只是已安装元数据，不是可用性/兼容性结论，未覆盖或卸载。

GitHub 分支保护/强制审查尚未配置；协作规范已写入 CONTRIBUTING.md，不能把文档约定当作平台强制规则。
