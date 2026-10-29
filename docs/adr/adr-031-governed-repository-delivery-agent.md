# ADR-031: 受治理的仓库交付 Agent

日期：2026-10-27
状态：已接受；S0 实现完成，真实仓库端到端验收待完成；S1 Verifier 为独立预注册实验
关联：ADR-021、ADR-023、ADR-027、ADR-030

## 决策

1. 产品入口收敛为 `DeliveryRequest -> DeliveryRun`。
2. 默认拓扑是单 Implementer；只读 Verifier 是预注册实验 S1，不是默认路径。
3. Implementer 可读写工作区并运行固定验证命令，不能访问 GitHub、审批或合并。
4. Verifier 只能读文件、搜索和运行固定验证命令，不能写文件、审批或合并。
5. 所有远端写仍经 `ChangePipeline` 的 bundle、审批和审计顺序。
6. PR 创建后直接登记 `MergeRecord`，合并继续由现有 worker、审批和 SHA 锁完成。
7. CI 红只转人工，v1 不做 CI 自动修复。
8. 不新增队列、dashboard、RAG、MCP、多租户或数据库表。

## 后果

- “开发辅助 Agent”不再是产品否认句；它是本 ADR 定义的受治理交付负载。
- 对外只能宣称“受治理的仓库交付 Agent”，S1 只能宣称“预注册实验能力”。
- 默认部署仍为 dry-run 和单 Agent；真实推送必须显式开启并人工审批。
- 在真实仓库完成推送、CI、合并审批与三方证据对账前，不得宣称 S0 闭环已验收。
