# moa-gateway 产品体验评估报告

- 被测产品：`D:\HermesData\moa-gateway`（FastAPI Agent 网关：智能路由省钱 + 安全守卫/HITL + 评估体系 + GitHub PR 审查垂直应用）
- 体验方式：隔离实例真实操作（Playwright 驱动浏览器 + 直接 HTTP 请求 + 代码级根因定位）
- 体验时间：2026-08-16
- 报告结论：**当前默认配置下网关连一个 LLM 请求都发不出去（阻塞级），且多个核心功能与 README 宣称不符（装饰性 FSM、未接线的三级意图路由、PR 审查链路 500 等）。** 建议在修复 LLM Provider 配置问题并恢复 OmniRoute 之前，任何"演示可用"的对外宣称都不成立。

---

## 一、体验概览

我以真实用户身份在**隔离测试实例**（独立端口 8123、独立数据目录、飞书发送禁飞）上完整体验了管理后台 7 个页面、飞书/GitHub Webhook、消息流水线、知识库、会话、限流、安全合规、运维配置等功能，共发现 **14 个问题**（阻塞 1 / 高 3 / 中 4 / 低 6，另有性能观察若干），其中：

- **最严重**：LLM 调用层存在硬伤（`_build_kwargs` 未给裸模型名补 provider 前缀、未传 `custom_llm_provider`），当前环境配置下任何依赖 LLM 的流程（智能路由、审查、HITL 完整链路、工具循环、记忆对话）都必然失败。这是"整个产品核心不可用"的根因。
- **文档与实现矛盾多处**：README 宣称"三级意图路由（正则→微模型→路由 LLM）""无 GITHUB_TOKEN 时优雅降级""execute_code 高危操作强制 REVIEW"，实际微模型/路由 LLM 未接线、GitHub review webhook 无 token 直接 500、execute_code 的 handler 是占位桩。
- **FSM 是装饰性的**：状态每次从 INIT 开始、不持久化、pipeline 不消费状态，"cancel/reset" 等控制消息被当成普通消息处理。

---

## 二、测试环境与方法

| 项 | 说明 |
|---|---|
| 测试实例 | `http://127.0.0.1:8123`，`uvicorn app.main:app --app-dir D:/HermesData/moa-gateway` |
| 隔离措施 | 独立端口；FEISHU_APP_ID / FEISHU_APP_SECRET / VERIFICATION_TOKEN 置空（禁飞书发送副作用）；Redis 未运行 → 走内存回退；全部使用测试数据，未触碰真实数据 |
| 工具 | Playwright（系统 python，E:\anaconda）驱动 Chrome 实测 + `curl`/requests 直连 API + 源码静态分析定位根因 |
| 证据 | 服务端审计日志 `logs/audit-2026-08-16.jsonl`、服务日志、18 张截图（见附录 A）、DOM/API 分析 |
| 环境限制 | 机器上无可用 LLM key：`OPENAI_API_KEY` 是 deepseek 的 key，但 `LLM_PROVIDER=omniroute`、`LLM_BASE_URL`/`CODE_REVIEW_BASE_URL` 指向 `localhost:20128`，而 OmniRoute 并未运行（20128/20129/11434 端口全关闭）。因此**所有依赖 LLM 的功能只能做代码级分析，无法端到端实测**，报告中对这些项明确标注"未体验（环境不可用）"。 |

> 注：`LLM_MODEL=high-availability`、`LLM_BASE_URL=localhost:20128`、`OPENAI_API_KEY=<deepseek key>` 是用户当前 `.env` 的演示配置；即便 OmniRoute 起来了，`OPENAI_API_KEY` 也是 deepseek 的 key，对 omniroute 无效。这属于用户环境问题，但**代码层的 Provider 前缀缺失是产品自身的硬伤**，两条叠加导致 LLM 完全不可用。

---

## 三、全量功能覆盖清单

