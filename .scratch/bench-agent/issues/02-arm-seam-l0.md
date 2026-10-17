# 02 — arm 缝：arm 注册表 + L0 立起来

**What to build:** 四档必须"实现同一个接口、唯一变量是编排拓扑"，那就先得有那个接口的位置。建一个 arm 注册表，把已有的单 agent 循环注册成 L0，让手动 CLI 按 arm 名取到它并一路跑到 `RunResult` JSON。这一条不改变任何现有行为——它只是把 L1–L3 要插进来的缝先开好，并用一条端到端断言把它钉住。

**Blocked by:** None — can start immediately

**Status:** ready-for-agent

- [ ] L0 经注册表按 arm 名取到，实现 `CodingAgent` 协议
- [ ] CLI 的 `--arm` 走注册表；未知 arm 名报错，而不是静默退回 L0
- [ ] 端到端用例：scripted L0 → `outcome=pass`，落一份完整 `RunResult` JSON
- [ ] 四档共用同一段任务指令、同一套工具、同一个预算与判停条件（L0 先满足）
- [ ] 注册表对"哪些 arm 可用"有单一来源，CLI 与测试读同一处
