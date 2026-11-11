"""ctxsdd：面向软件持续演进的 SDD 上下文管理系统。

流水线（每一步都是独立进程，状态经 PowerContext 持久化）：

    init        把 specs / 代码 / 测试分块导入 OceanBase（seekdb）并建全文索引
    plan        用任务关键词在 OceanBase 中检索，构造可定位的开发上下文
    ask         把上下文交给 MiniMax 生成补丁，记录完整调用日志
    apply       应用补丁并留存修改前后快照
    verify      跑目标测试与回归测试，把结果写回 PowerContext
    state-show  在新进程中读取并打印任务状态（重启后复用证据）
"""

__version__ = "1.0.0"
