# ADR-0020: 稀疏腿独立召回、RRF 融合与维度 fail-fast

日期：2026-09-28
状态：已实施（2026-09-28）
关联：ADR-016（台账：本项属表外新发现立项；精排与换嵌入模型登记回该表）、`db/gateway_schema.sql`

## 背景

审计 v2 给 moa-gateway 的 D4（RAG）判了 ✅，但证据是"定长分块 → pgvector 真检索 → 注入"，
即**最弱形态的 ✅**；同批审计把另一个项目的 `BM25 ∥ 向量 + RRF k=60 + 精排` 称作最强的一处。
复核后确认：本仓的检索层**声称了它没有的东西**。

1. **"混合检索"是过度声明。** `app/vectordb/pgvector_client.py:12,14` 的 docstring 写
   "search gains real hybrid ranking" / "Hybrid ranking"，`README.md:326` 写"切换到混合向量检索"。
   实际：
   - `fuse()`（`:107-108`）= `VECTOR_WEIGHT * vector_score + KEYWORD_WEIGHT * keyword_score_01`
     （权重 `:75-76` 为 0.7 / 0.3），是**加权和**，不是 RRF；
   - 关键词腿 `normalized_keyword_score`（`:400-406`）只在**同一批稠密候选池内**做
     min-max 重排——池子由 `_vector_candidates`（`:419-441`）先按 HNSW 取
     `max(top_k * CANDIDATE_MULTIPLIER, MIN_CANDIDATES)` 条决定；
   - 真正的全文扫描 `_keyword_search`（`:437`）只在**三处**触发：embedding 不可用（`:375`）、
     候选为空（`:379`）、候选数不足 `top_k` 时补齐（`:389`，即表很小）。

   结论：**不存在独立稀疏召回、没有 FTS、没有 RRF、没有精排。** 稀疏腿从未独立跑过，
   所以"词法命中但排在稠密 top-k 之外"的文档**永远召不回**——这正是"精确标识符查不到"
   的机制性原因。

2. **中文不能直接用 PG 默认 FTS。** 默认配置按空白与词典切词，中文没有空白边界，
   结果**静默为空**（不报错、只是查不到）。本仓其实已经有正确的分词器：
   `app/vectordb/keywords.py:44-69` 的 `query_tokens` 已做 CJK bigram 化
   （`cjk_bigrams:34-41`、停用对 `CJK_STOP:19`、ASCII 词与 CJK bigram 分别加权 `:29-30`）——
   这是应当复用的**唯一分词来源**，不能再写第二套。

3. **维度不符是静默失效。** `pgvector_client.py:509-517` 对长度 != `self._dim` 的向量
   只 `logger.warning` 然后**丢弃**（append None）。一旦运行时配置维度与建表维度不一致，
   整库向量被逐条丢弃、检索**静默退化成纯关键词**。`db/gateway_schema.sql:48-49` 的注释
   已经声明"更换 embedding 模型需重建本列与索引"，但**没有任何执法点**。

4. **schema 现状**（`db/gateway_schema.sql`）：表 `gateway_documents`（`:31-38`，
   `embedding vector(1536)`，维度由 `render_schema` 注入）、metadata GIN（`:41-42`）、
   embedding HNSW（`:50-51`）。**没有任何全文/tsvector 列或索引。**

## 决策

1. **新增独立稀疏腿（DB 侧召回）**，并复用 `keywords.py:query_tokens` 作为**唯一分词来源**。
   做法：给 `gateway_documents` 增加一个**预分词列**（写入时用 `query_tokens(content)` 生成
   词元串），其上建 GIN 索引，配置用 `simple`（**不做 stemming**——bigram 被词干化会破坏召回）。
   查询侧用**同一函数**对 query 分词。
   明确否决：PG 默认 FTS 直接切中文（静默空）、`zhparser`（新增扩展，部署成本不划算）、
   ES 当稀疏腿（会引入第二套语料与第二套权限，与"配置单一来源"冲突）。