覆盖口径：界面（所有页面/菜单/按钮/链接/路由/设置项/列表操作/搜索/筛选/分页/导入导出）+ 代码（路由/接口）。每项至少走一遍；`未体验` 均注明原因。

### 3.1 管理后台 UI（7 个页面）

| 页面 | 功能点 | 状态 | 备注 |
|---|---|---|---|
| 概览 | 系统状态/健康、Redis 状态（内存回退）、依赖检查（redis/fallback_memory）、运行配置（模型/API 地址/飞书卡片）、快捷入口、最近请求 | ✅ 已体验 | 空态正常；5s 轮询 /healthz，Redis 挂时每次新建连接反复打印失败（见性能观察 P1） |
| 知识库 | 上传文档（拖拽/选择文件）、列表、检索、删除、详情、空文件提示、未知文档 404 | ✅ 已体验 | 正常；详情页删除 toast 因跳转丢失（问题 L3）；404 详情页见 `13_unknown_doc.png` |
| 会话 | 会话列表、模式（常规/审查等）切换 | ✅ 已体验 | 模式 API 正常；会话内容因 LLM 不可用无真实对话 |
| 测试台 | 发送测试请求（hello / 自定义消息）、429 展示 | ✅ 已体验 | hello 请求 20s 不返回（性能观察 P2）；429 UI 展示正常（`14_test_429.png`） |
| 请求日志 | 审计列表、状态/会话过滤、展开详情 | ✅ 已体验 | 正常 |
| 安全合规 | 隐私数据擦除（privacy erase） | ✅ 已体验 | 正常 |
| 运维 | Provider 配置（模型/API 地址/API Key）、测试连接、Feature Flags（切换/还原）、Obsidian 同步、运行状态、OTel 端点、全局限流 | ✅ 已体验 | 保存空 API Key 清空运行时 Key（问题 H1）；Flag 接受非布尔值 50（问题 L5）；Obsidian 未启用提示"同步完成 0 篇"（问题 L4）；运行状态圆点全 neutral（问题 L6） |

### 3.2 路由与中间件

| 功能点 | 状态 | 备注 |
|---|---|---|
| `/webhook/feishu` url_verification challenge | ✅ 已体验 | fail-open 正常（FEISHU 未配置也回 challenge） |
| 飞书事件去重 | ✅ 已体验 | 重复事件只处理一次 |
| 飞书 schema2.0 消息事件 → pipeline | ✅ 已体验 | 返回 msg ok |
| 畸形 JSON body | ✅ 已体验 | 返回 500 并泄露内部错误（问题 M3） |
| `/webhook/callback`（HITL 卡片回调） | ✅ 部分 | 未知 hitl → 404 正常；approve/reject 完整链路因 FEISHU 未配置 + LLM 不可用未体验 |
| `/webhook/github`（PR 审查） | ✅ 已复现异常 | 无 token → 500 `pipeline_init_failed`（问题 H3）；PR 消息 → 500 KeyError（问题 H2） |
| 限流 10 次/60s/会话 | ✅ 已体验 | 25 连发 = 10×200 + 15×429，429 带 `message` 文案 |
| 鉴权中间件（WEBHOOK_AUTH_TOKEN / DASHBOARD_PASSWORD） | ✅ 部分 | 未配置时无鉴权直通；带密码场景未实测（避免改动用户配置）；侧边栏"无鉴权"文案硬编码（问题 L1） |
| 请求日志中间件 | ✅ 已体验 | 每次请求落 audit jsonl |
| Feature Flag 中间件 | ✅ 已体验 | flag 注入 request.state 正常 |
| `/docs`、`/openapi.json` | ✅ 已体验 | 正常 |

### 3.3 消息处理流水线

