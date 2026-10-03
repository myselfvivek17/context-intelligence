# Context-Intelligence v2

Persistent memory and skills for AI agents over MCP. One SQLite file, no LLM on the read path, and a ledger
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
