"""基础设施层：SQLite 权威存储、跨进程锁、结构化日志、事件传输。

依赖方向：本层可以依赖 domain 与 ports；domain 与 ports 不得依赖本层。
"""