| 功能点 | 状态 | 备注 |
|---|---|---|
| 指令：`/help`、`/coding`、`/foo` | ✅ 已体验 | 正常返回指令响应 |
| 意图路由-正则层 | ✅ 已体验 | regex 命中正常 |
| 意图路由-微模型 / 路由 LLM 层 | ❌ 未体验 | 代码级：`IntentRouter()` 无参构造，两个 LLM 均为 None，从未接线（问题 M2） |
| FSM 状态机 | ✅ 已复现异常 | 每次从 INIT 开始不持久化；pipeline 不消费状态；`cancel/reset` 不重置（实测 500 agent_failed）；`debug` 消息 SUSPENDED 但流程照常执行（问题 M1） |
| 工具循环（≤3 轮） | ❌ 未体验 | LLM 不可用；代码级：execute_code 是占位桩（问题 M2） |
| 记忆/多轮对话 | ❌ 未体验 | LLM 不可用 |
| 智能路由省钱（Provider fallback / 成本核算） | ❌ 未体验 | LLM Provider 错误（问题 B1） |

### 3.4 安全守卫 / HITL / 评估体系

| 功能点 | 状态 | 备注 |
|---|---|---|
| 静态守卫（guard）敏感词/正则 | ✅ 部分 | 可触发 SENSITIVE_DETECTED 事件，但"挂起"是装饰性的（问题 M1） |
| HITL 审批（飞书卡片闭环） | ❌ 未体验 | FEISHU 未配置 + LLM 不可用；仅验证了 callback 未知 id → 404 |
| Eval 评估（evaluator flags、canary、skip_ast、guard.enabled/guard.hitl） | ✅ 部分 | flags 开关 API 设置/还原正常（含非布尔值 50，问题 L5）；评估真实执行依赖 LLM 未体验 |
| canary prompt 版本切换 | ✅ 已复现异常 | agent=review 时 KeyError（问题 H2） |

### 3.5 知识库 / 会话 / 隐私

| 功能点 | 状态 | 备注 |
|---|---|---|
| 上传 md（正常 / 空文件 400 / 类型限制） | ✅ 已体验 | 空文件 400 提示"请求失败: 400"不友好（问题 M4） |
| 检索、删除、详情（正常 / 404） | ✅ 已体验 | 正常 |
| 会话模式 API | ✅ 已体验 | 正常 |
| 隐私擦除（privacy erase） | ✅ 已体验 | 正常 |

### 3.6 GitHub PR 审查垂直应用（apps/code_review_pipeline）

| 功能点 | 状态 | 备注 |
|---|---|---|
| review_agent（聊天触发 PR 审查） | ✅ 已复现异常 | `octocat/Hello-World#1` → 500 KeyError（问题 H2）；无 token 时理论上有降级文案但被上游 500 抢先 |
| GitHub review webhook 路由 | ✅ 已复现异常 | 无 token → 500 `pipeline_init_failed`（问题 H3） |
| 历史 PR 摄取（RAG）、静态分析、semantic review、test coverage、triage 等子 agent | ❌ 未体验 | 依赖 GitHub 网络 + LLM，环境不可用 |
| 审查结果存储 / 飞书通知 | ❌ 未体验 | 同上 |

### 3.7 兼容性与常规体验维度

| 维度 | 结果 |
|---|---|
| 移动视口（375px） | ✅ 无横向溢出，页面可用 |
| 浏览器返回 / 前进 / 刷新 | ✅ 状态一致 |
| 空态 / 加载态 / 错误态 | ✅ 7 个页面均见到 |
| 加载性能（LLM 请求） | ⚠️ 20s 无响应（性能观察 P2） |

---

## 四、问题清单（按严重度排序）

> 严重级别定义：阻塞 = 主流程不可用、数据损坏/丢失或崩溃；高 = 核心功能错误但可绕过；中 = 非核心功能错误、明显体验问题；低 = 细节瑕疵。

### 阻塞级

#### B1. LLM 调用完全不可用——裸模型名未带 Provider 前缀，litellm 必报 "LLM Provider NOT provided"

