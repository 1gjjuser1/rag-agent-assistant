# 评测（Eval）

用 golden set 量化 RAG 检索与回答质量，便于在简历/README 中给出可验证的数字。

```bash
# 离线模式：确定性伪向量，无需 API Key，只验证评测链路本身
python evals/run_eval.py --offline

# 在线模式：真实 DashScope Embedding + LLM 问答（需 .env 配置 API Key）
python evals/run_eval.py
```

指标：

| 指标 | 含义 | 模式 |
| --- | --- | --- |
| hit@k | 期望来源是否出现在 Top-k 检索结果中 | 全部 |
| keyword_hit | 回答是否包含预期关键词 | 在线 |
| citation | 回答是否附带引用来源 | 在线 |

结果写入 `evals/last_run_report.json`。

扩展方向：接入 RAGAS（faithfulness / answer relevancy / context precision）做端到端自动评测，并按知识库/文档类型分组统计。
