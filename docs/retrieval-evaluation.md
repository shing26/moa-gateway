# 检索质量：gold set、离线度量、精排（ADR-016 / ADR-020）

> 顺序是被决策钉死的：**先有度量，再谈精排**。"无 gold set 调精排是盲调"。
> 本文记录度量怎么建、怎么跑、当前测出来是多少，以及精排为什么默认关着。

## 语料与 gold set

固定语料在 `evals/datasets/retrieval_corpus/`，6 篇本项目自己的文档
（网关总览 / 意图路由 / 守卫与 HITL / 混合检索 / 评测系统 / 审计链），
切成 12 个 chunk。选自己的文档而不是公开数据集，是因为"什么算相关"需要判断，
而这个判断只有熟悉系统的人做得对。

gold set 在 `evals/datasets/retrieval_gold.jsonl`，每行：

```json
{"id": "q002", "query": "请求链路是如何工作的？",
 "relevant_doc_ids": ["01-gateway-overview"],
 "relevant_chunk_ids": ["01-gateway-overview:chunk:0", "01-gateway-overview:chunk:1"],
 "graded": {"01-gateway-overview:chunk:0": 2, "01-gateway-overview:chunk:1": 1},
 "notes": "human spot-checked"}
```

`relevant_chunk_ids` 的 id 与检索时真实写入的 id **同源**——评测用的是
`app.knowledge.chunk_text`，与知识库写入是同一个分块器。这不是巧合，是
"gold set 能被检索到"的前提。

### 标注流程

`scripts/label_retrieval_gold.py`，两阶段：

1. `--mode llm`：由一个**独立模型**（本机 Ollama 上的 `qwen2.5:7b`）读语料后
   生成 query → 候选 doc 的对应关系，落到 `.staging.jsonl`；
2. 人工逐条抽检，确认后 `--finalize` 转成 `retrieval_gold.jsonl`。

标注者与分块器作者不是同一个人（ADR-020 的独立性要求），来源与时间记在
`retrieval_gold.meta.json` 里——这是那条要求**可审计**的证据，不是一句口头承诺。

`--mode heuristic` 是纯规则兜底（关键词共现），未经人工抽检不得 finalize。

## 度量

`evals/run_evals.py` 的 `run_retrieval_eval(cases, retrieve_fn, top_k=5)`：

| 指标 | 含义 |
| --- | --- |
| `hit_at_1` / `hit_at_3` / `hit_at_5` | 前 k 条里是否至少命中一个相关 chunk |
| `recall_at_5` | 相关 chunk 有多少比例出现在前 5 条 |
| `mrr` | 第一个相关结果的名次倒数 |
| `ndcg_at_5` | 考虑分级的相关度增益 |

两个口径值得说明：

* **分母是实际评过的行数**（`evaluated`），不是 `len(cases)`。缺
  `relevant_chunk_ids` 的行无法评分，若仍进分母，一条坏行会把所有指标静默稀释
  成 (n-1)/n，而报告里看不出少评了一条。现在这类行会被数出来并写进 `note`。
* **不判分、不宣称**。函数只出数字，"rerank 是否有用"由调用方读 delta 后自己下
  结论（ADR-015 口径）。数学实现由 `tests/unit/test_retrieval_quality.py` 的
  手算合成用例钉住。

## 当前实测基线

`uv run python evals/run_evals.py --offline`（内存后端 + 确定性 stub embedding
+ 真实分词/RRF，23 条 gold set）：

```text
retrieval Hit@1=0.8261 Hit@3=0.9565 Hit@5=0.9565
          Recall@5=0.8478 MRR=0.8768 nDCG@5=0.8339 (23 条)
```

这是**纯词法腿 + RRF 融合**的数字（离线没有真 embedding，稠密腿为空）。
它不是"检索很好"的证据，只是这条路径当前的水平，作为后续改动的前后对比原点。

### 精排的实测 delta

`uv run python scripts/compare_rerank.py` 在同一 gold set 上跑
base vs `DeterministicLexicalReranker`（纯 Python 覆盖率排序）：