- **现象**：任何 LLM 请求（智能路由、审查、HITL、工具循环、记忆对话）100% 失败。实测 Ops 页"发送测试消息"、webhook 消息、`/dashboard/api/ops/test` 均返回 `litellm.BadRequestError: LLM Provider NOT provided. ... You passed model=deepseek-v4-flash / high-availability`。
- **复现步骤**：① 启动网关；② 任意 LLM 依赖请求（如 Ops 测试台点"ping"）；③ 服务日志出现上述 litellm 错误。
- **实际 vs 预期**：实际——所有 LLM 调用失败；预期——按 `LLM_PROVIDER`/模型名调用对应 provider。
- **根因**：`app/agents/provider.py` `_build_kwargs()` 只把 `model` 原样传入 + `api_key`/`api_base`，既不给裸模型名加 `openai/`、`deepseek/` 等前缀，也不传 `custom_llm_provider`。litellm 无 provider 可推断时必然抛错。`LLM_PROVIDER=omniroute` 环境变量在代码中无任何消费点（`provider.py` 只读 `*_API_KEY` / `*_BASE_URL`）。
- **叠加环境问题**：`LLM_MODEL=high-availability`、`LLM_BASE_URL/CODE_REVIEW_BASE_URL=localhost:20128`，OmniRoute 未运行；`OPENAI_API_KEY` 是 deepseek key，对 omniroute 无效。已用最小复现验证：A. 裸名（`deepseek-v4-flash`）→ 必现 Provider 错误；B. `openai/` 前缀可越过该错误走到鉴权阶段。**当前配置下网关一个 LLM 请求都发不出。**
- **证据**：服务日志 `litellm request failed model=...: LLM Provider NOT provided`（`/dashboard/api/ops/test` 连打 3 次全失败）；源码 `provider.py:197-230`。

### 高级

#### H1. Ops 页保存空 API Key 会把运行时 Key 清空，与 placeholder "留空则保持当前 Key" 自相矛盾

- **现象**：运维页 API Key 输入框 placeholder 写"留空则保持当前 Key"，但用户把输入框清空后点"保存配置"，页面从"当前已配置 API Key"变成"当前未配置 API Key"——Key 被真正清空了。
- **复现步骤**：① Ops 页（此时已配置 Key，显示"当前已配置 API Key"）；② 清空 API Key 输入框；③ 点"保存配置"；④ 状态变"当前未配置 API Key"。实测后手动恢复成功（不影响后续测试）。
- **实际 vs 预期**：实际——空字符串覆盖环境变量为空；预期——留空应保持当前 Key（placeholder 承诺）。
- **根因**：前端 `app/static/dashboard.js` `onOpsSave()` 发送 `api_key: document.getElementById('ops-api-key').value.trim()`（空串），而后端 `app/routes/dashboard.py:847-854` `if body.api_key is not None: os.environ["OPENAI_API_KEY"] = body.api_key.strip()` 对空串照单全收。对比同文件 `onOpsTest()` 用的是 `value.trim() || null`（空则 null，后端 `body.api_key or env` 会回退）——保存和测试两条路径的"留空语义"不一致。
- **证据**：UI 复现（初始"已配置"→保存空→"未配置"→恢复成功）；截图 `02_ops.png`/`10_ops.png` 显示 placeholder 与状态文案。

#### H2. PR 审查链路 500：发 "octocat/Hello-World#1" 消息直接 500 KeyError

