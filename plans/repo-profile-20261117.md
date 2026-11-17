# Contract — 仓库档案（D 阶段：换一个仓库不改代码、不改环境变量）

**冻结于实现之前**（2026-11-17）。所有者决定：D 阶段先用 moa-gateway 自己当第二个仓库跑通。

## 为什么

`delivery run` 的验证命令现在是**全局环境变量**（`CHANGE_AGENT_VERIFY_COMMAND`，
回退到 `CODE_REVIEW_VERIFY_COMMAND`，见 `app/config.py:266`；由
`verify_command_from_env` 做 `shlex.split`，见 `apps/code_review_pipeline/change/pipeline.py:663`），
基准分支同样来自 `CHANGE_AGENT_BASE_BRANCH`（`app/config.py`）。

后果：**换一个仓库 = 改环境变量 + 重启进程**。第二个人在自己的机器上给两个仓库做交付，
要么反复改 `.env`，要么为每个仓库起一套进程。这是「通用性」最直接的一处硬伤，也是
路线图「阶段 1 D」要修的东西。

## 设计

新增**仓库档案**：一个按仓库归位的 TOML 文件，承载该仓库交付所需的配置。

```
config/repos/<owner>/<repo>.toml
```

示例（本阶段随仓库落地的那一份）：

```toml
# 交付给 shing26/moa-gateway 时使用的仓库档案
verify_command = ".venv/Scripts/python.exe -m pytest tests/unit -q"
base_branch = "main"
```

字段（v1，只做这两个，别的等真的有需要再加）：

| 字段 | 必填 | 说明 |
|---|---|---|
| `verify_command` | 是 | 字符串，按 `shlex.split` 解析成 argv（与既有环境变量同一套语义） |
| `base_branch` | 否 | 默认 `main` |

**优先级**：档案存在 → 用档案；档案不存在 → 回退到现有环境变量；两者都没有 → 与今天
一致的报错（`delivery run` exit 2）。档案**存在但解析失败**（坏 TOML、缺 `verify_command`、
`verify_command` 为空）→ **报错退出，不得回退环境变量**：一个坏掉的档案如果悄悄回退，
就会用另一个仓库的验证命令去验一个它不认识的工作区，那是最危险的静默错误。

档案目录允许覆盖：`DELIVERY_REPO_PROFILE_DIR`（默认 `config/repos`，相对仓库根解析）。

## Allowed paths

```
apps/code_review_pipeline/delivery/repo_profile.py    (新文件：定位、解析、校验)
app/cli.py                                            (只改 delivery run / preflight 的配置来源与提示行)
app/config.py                                         (只加 DELIVERY_REPO_PROFILE_DIR 一个只读设置)
config/repos/shing26/moa-gateway.toml                 (本阶段真实使用的档案)
tests/unit/**                                          (只新增)
docs/adr/adr-041-per-repo-profile.md                  (新 ADR：为什么是档案而不是环境变量、优先级、失败姿态)
docs/roadmap-generality-20261117.md                   (D 阶段的路线图原件，入库)
.env.template                                         (只加 DELIVERY_REPO_PROFILE_DIR 及注释)
plans/repo-profile-20261117.md                        (本文件)
```

## Acceptance criteria

1. **解析与校验可单测**：`load_repo_profile(repo, root)` 之类是纯函数（只读文件系统，
   不读环境变量、不发网络），返回 `RepoProfile(verify_command: tuple[str, ...], base_branch: str)`
   或抛出一个**明确类型**的异常（例如 `RepoProfileError`），消息里带文件路径与原因。
2. **优先级三条路各有测试**：
   - 档案存在 → 用档案（即使环境变量另有一套，也必须用档案）；
   - 档案不存在 → 用环境变量；
   - 两者都无 → `delivery run` 打印既有报错文句并以 exit 2 结束。
3. **坏档案 fail-closed 三种**：坏 TOML、缺 `verify_command`、`verify_command` 为空字符串
   ——都必须报错退出且**不读取环境变量**（测试要构造「环境变量有效 + 档案损坏」的组合，
   断言不走环境变量）。
4. **路径安全**：`repo` 必须是 `owner/name` 形态（各自 `[A-Za-z0-9._-]`，不含 `..`、
   不含分隔符），构造出的路径必须落在档案目录之内；越界输入必须被拒绝并给明确错误。
5. **可观测**：`delivery run` 在开跑前打印一行来源，形如
   `verify command: <argv> (source: profile config/repos/shing26/moa-gateway.toml)`，
   `preflight` 打印所用 `base_branch` 与来源。**不得**回显任何 token 或密钥。
6. **不改变既有语义**：没有档案时行为与今天逐字一致（同样的错误文句、同样的 exit code、
   同样的 `shlex.split` 语义）；`DeliveryPolicy` / `DeliveryRequest` / 状态机零改动。
7. **既有测试全绿**，`tests/` 只新增不删改。
8. **证据绑定**：HEAD SHA + `git status --short`。

## Verification commands

```
V1  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit/test_repo_profile.py -q
V2  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit/test_delivery_cli.py tests/unit/test_config.py -q
V3  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m ruff check .
V4  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit -q
```

`V1` 的文件名以实现实际创建为准，PR 里写清。**本契约不含真实交付运行**：跑那 1–2 单是
合并之后的运维动作，不属于本次验收（合并后由主会话执行并留证）。

## Forbidden

- 档案存在但损坏时回退环境变量（见 criteria 3）。
- 从被交付仓库的检出里读档案（档案属于**部署方**，不属于被交付仓库；否则每个被交付
  仓库都得先被交付一次才能配置，鸡生蛋）。
- 把 `DELIVERY_REPO_PROFILE_DIR` 之外的路径、或 `..` 拼出来的路径当档案目录。
- 在档案里写入 token、密钥、PAT。
- 改 `DeliveryPolicy` / 状态机 / 推送与合并闸门 / 飞书与 GitHub 评论通道。
- 顺手把其他环境变量搬进档案（本阶段只搬 `verify_command` 与 `base_branch`）。