```text
delta: hit_at_1 -0.087, hit_at_3 0.0, hit_at_5 0.0, recall_at_5 0.0,
       mrr -0.0362, ndcg_at_5 -0.0356
```

**六个指标里没有一个是正的，这里如实记下。** 这个结果不奇怪也不失败：lexical reranker 用的
覆盖率信号与词法腿本身高度同源，它重排的是同一批候选，换来的排序改善有限，
而覆盖率排序会牺牲"词频高但只覆盖部分 query"的文档。它证明的是**度量能跑通、
能分辨正负**，不是"精排有用"。

## 精排怎么接

```text
ContextRetriever.retrieve()
  → store.search(top_k = max(top_k*4, 20))     # 取宽候选
  → reranker.rerank(query, docs, top_k)        # 精排（可选）
  → docs[:top_k]                               # 接入点自己截断
```

放在 `ContextRetriever` 层而不是 `VectorStore` 里：`VectorStore` 协议不变、
内存后端行为不变、RRF 融合不受影响，blast radius 最小。截断由接入点负责，
不指望精排器自觉返回 `top_k` 个（有用例钉住）。

三种实现（`app/vectordb/rerank.py`）：

| 实现 | 用途 |
| --- | --- |
| `NoopReranker` | 默认，恒等。未启用时行为零变化 |
| `HttpRerankProvider` | 本地/自托管 OpenAI/Cohere 兼容 `/rerank` 端点，带熔断与降级 |
| `DeterministicLexicalReranker` | 纯 Python 覆盖率排序，供离线前后对比 |

**失败绝不让检索报错**：端点挂掉时保留 RRF 顺序原样返回，连续失败
`max_failures` 次后熔断（本进程内不再发请求）。精排是增强，不是前提。

## 为什么默认关闭

`RERANK_ENABLED=false`。理由有三条，都是实测的：

1. 本地/自托管 rerank 端点本身不免费——多一次推理调用就是多一份延迟与成本，
   而上面的 delta 不支持无条件开启；
2. 没有真的交叉编码器端点可测，`DeterministicLexicalReranker` 只是度量用的尺子，
   不是生产精排器；
3. ADR-016 的触发线是"度量显示召回不是瓶颈、排序才是"。当前 Recall@5=0.8478
   说明**有 chunk 没被召回**——那是召回侧的问题，先动精排是修错了地方。

分块策略升级（markdown/代码感知 + breadcrumb）同样停在触发线上：只有当 gold set
显示瓶颈是**召回**时才重开，届时再用同一把尺子量前后。

## CI 门禁：只保"跑过"，不设质量阈值

`main()` 现在要求 retrieval 维度必须真的跑过（`total > 0`），gold set 或固定语料
被删/改名 → CI 红。**没有设质量下限**，理由很具体：23 条 gold set 上，一行标注
的修正就是 4.3 个百分点，拿单次实测当阈值只会让 CI 在合法增删标注时误红，
最后逼着人为了绿灯去改标注——那比没有门禁更糟。

加入质量下限的条件（满足后再议）：

1. gold set 行数 ≥ 100，且经过**第二位**标注者的一轮独立抽检（一致性可量化）；
2. 语料覆盖到检索路径的主要文档类型，而不是只覆盖网关自身；
3. 连续 ≥ 5 次基线稳定在同一水平，下限取"最低一次再降一个标注粒度"。

## 一条顺带堵住的静默降级

ADR-020 残留：provider 返回错维向量时只 warning 后丢弃。后果是模型换了而
`VECTOR_DB_EMBEDDING_DIM` 没跟着改时，**每一条**向量都进这条分支 → 稠密腿永远
空 → 检索静默退化成纯稀疏，而表现与"没配 embedding"几乎一样。

现在改为 **计数 + error 日志**，并进 `describe()` / `/healthz`
（`embedding_dim_mismatches`）。启动时的表维度校验抓不到这一种（那边比的是
配置 vs 建表，两边都对，错的是模型），所以只能在写入点计数。