- **现象**：向 /webhook/feishu 发一条 PR 格式消息（如 `octocat/Hello-World#1`），返回 500，服务日志 `unhandled exception: KeyError: no prompt found for agent=review version=stable`。核心"GitHub PR 审查"功能完全不可用。
- **复现步骤**：① 启动网关；② `POST /webhook/feishu` body 含 `text: "octocat/Hello-World#1"`；③ 500。
- **实际 vs 预期**：实际——500；预期——进入 review agent 执行 PR 审查（README 宣称的垂直应用）。
- **根因**：`app/deps.py` `init_prompts()` 只注册 `coder`/`general` 两个 agent 的 prompt；`app/pipeline.py:146` 对每个意图无条件调 `select_canary_version()`，`app/prompt_registry/__init__.py` `get_or_default()` 对未知 agent 抛 `KeyError`，且该调用在 `try/except agent.execute` 之外，无兜底。`app/prompt_registry/canary.py` 也没有 fallback。
- **证据**：日志 500 + `KeyError: no prompt found for agent=review version=stable`；源码 `deps.py:90-104`、`pipeline.py:146-154`、`prompt_registry/__init__.py:55-66`。

#### H3. GitHub review webhook 无 token 返回 500，README 宣称"优雅降级文案，不会崩溃"

- **现象**：`apps/code_review_pipeline/routing/github_review_route.py:40` 在无 `GITHUB_TOKEN` 时返回 `500 {"error": "pipeline_init_failed", "detail": "GITHUB_TOKEN is required for Code Review Pipeline"}`；README 第 82 行却写"无 GITHUB_TOKEN 时返回优雅降级文案，不会崩溃"。
- **复现步骤**：① 不设置 GITHUB_TOKEN；② 调 GitHub review webhook（构造合法 payload）；③ 500。
- **实际 vs 预期**：实际——500 + 内部错误串；预期——优雅降级文案（README）。
- **根因**：`github_client.py:50-52` 无 token 抛 `RuntimeError`，`github_review_route.py:40` 把异常映射为 500。聊天路径 `app/agents/review_agent.py:22-23` 确实有降级文案，但该文案被 H2 的 KeyError 500 抢先，且 webhook 路由路径完全没有降级。
- **证据**：源码 `github_review_route.py:40`、`github_client.py:50-52`、README.md:82。

### 中级

#### M1. FSM 状态机是装饰性的：状态不持久化，pipeline 从不消费；"cancel/reset" 不重置、"debug" 不挂起

- **现象**：
  - 发 `cancel` / `reset` / `取消` 消息，本应重置会话，实际被当成普通消息处理，进入 LLM 流程报 500 `agent_failed`（当前 LLM 不可用场景下）；
  - 发 `debug`（敏感词触发 SENSITIVE_DETECTED），FSM 返回 SUSPENDED，但流程照常执行，无任何挂起/拦截动作。
- **复现步骤**：① `POST /webhook/feishu` text=`cancel` → 500 agent_failed（不是重置成功响应）；② text=`debug 错误` → 返回正常 ok 结果，而不是挂起/需审批。
- **实际 vs 预期**：实际——FSM 状态仅是日志里的装饰；预期——cancel/reset 重置会话状态、debug 触发挂起等待人工。
- **根因**：`app/engine.py:232-233` `handle_event()` 每次 `StateContext(state=State.INIT, ...)` 从零开始，状态不跨事件持久化；`app/pipeline.py` 从不检查 `handle_event` 返回的状态（`state_stack` 只是写回 metadata）；`cancel/reset` 映射的 `CANCEL/RESET` 事件在 `TRANSITIONS` 中只对特定状态有转移，且没有任何"重置会话"的业务实现。
- **证据**：实测 500（日志 `agent_failed`）+ 代码 `engine.py:232-257`、`fsm/state_machine.py:24-70`、`pipeline.py`（未消费状态）。

#### M2. 三级意图路由只有正则层：微模型/路由 LLM 从未接线；README 宣称与实际不符；execute_code 是占位桩

- **现象**：
  - README 宣称"三级降级意图路由：正则 → 微模型 → 路由 LLM，只有低置信度才升级"，实际 `app/deps.py:36` `router = IntentRouter()` 无参构造，`router_llm`/`micro_llm` 均为 None，`_micro_llm_fallback`/`_router_llm_fallback` 直接返回默认值。任何非正则命中的消息（如"今天天气怎么样"）intent 恒为 `assistant` → general agent，**微模型分流/路由省钱完全不生效**。
  - README 宣称"高危操作（如 execute_code）强制 REVIEW，飞书审批卡片闭环"，实际 `app/agents/tools.py:56-60` `_execute_code_handler` 是占位桩：只打印 warning 并返回 `EXECUTION_REQUIRES_APPROVAL: ...` 字符串，**不执行任何代码，也没有审批链路**。
