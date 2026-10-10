# ADR-001：先完成可验证的工程载体

状态：已采用。日期：2026-10-10。

决定：个人私有仓库 WMASTER123/prefix-routing-mod；使用组内 import_only 生成器，按固定 Manager 的真实 0.3 schema 修正。Python 只承载描述、预检和测试；Router/索引选型继续在 P1a 验证，不复制完整引擎或重写生产代理。

原因：用户要求可以上手、复用成熟组件且符合 MOD 规范；外部请求 Router 的可激活 contract 尚未确认。套用 KV connector 或占位协议会制造错误兼容承诺。

代价：当前不能通过 Manager enable 启动路由，也不能演示推理性能。收益是后续各阶段有统一门槛和可重现测试。

退出条件：实际 Router/契约被实现并通过启用、执行、停用与兼容测试后，以新 ADR 替换 import_only 状态；不能只改字符串让验收变绿。

模板原定组织 URL 与本次个人建仓不同，canonical 如实填写；正式组内收录另行办理。Apache-2.0 延续模板，后续引入代码先审查许可证。
