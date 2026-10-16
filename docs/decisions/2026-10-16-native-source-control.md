# 固定 HUST 的独立只读身份控制接口

状态：研发候选，需 CPU 与真实 HUST 联调后接受。适用 Core ced6857 / Ascend b47795c，沿用默认关闭、源码守卫和 loopback token 认证。

原生 KV 传输不含 publisher epoch，需要在重放前后核对实际前端/核心身份。现有 terminal 读取接口同时携带有界终态历史；目录控制不应读取全部历史、推进消费者 ACK，或依赖已经回收的游标。

在已有可选 terminal endpoint plugin 内增加 `GET /prefix-routing/identity`，复用原 utility 的核心身份验证及锁。响应只有 schema_version、binding(worker_id/instance_epoch/observer_epoch)、frontend_pid、core_pid、core_boundary、frontend_boundary、family_count 和 disabled。它不返回请求内容，不确认/回收前端终态，不产生 KV 驻留；核心轮询仍可将真实终态观察到同一内存 journal。

Rust M7 控制适配仅在专属阻塞 owner 线程调用，使用宿主已有 reqwest，限定明确的 loopback HTTP origin、禁用代理/重定向、有界响应及总请求超时，Debug 不输出 token。初始化须匹配启动器实际 PID、worker 身份及受控空边界；之后每次 verify 核对同一 binding/PID 和非 disabled 状态。KV/model identity 仍由独立已核验的配置产生，不能从此接口推断。

不修改 HUST 引擎文件、调度或淘汰。新接口不会补出原生 publisher epoch/完整快照，不能支持任意已热接管或超窗恢复。终态持久 ACK/GC 和 M3 持久化仍分别验收；该接口不能作为释放负债的证据。
