# 多智能体 M5 单次全量基线报告

> 状态：已完成
> 批次：`m5-baseline-live-r1`
> 完成时间：2026-10-23
> 结论边界：只有单次样本，不授权任何统计显著性声明

## 实验身份

| 字段 | 值 |
| --- | --- |
| 协议 | `multi-agent-first-batch` v3 |
| 协议哈希 | `20b236344df8bf44f7e8417be6a029c65d1805bc4e608cb91c76eeed6dad8701` |
| provider / model | `nvidia` / `nvidia/nemotron-3.5-lightning-30b-a3b` |
| 预算档 | B1：40 steps / 400,000 tokens |
| 矩阵 | 8 tasks × 4 arms × 1 repeat = 32 runs |
| 成本信号 | 不可用；所有 Run Record 的 `cost_usd` 均为 0，不据此作成本优势结论 |

权威产物位于 `.bench-experiments/_batches/m5-baseline-live-r1/`：

- `batch.json`：32 / 32 单元完成，`missing_runs=0`；
- `report.json`：机器可读聚合；
- `report.md`：人可读聚合；
- 32 个独立 Experiment 各有一条 `trials/trial-000.json` Run Record。

对同一完成批次连续执行两次权威汇总入口后，报告哈希保持一致：

- `report.json`：
  `C5B703C8AEAED5766A52BD9635E4F5CB9DD3EEC3C50EFE722112E906B77B3E7A`
- `report.md`：
  `CB8C35C5F62EB3B72F2433C7B1B10A054486EF854DF064998747F49AED9E4067`

## 总体结果

32 条 Run Record 的最终状态为：

| 状态 | 数量 | 是否进入通过率分母 |
| --- | ---: | --- |
| `pass` | 15 | 是 |
| `fail` | 8 | 是 |
| `budget_exhausted` | 0 | 是 |
| `error` | 9 | 否 |

因此有效样本为 23 条，整体有效通过率为 15 / 23 = 65.2%。这只是一个单次描述性
比例，不是对 L0-L3 优劣的统计判断。

## 按 Arm

| Arm | Runs | 有效样本 | Pass | Fail | Error | 有效通过率 | 总 token | 平均 token |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| L0 | 8 | 6 | 4 | 2 | 2 | 66.7% | 1,250,847 | 156,356 |
| L1 | 8 | 7 | 4 | 3 | 1 | 57.1% | 1,965,179 | 245,647 |
| L2 | 8 | 6 | 5 | 1 | 2 | 83.3% | 1,665,984 | 208,248 |
| L3 | 8 | 4 | 2 | 2 | 4 | 50.0% | 1,752,770 | 219,096 |

方向性观察：

- L2 在本批次有最高的有效通过率，但只有 6 个有效样本；不能据此宣称统计优势。
- L1 使用了最多的总 token；但不同 run 的完成路径和错误数不同，不能直接解释为
  单纯的协作开销。
- L3 出现 4 个 `error`，有效样本最少。当前证据不足以区分“L3 质量较差”和
  “L3 更容易触发模型输出上限或端点错误”。
- `bd4f186` 在 L0-L3 全部得到 `fail`，是四个 arm 都没有通过的真实失败证据；
  单次运行仍不足以估计稳定难度。

## 按任务来源

| 来源 | Runs | 有效样本 | Pass | Fail | Error | 有效通过率 | 总 token |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| E1 历史缺陷 | 20 | 17 | 9 | 8 | 3 | 52.9% | 5,131,042 |
| E3 合成缺陷 | 12 | 6 | 6 | 0 | 6 | 100.0% | 1,503,738 |

E3 的 100% 只来自 6 条有效记录；另外 6 条全部是 `error`。特别是
`e3-c2-keyword-normalization` 的四个 arm 都因 429 失败，不能把它解释为
“四个 arm 都通过”，也不能拿来支持 E3 难度更低的结论。

## 错误分类

9 个 `error` 均未进入通过率分母：

| 原因 | 数量 | 涉及单元 |
| --- | ---: | --- |
| 单次 completion 达到 4096 token 后 `finish_reason=length` | 5 | `0896629×L0`、`60c64ed×L3`、`988fccb×L2`、`e3-c1-budget-ttl-refresh×L3`、`e3-c3-binary-write-review×L3` |
| 端点 429 `Too Many Requests` | 4 | `e3-c2-keyword-normalization×L0/L1/L2/L3` |

这批 M5 运行发生在协议声明的错误重试真正接入执行器之前，因此 9 个错误都只记录了
第一次 attempt。该实现缺口已修复：后续正式批次会对 `error` 按协议最多重试一次，
并把每次 attempt 的状态、错误、usage 和工作区快照写入 Run Record 的
`evidence.retry_attempts`；`fail` 与 `budget_exhausted` 不重试。

## 判定

M5 完成判据已满足：

- 32 个矩阵单元均有结果，没有缺格；
- 每个结果都能映射到独立 Experiment 和 Run Record；
- `error` 与 `fail` 分开聚合，错误未进入通过率分母；
- 报告可由持久化记录重算，连续重算哈希一致；
- 报告明确保留单次样本限制，不输出统计优势结论。

下一步正式实验必须使用已冻结的 M8 执行契约与修复后的错误重试语义，完成
`8 × 4 × 3 = 96` 条 B1 记录，再只对最终为 `fail` / `budget_exhausted` 的单元派生
B3。任何最终 `error` 继续保留为缺口，不得通过重试或筛选静默抹掉。
