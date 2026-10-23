# 服务就绪、HTTP 工作和有界排空

状态：实施中的 CPU 候选；不提升 MOD 的 `import_only` 状态。

复用原 Router HTTP/SSE、唯一 DurableRuntime 和 TerminalTask，不创建代理或第二账本。外层 Axum middleware 在 handler 前登记 HTTP 工作，持有到响应体结束、错误或丢弃；上传、排队和流式响应都计入。该计数不是后端负债，不执行结算。正文帧、trailer、状态和头部保持原语义。

服务从 starting 开始；只有来源集合与持久 cursor 完整匹配、同一控制门已安装、恢复已授权、监听已建立、所有终态来源新鲜且至少一个控制门有效，才返回 ready。M2 unknown 不阻止安全基础回退。ready 不保证容量或未来发送成功，M3 仍执行原子准入及发送围栏。失联撤销 ready；draining 不可重新 serving。就绪端点不进入业务工作计数。

停止先在同一服务锁内撤销就绪并禁止新的 HTTP 工作，再关闭所有控制门；继续原 HTTP 和终态任务。HTTP=0、后端 active=0 和所有来源已确认是停止后台任务的必要条件。对全部任务先发 stop，再 join 并检查 pending；最后等待监听退出。单一单调 deadline 覆盖等待和 join，重复停止不能延长期限。

超时或异常返回非成功，关闭 owner，保留已发负债、cursor 和未决确认；不 abort 持久提交，不把停止任务视为释放资源。超时后后台操作可能仍在完成，调用方必须退出整个自有进程，不能重用这个 runtime。硬期限由外部 Manager 负责；存储调用卡死时不能保证进程内硬实时退出。

Manager 继续复用固定 `extension-manager@00d1b940` 的 process_supervisor；其第一信号转发、第二信号或 grace 到期强退语义需以独立 CPU 子进程验证。Manager 收到 SIGTERM 时返回 143，不论子进程是否正常排空，故排空结果必须另记，不能单看 Manager 退出码。生产入口、公开配置和完整 NPU 恢复资格通过前不启用 manifest。