- **复现步骤**：① 发一条非正则消息（如"今天天气怎么样"）→ intent 落 assistant（可从响应 `intent` 字段或审计日志确认）；② 源码确认 IntentRouter 无参构造。
- **实际 vs 预期**：实际——只有正则层；预期——三级降级。
- **根因**：`app/router/intent_router.py` 支持注入 LLM 但 `deps.py` 未注入；`tools.py` execute_code 的 handler 是桩。
- **证据**：源码 `deps.py:36`、`intent_router.py:36-79`、`tools.py:56-60`；README.md:22,43。

#### M3. 畸形 JSON 返回 500 而非 400，且全局异常处理器泄露内部错误详情

- **现象**：`POST /webhook/feishu` 发送畸形 body（如 `{'a'}` 单引号 JSON）返回 500，响应体为 `{"error": "JSONDecodeError", "detail": "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"}`；任何未捕获异常（如 H2 的 KeyError）也会把异常类名+前 500 字详情返回给客户端。
- **复现步骤**：① `curl -X POST http://127.0.0.1:8123/webhook/feishu -d "{'a'}" -H "Content-Type: application/json"`；② 500 + 内部错误明文。
- **实际 vs 预期**：实际——500 并泄露内部信息；预期——客户端错误应 400/422，且不泄露堆栈级细节。
- **根因**：`app/main.py:63-66` `@app.exception_handler(Exception)` 把所有异常映射为 500 且 `detail: str(exc)[:500]`。
- **证据**：实测响应体 + 服务日志 JSONDecodeError 堆栈；源码 `main.py:63-66`。

#### M4. 前端错误提示丢失服务端详情，一律"请求失败: 400"

- **现象**：空文件上传、未知文档 404、Flag 设置失败等场景，UI 只显示 `请求失败: 400`（或对应状态码），服务端返回的 `detail`/`error` 全部丢失，用户无法知道错在哪。
- **复现步骤**：① 知识库上传空文件 → toast "请求失败: 400"；② 打开未知文档详情 → "请求失败: 404"。
- **实际 vs 预期**：实际——只有状态码；预期——应展示服务端错误详情（如"文件为空"）。
- **根因**：`app/static/dashboard.js:21` `fetchJSON` 抛 `throw new Error('请求失败: ' + res.status)`，不解析响应体。
- **证据**：截图 `13_unknown_doc.png`；源码 `dashboard.js:21`。

### 低级

#### L1. 侧边栏硬编码"本地模式 · 无鉴权 · 数据仅本机可见"，配置了 DASHBOARD_PASSWORD 也照显示

- **现象**：所有页面侧边栏固定显示"本地模式，无鉴权，数据仅本机可见"；配置了 DASHBOARD_PASSWORD / WEBHOOK_AUTH_TOKEN 后该文案不变，误导用户以为没有鉴权。
- **根因**：文案在模板/前端硬编码，未读 `DASHBOARD_PASSWORD` 环境变量。
- **证据**：截图 `01_overview.png` 等；代码检索未发现动态读取。

#### L2. 详情页删除文档的 toast 因页面跳转丢失

- **现象**：在知识库详情页删除文档，页面立即跳回列表，成功 toast 从未展示，用户看不到"已删除"确认。
- **根因**：详情页删除后跳转路由，toast 渲染在旧页面销毁后。
- **证据**：UI 实测（删除成功但无任何反馈）。

#### L3. Obsidian 未启用时点"立即同步"仍提示"同步完成：0 篇变更"

