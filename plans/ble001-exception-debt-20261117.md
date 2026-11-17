# Contract — ADR-039 第一步：BLE001 三步开启

**冻结于实现之前**（2026-11-17）。依据：`docs/adr/adr-039-exception-debt-and-result-interface.md`
§决策 1（三步：盘点 → 收敛 → 开启）。

## 为什么现在做

ADR-039 把两笔异常处理债编号立项，并把 BLE001 的触发点写成「窗口后任意时间，一次
独立验收链路会话」。外部锐评的原话是「fail-closed 只活在中间件和文档里，没有活在
类型和 lint 里」——`app/` 内 112 处 `except Exception`，规则关着，同类 fail-open
还会再长出来。ADR-037 修的三处站点就是它们。

**本契约只做 ADR-039 §决策 1（BLE001），不做 §决策 2（Result 接口）**：后者按 ADR
的触发条件（下次修改 `collaboration.py` 执行接口时一并做）执行。

## Allowed paths

```
app/**                                    (只允许：收窄 except 子句、加 `# noqa: BLE001` + 理由、
                                           因收窄而需要的 import；不得改方法签名或公共行为)
pyproject.toml                            (只允许：从 [tool.ruff.lint].ignore 删除 "BLE001")
tests/unit/**                             (只允许：为「fail-open → fail-closed」的语义改变补测试；
                                           不得修改既有断言)
docs/adr/adr-039-exception-debt-and-result-interface.md   (状态从「预注册」改为「第一步已实施」，
                                           并记录盘点结论与未做的部分)
plans/ble001-exception-debt-20261117.md   (本文件)
```

## Acceptance criteria

1. **盘点可审计**：`app/` 内每一处 `except Exception`（含 `except Exception as e`、
   元组中的 `Exception`、以及裸 `except:`）都被归入三类之一，且每一处的归属写进
   PR 描述的分类表（文件:行 → 类别 → 一句话理由）：
   - **A 健康兜底**（重试边界、审计/通知「不拖垮主流程」的既定设计）→ 保留
     `except Exception`，加 `# noqa: BLE001` 且**冒号后必须有一句理由**；
   - **B 该 fail-closed**（吞掉异常会让危险操作继续）→ 改语义：改捕更窄的类型，
     或保留捕获但把失败传播出去；每一条改变都必须补一条测试；
   - **C 可收窄**（只需 `httpx.HTTPError` / `OSError` / `ValueError` 一类）→ 改 except 子句，
     并说明为什么收窄后不会漏。
   分类总数与 `ruff check --select BLE001 app` 在 base 上的命中数**必须一致**，
   PR 里给出两个数字。
2. **每一处 `# noqa: BLE001` 都有理由**：禁止裸 `# noqa` 或 `# noqa: BLE001` 后无文字。
   理由要说明**为什么这里必须兜住所有异常**，不接受「历史原因」「保持一致」。
3. **不改变公共行为**：B 类之外的改动不得改变任何函数/端点的返回值语义；B 类改动
   必须在 PR 里逐条列出「旧行为 → 新行为 → 为什么更安全 → 覆盖它的测试名」。
4. **规则真的生效**：`pyproject.toml` 的 `ignore` 里删除 `BLE001` 后，
   `ruff check --select BLE001 .` 零命中；`ruff check .` 全绿。
5. **存量测试全绿**：`pytest tests/unit -q`（基线以合并 #104 之后的 main 为准），
   不得弱化或删除任何既有断言，不得新增 `xfail`/`skip`。
6. **证据绑定**：HEAD SHA + `git status --short`；分类表与 `ruff` 输出都要能被
   verifier 逐条复核。
7. **不顺手做别的**：不合并 `except` 分支的其他重构、不改 CI、不改 dashboard、
   不改 pyproject 里除 BLE001 之外的任何配置。

## Verification commands

worktree 无 `.venv`，用主检出解释器并指向被验证的树：

```
V1  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m ruff check --select BLE001 app
V2  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m ruff check .
V3  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit -q
V4  git grep -n 'noqa: BLE001' -- app | wc -l     # 与分类表 A 类行数一致
V5  git grep -nE 'noqa(:\s*BLE001)?\s*$' -- app   # 无理由的 noqa 必须为空
```

V1 在改动树上必须 **exit 0 且零输出**；在 base 上应先记录命中数（盘点分母）。
V3 失败按 base 归因（同一 main 的干净 worktree 复现）；复现不到 → 阻塞。

## Forbidden

- 用一次性批量脚本无差别加 `# noqa: BLE001`（必须逐处判定并写理由）。
- 全局 `per-file-ignores` 或 `extend-ignore` 里继续压住 `BLE001`（等价于没开）。
- 为了让 V3 变绿而改测试、加 skip/xfail、或回滚 B 类改动。
- 把 B 类（fail-closed）改动混进无关重构；一个 commit 只做一类。
- 触碰 `app/routes/dashboard.py`、`.github/workflows/ci.yml`、台账、其他 ADR。