2. **融合改 RRF（k=60）**，替代 `fuse()` 的加权和。理由：加权和依赖 `pool_best` 归一化基准
   （`:389-396` 已有"归一化基准必须覆盖全部候选"的注释，说明这个基准本身就是脆弱点），
   而 RRF 只用**名次**，免量纲、免调参。
3. **权限过滤必须进两条腿的 SQL WHERE**，位置与现有 `_vector_candidates` 的
   `metadata @> %s::jsonb`（`:426-430`）一致；**禁止**事后用 Python 过滤。
   原因：RRF 融合后的高分文档若在事后被剔除，会把合法的低分文档一并挤掉名额。
   **但必须如实记下**：本仓当前**根本没有租户概念**——全仓 grep `tenant_id` / `acl_groups`
   为 0（`app/knowledge_access.py` 只是一个 DI port），知识库**无任何隔离层**。
   因此本 ADR 只钉住"**一旦引入租户，权限必须进两条腿的 WHERE**"这条约束；
   **租户隔离本身登记进 ADR-016 台账**，不在本轮实现。
4. **维度不符改启动 fail-fast**：`start()` 阶段校验运行时维度与表定义维度，不一致直接拒绝启动，
   取代 `pgvector_client.py:509-517` 的静默丢弃。
5. **嵌入模型本轮不动**：继续 `nomic-embed-text` / 768（本地 `.env` 已在用，
   `VECTOR_DB_EMBEDDING_DIM=768`）。**精排（rerank）与换 `bge-m3`（1024 维，需全库重嵌入 +
   重建 vector 列/索引）登记回 ADR-016 台账**，触发线见该表。
6. **宣称与实现对齐**：在稀疏腿真正落地之前，`pgvector_client.py:12,14` 与 `README.md:326`
   先改成对当前实现的**准确描述**（加权和 + 同池重排 + 降级回退）。不得让文档继续领先于代码。

## 验收

1. **稀疏腿真的独立召回**：构造"词法命中但不在稠密 top-k 内"的用例（精确标识符），
   新实现能召回，旧实现不能——这是本 ADR 的核心证据。
2. **中文查询不空结果**：bigram 腿对中文查询返回非空，且与 `query_tokens` 的分词一致。
3. **RRF 前后可比**：在检索 gold set 上，Hit@1/@3/@5 与 MRR 可离线对比
   （gold set 的构造与离线跑法见 RAG 计划文档；**注意标注者不得与分块器作者同一人**）。
4. **维度不符启动即失败**，不再静默丢弃。
5. HNSW 索引（`:50-51`）与 metadata GIN（`:41-42`）不动；零回归。

## 实施记录（2026-09-28）

落地：`app/vectordb/keywords.py`（`tokenize_for_index` / `tsquery_text`）、
`app/vectordb/pgvector_client.py`（`rrf_scores` 取代 `fuse`、`_sparse_candidates`、两腿 `search`、
`start()` 的维度校验与回填）、`db/gateway_schema.sql`（`tokens` 列 + GIN 索引）、
`tests/unit/test_pgvector_client.py`、`tests/unit/test_knowledge_store.py`。

### 对着真实 PostgreSQL 验过，不只断言 SQL 字符串

本地 `moa-gateway-postgres-1`（pgvector/pg16）上端到端跑通：

- 迁移建出 `tokens` 列与 `gateway_documents_tokens_idx`，索引表达式为
  `gin (to_tsvector('simple'::regconfig, tokens))`；
- 启动回填 **7/7 行**完成（`tokens IS NULL` 归零），中文被切成 bigram
  （`数据库连接池…` → `数据 据库 库连 连接 接池 …`）；
- `search('连接池')` → `emb-doc-1`、`search('pgvector')` → 对应分片、编造词 → 空；
- 分数 `0.016393 = 1/(60+1)`，正是 RRF 名次分。

**实施前先用真库探了两个决定设计成立与否的点**（值得记，否则下次还要重探）：
`to_tsvector('simple', 'redis 连接 接超 超时')` 把每个 bigram 切成**独立词元**，
`@@ to_tsquery('simple', 'redis | 连接')` 能命中——**中文 bigram 在 `simple` 配置下可用**；
并且该表达式**确实能建索引**（两参形式是 IMMUTABLE，单参不行）。

