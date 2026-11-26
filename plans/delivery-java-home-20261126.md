# 契约：交付验证环境保留 JAVA_HOME

- **task id**：`delivery-java-home-20261126`
- **分支**：`fix/delivery-java-home-20261126`
- **背景**：2026-11-26 首次跨仓交付（`shing26/OpsShield`，run
  `dlv-e77f2536a2294258905ea05bbc876483`）在真门禁处失败：

  ```
  [ERROR] Failed to execute goal ...maven-compiler-plugin...:
  Fatal error compiling: 错误: 不支持发行版本 21
  ```

  宿主机 `mvn -version` 是 Java 21（`JAVA_HOME=E:\java\jdk21`），但 `PATH`
  上另有一个 Java 18。交付验证走 `build_verify_env()` →
  `isolated_env()` 的白名单，`_ALLOWED_NAMES` 不含 `JAVA_HOME`，于是子进程
  的 `mvn` 回落到 PATH 上的 Java 18，无法编译 `--release 21` 的仓库。

  这不是 OpsShield 的问题，也不是 `mvn` 配置问题：任何需要 `JAVA_HOME`
  决定工具链的仓库（Java/Maven/Gradle）在跨仓交付时都会撞上。

## 1. 语义

### R1 白名单只加工具链定位变量

`app/agents/subprocess_env.py` 的 `_ALLOWED_NAMES` 增加 `JAVA_HOME`。

- 只加这一个变量，不放开任意环境变量；
- 不改变「未登记即丢弃」的默认；
- 不加 `MAVEN_HOME` / `M2_HOME`：`mvn` 启动脚本只依赖 `JAVA_HOME` 与自身
  安装路径，多放变量会扩大跨边界面而无收益。

### R2 行为不变

- `isolated_env()` 的其它语义不变：仍从 `base`/`os.environ` 过滤、仍把
  `workspace` 放到 `PYTHONPATH` 最前、仍设置 `PYTHONDONTWRITEBYTECODE`。
- `build_env()` / `build_verify_env()` 的签名与调用点不变。
- 未设置 `JAVA_HOME` 时不注入空值（白名单过滤器天然跳过不存在变量）。

### R3 边界

- 不改验证命令、超时、workspace 复制逻辑。
- 不改 OpsShield 档案或任何仓库配置。
- 不新增依赖。

## 2. 允许路径

```
app/agents/subprocess_env.py
tests/unit/test_subprocess_env.py
plans/delivery-java-home-20261126.md
```

## 3. 验收标准

- **T1** `_ALLOWED_NAMES` 含 `JAVA_HOME`；`isolated_env()` 在 `base` 含
  `JAVA_HOME` 时把它原样带进结果。
- **T2** 未登记变量仍被剥离：例如 `AWS_SECRET_ACCESS_KEY`、
  `GITHUB_TOKEN` 不出现在结果里。
- **T3** `JAVA_HOME` 不存在时结果里没有该键（不写空串）。
- **T4** `build_verify_env()` 同样保留 `JAVA_HOME`（它复用 `build_env`）。
- **T5** 既有 `test_subprocess_env.py` 全绿；不删弱化断言；无新增 skip。
- **T6** `ruff check .` 全绿；无新依赖。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; python -m pytest tests/unit/test_subprocess_env.py -q
V2  $env:PYTHONPATH='.'; python -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; python -m ruff check .
```

解释器一律 `D:\HermesData\moa-gateway\.venv\Scripts\python.exe`。

## 5. 禁止

- 放开整个 `os.environ` 或改成 denylist。
- 顺手加 `MAVEN_HOME` / `M2_HOME` / `GRADLE_HOME`（不在本单需求内）。
- 改 OpsShield 仓库或交付档案。
- 新增依赖；改动 Allowed paths 之外的文件。

## 6. 签收记录

（待所有者书面签收）