- **现象**：运维页 Obsidian 显示"未启用"，但点"立即同步"仍提示"同步完成：0 篇变更"，制造"已同步"假象。
- **根因**：同步接口在未启用时返回 0 篇的成功响应，前端照单全收；应提示"Obsidian 未启用"。
- **证据**：UI 实测（截图 `10_ops.png` 显示 Obsidian 未启用）。

#### L4. Feature Flag 值未做类型校验：布尔开关可被设为非布尔值（实测 50）

- **现象**：Ops 页 Feature Flags 的布尔开关（如 `canary.enabled`）可通过 API 设为 `50`，后端原样存储字符串，开关语义被破坏。
- **复现步骤**：① `POST /dashboard/api/ops/flags/canary.enabled` value=50；② 返回成功，后续读取为 50。
- **根因**：`app/feature_flags/__init__.py` `set()` 直接 `str(value)` 存储，`_parse_value` 无布尔类型约束；前端 toggle 对非布尔值无校验。
- **证据**：API 实测（设置/还原正常，但接受 50）。

#### L5. guard_service 每次请求打 warning 调试日志；Ops 页运行状态圆点全部 status-neutral

- **现象**：每次请求服务日志刷一行 `logging.warning`（guard_service.evaluate 中的调试行），正常请求噪音大；Ops 页"运行状态"区的圆点全部是灰色 neutral，无任何成功/失败状态感。
- **根因**：`app/agents/guard_service.py` evaluate 内残留 `logger.warning` 调试行；前端运行状态未按真实值渲染状态色。
- **证据**：服务日志 + 截图 `02_ops.png`/`10_ops.png`。

### 性能与稳定性观察（非缺陷定性，但影响体验）

- **P1**：概览页 5s 轮询 `/healthz`，每次新建 Redis 连接；Redis 未运行时反复打印连接失败，且轮询无退避。
- **P2**：测试台发 hello 请求 20s+ 无返回（litellm 初始化/连接超时路径），UI 无超时反馈，用户会以为卡死。
- **P3**：`/dashboard/api/ops/test` 每次请求都在 litellm 层串行尝试主模型 + fallback，失败前无进度提示。

---

## 五、体验维度小结

| 维度 | 结论 |
|---|---|
| 功能正确性 | 非 LLM 功能（知识库、日志、限流、指令、flag API、privacy erase）基本正确；LLM 依赖功能全线不可用；FSM/意图路由/工具等核心编排是"声明式外壳" |
| 流程逻辑自洽 | 多处文档与实现矛盾（三级路由、execute_code 强制 REVIEW、GITHUB_TOKEN 优雅降级）；"留空保持 Key" 与保存行为矛盾 |
| 状态一致性 | 刷新/返回/前进正常；FSM 状态不持久化是最大状态不一致来源 |
| 错误提示 | 后端 500 泄露内部错误、前端吞掉详情只给状态码，双向都不友好 |
| 边界与异常输入 | 畸形 JSON、空文件、超长文本、非布尔 flag 值均已覆盖；畸形 JSON 处理错误 |
| 性能 | 无 LLM 时 hello 20s 无反馈；Redis 不可用时轮询噪音 |
| 兼容性 | 移动视口无横向溢出；Chrome 实测；其他浏览器未测 |

---

## 六、未覆盖项与原因

| 未覆盖项 | 原因 |
|---|---|
| 智能路由省钱（Provider fallback、成本核算） | LLM 不可用（B1） |
| 微模型/路由 LLM 意图分流 | 代码未接线（M2），且 LLM 不可用 |
| HITL 审批卡片完整闭环（飞书发送、approve/reject 后真实执行） | FEISHU 未配置 + LLM 不可用；已验 callback 404 分支 |
| 工具循环（knowledge_search / execute_code 被 LLM 调用） | LLM 不可用；execute_code 本身是桩 |
| 记忆/多轮对话 | LLM 不可用 |
| 评估体系真实执行（evaluator 跑 Eval、canary 实际切换 prompt） | LLM 不可用；canary 对 review 已复现 500 |
| GitHub PR 审查完整链路（真实 PR 拉取、RAG、静态分析等子 agent） | 无 GITHUB_TOKEN + LLM 不可用 + 上游 500 |
| 带 DASHBOARD_PASSWORD / WEBHOOK_AUTH_TOKEN 的鉴权实测 | 避免改动用户配置；仅验证无鉴权默认路径 |
| 其他浏览器（Firefox/Edge） | 时间与限流考虑，仅 Chrome + 移动视口 |