### 决策里没写、但影响正确性的选择

- **已存在的表必须显式 `ALTER TABLE … ADD COLUMN IF NOT EXISTS tokens`。**
  `CREATE TABLE IF NOT EXISTS` 对**已建好的表不会补列**——漏了这一句，老库升级后 `tokens`
  永远是 NULL、稀疏腿永远空，而且是**静默**空。这是决策 1 漏掉的一步。
- **tsquery 用 `|`（或）而非 AND**：bigram 做 AND 太严，中文长查询几乎不可能让全部 bigram
  出现，召回会塌。排名交给 `ts_rank`。
- **维度 fail-fast 放在"启动不炸"的 `except` 之外**，故意让它逃出去——放进去会被兜底吞成
  "降级"，fail-fast 就失效了。
- **回填分批（5000/次）+ best-effort**：失败只让稀疏腿不完整，不该拖垮启动；余量写进日志。
- **第三条腿保留**：稠密候选不足 `top_k` 时补一次有界词法扫描，覆盖"无向量**且**未回填 tokens"
  的行。区别是它现在**作为第三条腿参与 RRF**，不再自己算一套加权分（那正是量纲要小心处理之处）。
- `VECTOR_WEIGHT` / `KEYWORD_WEIGHT` / `fuse` **删除**，`__all__` 换成 `rrf_scores`。

### 决策 6（宣称对齐）的结论

README 的"混合向量检索"此前是过度声明，而本次实现**让它变成真的**，所以 README **不需要改**；
要改的是模块 docstring（已重写为"两条腿 + RRF"），并把原先"八处 B608 抑制标记"的硬编码计数
改成不带数字的表述——本轮新增 3 处站点，硬编码数字是维护陷阱。

### 验收实测

- 单测：本 ADR 落地时 **844 passed**（新增 RRF 与两腿用例、改写旧的加权和断言）。
  `test_knowledge_store.py` 的 `[postgres]` 参数是**复刻 SQL 语义的内存假后端**（不是真连库），
  加列后必须同步改它——本次有 14 条用例在此红过。
- 真库：迁移 / 回填 / 检索 / 分数四项见上。
- 维度 fail-fast：单测覆盖；真库上配置 768 与建表 768 一致，正常启动。

### 残留（如实记）

- **决策 4 只堵住了"配置 vs 建表"的维度不一致；provider 返回错维向量是另一扇门。**
  `_embed_batch` 对每条不符的向量仍只打 **warning** 然后丢弃 → 稠密腿永远空 → 检索静默退化
  成纯稀疏。启动校验抓不到它（配置与表都是 768，错的是模型），表现与"没配 embedding"几乎
  一样，只有日志里一条 warning。**这是一处仍未堵的静默降级**，最省的修法是把该 warning
  升为 error 并计数，或首次不符即 fail-fast。
- **验收 3（gold set 上的 Hit@k / MRR 前后对比）未做**：`evals/datasets/retrieval_gold.jsonl`
  还不存在，标注者也不该与分块器作者同一人。所以"RRF 更准"这句**目前没有数字支撑**，
  只有"稀疏腿确实独立召回"这一条结构性证据（真库上的词命中 + 单测里的两腿融合与排序）。
  按 ADR-015 的口径：**不许宣称更准。**

## 后果

- 新增一列 + 一个 GIN 索引 + 写入侧分词。复用 `query_tokens` 是刻意的：**避免第二套分词
  随时间与 `keywords.py` 漂移**（`keywords.py` 开头的注释正是在讲这个危险）。
- 融合从加权和改 RRF 后，`VectorDocument.score` 的**语义与量纲都变了**
  （从 0..1 融合分变成 RRF 名次分）。任何依赖 `score` 绝对值的调用点必须一并审——
  这是本 ADR 最容易被漏的回归面。
- RRF 落地后**不得**顺势宣称"混合检索更准"：精度提升必须有 gold set 数字支撑，
  否则只是把一处过度声明换成另一处（ADR-015 精神）。
- 换嵌入模型的那一天，决策 4 的 fail-fast 是唯一的安全网；在此之前它只是防止静默退化。
