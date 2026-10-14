# MOD / 插件规范映射

2026-10-14 最新实机状态：新分配 32068 的 HUST ced6857/b47795c 已完成真实 HTTP→完整 family→核心逻辑终态→原账本的可选有限闭环。4 个 family/8 个 child 全部关联，stop string/断连核心 abort、重放、账本重开和自有包装恢复通过；不是原生 Router 已联通，也不是在线 KV 目录或性能验收。默认关闭、固定守卫、MOD import_only 不变。当前限制和下一步以[实机接线说明](planning/cedb477-live-terminal.md)及[证据](../evidence/cedb477-live-terminal-2026-10-14.md)为准，下方按日期保留历史。

## 已采用的真实来源

- [extension-template](https://github.com/vLLM-HUST/extension-template/tree/0d537f3193f217b21cf730a32f0a2629b631244b)，通过 tools/new_extension.py --kind import_only 生成。
- [extension-manager](https://github.com/vLLM-HUST/extension-manager/tree/00d1b940dc6ce8d60e2cd28708b80d80e9c33a09)，CPU 测试依赖固定此提交。它是本次实测版本；与原计划曾引用的 0e493914 不混同。
- 代码、包、manifest 的 ID 均为 io.github.wmaster123.prefix-routing-mod。

模板的占位协议、错误 canonical URL、旧依赖和 0.3-experimental 写法已替换。manifest 使用 0.3；kind=control_plane_extension、runtime=external_service 描述未来职责，implementation=import_only 描述当前事实。没有声明不存在的 provider 或协议版本。

## 验收映射

| 要求 | 当前处理 | 后续门槛 |
|---|---|---|
| 静态发现无副作用 | 惰性元数据入口；不导入引擎、不连网、不起服务 | 每次包变更回归 |
| 未实现不可启用 | 用真实 Manager 测试拒绝 enable，配置不落盘 | 契约实现前保持 |
| 制品可安装 | 干净临时 venv 安装 wheel、加载包内 manifest | CPU 仅是制品验证 |
| 外部生命周期 | external_operator；当前无控制共享服务动作 | P1/P7 验证合法 plan/render/check |
| 身份与版本 | 支持矩阵分候选/已验证 | 不将 metadata 版本当兼容证据 |
| 启停/回退 | 当前仅卸载描述包；没有运行中的 Router | P2/P7 实测服务排空与入口回退 |
| 可选引擎 hook | 用户批准实验的 general plugin publisher 适配；默认关闭、固定版本/源文件守卫，不替换 Ascend platform plugin | 见 [ADR](decisions/2026-10-11-optional-hust-publisher.md)；功能、关闭回退、开销与目标配置分别验收，实验入口不是 Manager 激活 |
| 责任与证据 | WMASTER123 个人私有仓库、顾问 unknown、无性能声明 | 正式组内准入另行审查 |

host.version_range >=0 仅允许惰性描述被解析，不是“兼容所有 vLLM”的声明；激活被明确阻止。未来启用前必须改成经过验证的守卫。components/protocols 为空，不伪装 KV connector 获取准入。

模板中组织归档要求与当前私人建仓是两回事：本仓库是个人开发仓库，未申请组内目录/网站收录，也不宣称完整 MOD 发布认证。

新增 `vllm.general_plugins` entry point 只允许在明确 opt-in 的实验中绑定内部 zmq 工厂。静态 MOD 描述与 package import 不触发它；默认插件加载亦无引擎导入或绑定副作用。关闭开关并移除 allowlist 后重启测试进程即可回到原生 publisher，不支持进程内热切换。该入口、内部依赖、验证范围和回退须在审查中单独检查，不能借此跳过模板/Manager 最终启用合同。