---

## 附录 A：截图索引（`ui-shots/`）

| 截图 | 内容 |
|---|---|
| `01_overview.png` | 概览页（含侧边栏"本地模式，无鉴权"硬编码文案） |
| `02_knowledge.png` / `03_knowledge.png` | 知识库页 |
| `02_logs.png` / `08_logs.png` | 请求日志页 |
| `02_ops.png` / `10_ops.png` | 运维页（API Key placeholder、Feature Flags、Obsidian、运行状态圆点） |
| `02_security.png` / `09_security.png` | 安全合规页 |
| `02_sessions.png` / `06_sessions.png` | 会话页 |
| `02_test.png` / `05_test_hello.png` / `14_test_429.png` | 测试台（hello、429 展示） |
| `04_knowledge_detail.png` | 知识库详情 |
| `11_mobile_overview.png` / `12_mobile_ops.png` | 移动视口 |
| `13_unknown_doc.png` | 未知文档 404 页 |

## 附录 B：关键复现请求

```bash
# B1 复现：Ops 测试连接（任意 LLM 请求）
curl -s http://127.0.0.1:8123/dashboard/api/ops/test \
  -H "Content-Type: application/json" -d '{"message":"ping"}'   # → ok:false, error 含 LLM Provider NOT provided

# M3 复现：畸形 JSON
curl -s -X POST http://127.0.0.1:8123/webhook/feishu \
  -H "Content-Type: application/json" -d "{'a'}"               # → 500 JSONDecodeError + detail 明文

# H2 复现：PR 消息
curl -s -X POST http://127.0.0.1:8123/webhook/feishu \
  -H "Content-Type: application/json" \
  -d '{"header":{"event_type":"message","event_id":"x"},"event":{"message_id":"m1","message_type":"text","content":"{\"text\":\"octocat/Hello-World#1\"}"},"session_id":"s1","user_id":"u1"}'  # → 500 KeyError: no prompt found for agent=review
```

## 附录 C：修复建议（供参考，未改动代码）

1. **B1（最高优先）**：`provider.py` `_build_kwargs` 中对裸模型名按 `LLM_PROVIDER`（或模型名规则）补前缀 / 传 `custom_llm_provider`；同步修正 `.env`（OmniRoute 未运行、deepseek key 不匹配）或改用可用的 provider。
2. **H1**：前端保存时对空 api_key 发 `null`，与后端 `is not None` 语义对齐（参考 onOpsTest）。
3. **H2**：`init_prompts()` 注册 review agent 的 prompt，或 `select_canary_version` 对未知 agent 兜底为 general，并把它放进异常保护。
4. **H3**：github_review_route 无 token 时返回 2xx + 降级文案，与 README 对齐。
5. **M1**：把会话状态持久化到 session_store 并在 pipeline 消费，或明确移除 FSM 声明。
6. **M2**：要么接线微模型/路由 LLM，要么把 README 改成"仅正则层"；execute_code 要么实现审批执行，要么从工具列表移除。
7. **M3/M4**：客户端错误映射 4xx；异常处理器只返回通用信息；前端 fetchJSON 解析服务端 detail。

---

*报告由产品体验 Agent 产出；所有结论均有实测复现或源码证据支撑，标注"待确认"的仅 L5 的运行状态圆点部分（UI 语义推断，非缺陷定性）。*
