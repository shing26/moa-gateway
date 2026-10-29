# ADR-028: 并发 Trial 与持久化 HITL 是执行契约的一部分

## 背景

实验台此前逐单元、逐 Trial 串行执行。恢复只依赖已经写下的 Run Record，但正式矩阵
需要更长时间；如果为了提速临时改变并发度，又不在结果身份里记录，同一批次就可能
混入无法解释的执行条件。协作图的 HITL 则停在进程内 checkpointer，进程重启后挂起
状态消失，审批端只能得到“已失效”，无法证明它曾经存在。

## 决策

1. `BatchRunner` 在所有未完成 Experiment 之间并发调度。全局信号量限制同时执行的
   Trial 数；单个 Experiment 仍使用一个执行器并串行跑自己的重复 Trial，避免模型会话、
   用量记账和工作区状态串线。
2. `max_concurrency` 与 `rate_limit_per_minute` 写入 Batch Manifest。恢复批次时必须
   与原值一致；改变并发或启动速率属于新的执行契约，不覆盖旧批次。
3. 单次 Trial 仍使用独立 Experiment ID 和独立工作目录。全局闸门只限制同时运行数，
   不共享可变工作区。
4. 取消会取消所有在途单元，把未完成单元恢复为 `pending`，保留已落盘 Run Record，
   并把批次标为 `paused`。再次执行从已有记录继续，不重写旧文件。
5. 协作链路默认可使用 SQLite `AsyncSqliteSaver`，路径由
   `COLLAB_CHECKPOINT_PATH` 指定。`:memory:` 只作为显式临时模式。
6. 持久化 checkpointer 的读取使用异步 API：`apending_payload()` 与 `afinish()`。
   进程关闭时由应用 lifespan 调用 `aclose()` 释放 SQLite 连接。
7. 并发与串行批次共用 `Experiment → Trial → Run Record` 证据契约；并发不作为能力
   结论写进单个 Trial。

## 替代方案

- **同一 Experiment 内并发 Trial**：当前执行器为每个 Experiment 创建一个模型客户端
  和 Agent 实例，直接并发会让会话与用量状态互相污染。先保持单 Experiment 串行。
- **只提高进程内并发，不记录参数**：吞吐变快，但恢复时无法证明执行条件一致。
- **继续使用 `InMemorySaver`**：离线演示够用，但无法满足跨重启 HITL 恢复。
- **在协议哈希中加入并发度**：会把调度基础设施混入模型能力协议。这里固定为单位
  Batch 的执行契约，并让恢复拒绝漂移。

## 后果

- 正式批次可以显式提高吞吐，但必须承担相同并发契约的恢复约束。
- SQLite 是单机持久化方案，不解决多节点共享；跨节点部署应替换为 Postgres/Redis
  checkpointer，本 ADR 不伪造多节点能力。
- 并发默认仍为 1，旧脚本和旧批次行为不变；需要加速时显式传
  `--max-concurrency`。
