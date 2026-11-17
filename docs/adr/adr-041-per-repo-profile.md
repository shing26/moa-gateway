# ADR-041: 仓库档案——按仓库承载交付配置，而不是全局环境变量

日期：2026-11-17
状态：已实施（第一切片：`verify_command` 与 `base_branch` 两个字段）
关联：ADR-030（受治理变更）、ADR-031（受治理仓库交付）、`plans/repo-profile-20261117.md`

## 背景

`delivery run` 的验证命令来自**全局**环境变量 `CHANGE_AGENT_VERIFY_COMMAND`
（回退 `CODE_REVIEW_VERIFY_COMMAND`，见 `app/config.py`），基准分支来自
`CHANGE_AGENT_BASE_BRANCH`。换一个仓库交付，就要改 `.env` 并重启进程。

一个人在自己的机器上给两个仓库做交付时，只有两种做法：反复改同一份 `.env`
（容易把 A 仓库的验证命令用在 B 仓库的工作区上），或为每个仓库起一套进程
（重复占端口、重复持锁）。这正是通用性路线图「阶段 1 D」要消除的硬伤。

## 决策

### 1. 档案是部署方的配置，不是被交付仓库的一部分

档案放在**部署方自己的仓库**里：

```
config/repos/<owner>/<repo>.toml
```

不从被交付仓库的检出里读。否则每个新仓库都得先被交付一次才能拥有配置——
鸡生蛋。档案目录可用 `DELIVERY_REPO_PROFILE_DIR` 覆盖（默认 `config/repos`，
相对部署仓库根解析）。

v1 只承载两个字段，别的等真的需要再加：

| 字段 | 必填 | 说明 |
|---|---|---|
| `verify_command` | 是 | 字符串，按 `shlex.split` 解析成 argv（与既有环境变量同一套语义） |
| `base_branch` | 否 | 默认 `main` |

### 2. 优先级：档案 > 环境变量 > 既有默认

- **档案存在** → 两个字段都取自档案，即使环境变量另有一套；
- **档案不存在** → 回退到既有环境变量，`verify_command` 仍走同一套
  `shlex.split`，缺配置时的报错文句与 exit code 逐字不变；
- **档案存在但损坏** → 报错退出，**不回退**环境变量。

显式命令行参数（`delivery preflight --base-branch`）优先于档案：人在终端前
当次给出的值，是对这次运行最具体的意图。

### 3. 坏档案 fail-closed

坏 TOML、缺 `verify_command`、`verify_command` 为空字符串（以及无法按 shell
语义拆分、`base_branch` 为空）都属于「档案存在但损坏」。

不悄悄回退环境变量，理由只有一条：**回退会把错误变成静默**。环境变量可能是
给另一个仓库准备的值，拿它去验证一个它不认识的工作区，可能恰好通过，也可能
以误导性的方式失败；两种结果都比「档案写错了，先修档案」更难诊断。宁可让
`delivery run` 在开工前停住。

### 4. 路径安全

`repo` 必须匹配 `owner/name`（两段各自 `[A-Za-z0-9._-]`），不得含 `..` 或
额外分隔符。拼出的档案路径经 `Path.resolve()` 解析后必须仍在档案根之内——
这同时挡住 `..` 与指向根外的符号链接。越界或非法输入抛 `RepoProfileError`，
消息带路径与原因。

### 5. 可观测

`delivery run` 开跑前打印实际使用的 argv 与来源：

```
verify command: python -m pytest tests/unit -q (source: profile config/repos/shing26/moa-gateway.toml)
```

`delivery preflight` 打印所用 `base_branch` 与来源（`profile` / `env` /
`argument`）。只打印配置值本身，不打印任何 token 或密钥——档案本身也禁止
存放凭据。

## 后果

- 正面：同一台机器给多个仓库交付不再需要改环境变量或起多套进程；「用了哪个
  仓库的验证命令」第一次在输出里可见；
- 负面（如实）：档案与仓库名精确匹配，大小写不同的写法需要各写一份；档案
  需要运维自己维护，写错时命令会更早失败（这是有意的）；
- 边界：本切片只搬两个字段，不涉及 `DeliveryPolicy`、状态机、推送/合并闸门、
  飞书与 GitHub 评论通道。

## 未做的部分

- **不搬其他环境变量**：超时、模型、拓扑、token 仍走全局配置与既有凭据链；
- **不读被交付仓库的检出**：档案属于部署方（见决策 1）；
- **不提供包含/继承机制**：一份档案对应一个 `owner/name`，没有 shared 配置；
- **不在档案里放凭据**：token / PAT / 密钥一律不进档案。
