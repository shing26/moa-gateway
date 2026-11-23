# 交付记录：retry 抢占过期锁时补警告

这不是事后补写的契约，而是**一次真实交付的留痕**：该单由
`delivery run` 自行完成（实现 → 确定性门禁 → 推送审批 → CI → 合并审批 →
合并），没有预先冻结 `plans/<task-id>.md`，所以按交付记录归档，避免把
「事后补契约」当成「事前冻结」。

## 1. 交付内容

`apps/code_review_pipeline/delivery/retry.py` 的锁获取分支原先忽略
`acquire()` 返回的 `StaleLockReplaced`，与 `app/cli.py::cmd_delivery_run`
的行为不一致。本次补齐：

- 模块顶部 `import sys`，`lockfile` 导入补 `StaleLockReplaced`；
- `lock = acquire(lock_path)` 之后，若返回 `StaleLockReplaced`，向 stderr
  打印与 `delivery run` 同格式的
  `warning: replaced stale lock held by pid=... started_at=... host=...`；
- `tests/unit/test_delivery_retry.py` 新增一条用例，monkeypatch
  `retry.acquire` 返回 `StaleLockReplaced`，断言 stderr 文案与重试仍继续。

diff：2 个文件，`retry.py` +9/-1，测试 +52/-0。

## 2. 运行证据

| 项 | 值 |
|---|---|
| run_id | `dlv-effee4ed01d64e0fa2c6ef8e867325ba` |
| base_sha | `5ff1a7865433f4c7c2488425b998dd8ee521037a` |
| 分支 | `feat/resume-stale-lock-warning-3-20261123-delivery-1795419424802764700` |
| 实现 attempts | 1 |
| 确定性门禁 | `exit_code=0`（全量 `verify_command`） |
| bundle_hash | `16c04a1cba2cf85c1c54805bdb5870b88ed6000d78db51ee5590f7d181abe3f0` |
| 变更文件数 | 2 |
| 用量 | 107 次 LLM 调用 / 882,278 tokens（预算 1,000,000） |
| PR | #133，head `fb644e7421bbabdbd872aa6ee9cec13f40e78482` |
| CI | `test` pass、`integration` pass、`docker` skipped |
| 合并 | `f62119b8233a772e2594a06b077a6b7c0a70a953` |
| 合并审批 | 飞书卡片人工批准，approver `ou_ae3e5cc009fae3462147e15e8c271602` |

## 3. 签收

**所有者书面指令**：2026-11-24 消息「点掉 #133 → 补 #132/#133 留痕 →
加 merge worker 启动与健康检查 → 再做剩下 3 单」；合并审批另行通过飞书卡片
完成（2026-11-24 00:39 +08:00）。

## 4. 过程中暴露的问题

1. **合并审批卡依赖独立进程**：`app/main.py` 只做 `merge_channel_readiness`
   自检，不启动 `MergePoller`；审批卡由 `python -m app.merge_worker` 轮询发出。
   该进程未启动时，CI 转绿也不会有人收到卡，失败形状是「静默不发卡」。
2. **webhook 进程卡片发送器未初始化**：`moa.merge_notifier feishu card sender
   not initialized; skip PR opened card` 每次 PR 打开都出现，PR 打开通知实际
   从未送达。
3. **模型用量偏高**：2 文件、60 行的改动消耗 88 万 tokens / 107 次调用，
   与「读文件循环」的历史样本一致，属于评测与提示词层面的待解问题。
