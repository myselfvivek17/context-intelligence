# Context-Intelligence

Persistent memory and skills for AI agents over MCP. This is v2; the original Qdrant-based v1 is preserved at
tag [`v1.0`](../../tree/v1.0). One SQLite file, no LLM on the read path, and a ledger
that never serves a stale fact.

## What it does differently

| Problem in today's memory layers | v2 |
|---|---|
| Vector stores return the **old** value after a fact changes (15–40% of the time in published tests) | Bi-temporal ledger: **one active value per (subject, relation)**, enforced by a unique index. A new value closes the old one's validity window. History stays queryable (`as_of`, `include_history`). |
| Supersession schemes break when the same attribute is named differently ("primary db" vs "main database") | The calling agent extracts subject/relation; a calibrated **System One judge** (Jev, or local Laya) maps a new statement onto the existing key. Uncertain cases go to a **review queue** instead of being guessed. |
| Graph memory needs an LLM per write and a graph database | Entities + facts + A-MEM-style links in SQLite; **Personalized PageRank** (HippoRAG-style) as a retrieval channel for multi-hop questions. |
| Single-signal retrieval | Vector (bge-small) + BM25 (FTS5) + graph, **adaptive RRF** (rare-term queries lean on BM25), small importance/recency/usage prior, cross-encoder rerank. |
| Per-turn retrieval churns the prompt and defeats caching | A deterministic **core profile** (dated, current, important facts) for session start, plus a thresholded per-prompt recall. |
| Summaries erase time cues ("is driving" → "drives") | Original wording, temporal form and observation time are kept in separate fields. |

## Results

`python eval/run_eval.py --seed 11 --keys 90` (with `ABLATE=1` for the ablation rows): 90 people/attributes that
change 2–4 times (277 statements over time, phrased differently each time), 40 static facts and 10 two-hop chains.
`current@1` = the top result states the latest value; `stale@1` = the top result states an older value.

| system | current@1 | stale@1 | static recall@5 | two-hop recall@5 | p50 |
|---|---|---|---|---|---|
| v1-style baseline (vector only) | 8% | **90%** | 100% | 100% | 4 ms |
| v2 retrieval, facts stored as unkeyed notes | 4% | 96% | 100% | 60% | 19 ms |
| **v2 keyed ledger** | **99%** | **0%** | 100% | 80% | 21 ms |
| · rerank replaces fused order | 99% | 0% | 100% | 70% | 21 ms |
| · no rerank | 77% | 0% | 100% | 100% | 6 ms |
| · no graph channel | 98% | 0% | 100% | 70% | 21 ms |
| · fixed fusion weights | 99% | 0% | 100% | 80% | 21 ms |

What this shows, and what it doesn't:
- **Better retrieval alone does not fix stale facts** (row 2 is no better than the baseline); the ledger does,
  in every configuration. This matches the MemStrata result.
- The graph channel is what recovers two-hop answers once the reranker is on (70% → 80%). On this small,
  name-distinctive chain set plain vector search is still best at two-hop recall (100%) — v2 trades some of it
  for a far better top answer. The reranker blend weight (`rerank_fused_weight`, 0.5) was tuned on three seeds.
- Adaptive fusion made no measurable difference on this data; it is kept but not claimed as a gain.
- This is the **keyed** condition (the writer supplies subject/relation). Real-world staleness also depends on
  how often agents key their writes; measuring that needs an LLM-writer run, which is not in this harness yet.
- Latency is in-process on a laptop; over the LAN on the i5-8250U server a full search is ~230–310 ms.

## Run

```bash
cp .env.example .env   # set MCP_API_KEY (and TYPESAFE_API_KEY for the judge)
docker compose up -d --build
curl localhost:8084/health
```

MCP endpoint: `http://<host>:8084/mcp` (bearer `MCP_API_KEY`). REST: `POST /v1/memories`, `POST /v1/skills`,
`POST /v1/search`, `POST /v1/recall`, `GET /v1/profile`, `GET /v1/stats`. Optional web console at `/ui`
(`WEB_UI=true`).

Agent instructions: [docs/SYSTEM_PROMPT.md](docs/SYSTEM_PROMPT.md).

## Judge backends
- `jev` (default): TypeSafe's hosted System One model; needs `TYPESAFE_API_KEY`.
- `laya`: open-source local model. Build with `WITH_LAYA=1`. Measured on an i5-8250U: ~1.2 s per decision,
  ~2.5 GB RAM while loaded (it runs in a worker process that exits when idle).
- `none`: still fully functional; only exact subject+relation matches supersede.

## Tests

```bash
python server/test_ledger.py
```
