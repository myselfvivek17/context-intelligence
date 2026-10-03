"""Runtime settings: code default < environment variable < `settings` table (editable live from the web console)."""
import json
import os

import db

# key: (default, description). Env var name is the key upper-cased.
DEFAULTS: dict[str, tuple[object, str]] = {
    "judge_backend":        ("jev", "jev | laya | none — who makes entity/key/conflict decisions (laya needs ~2.5 GB RAM)"),
    "judge_idle_exit_s":    (600, "Laya worker exits after this many idle seconds (frees its RAM)"),
    "supersede_auto":       (0.8, "Judge confidence at/above which a keyed update supersedes automatically"),
    "supersede_review":     (0.5, "Judge confidence at/above which an update is queued for review"),
    "conflict_threshold":   (0.8, "Free-text contradiction probability that hides the old fact pending review"),
    "entity_link":          (0.8, "Judge confidence to link a mention to an existing entity"),
    "rrf_k":                (60, "Reciprocal-rank-fusion constant"),
    "adaptive_fusion":      (True, "Weight BM25 vs vector per query by the query's term rarity (IDF)"),
    "w_graph":              (0.7, "Weight of the PPR graph channel in fusion"),
    "prior_importance":     (0.1, "Fusion prior: importance weight"),
    "prior_recency":        (0.05, "Fusion prior: recency weight"),
    "prior_access":         (0.05, "Fusion prior: access-count weight"),
    "recency_half_life_d":  (90, "Recency prior half-life in days"),
    "rerank":               (True, "Cross-encoder rerank of the fused top candidates"),
    "rerank_blend":         (True, "Blend reranker order with fused order instead of replacing it (keeps graph/keyword "
                                   "evidence the reranker can't see)"),
    "rerank_fused_weight":  (0.5, "With blending on: weight of the fused (vector/keyword/graph) order relative to the "
                                  "reranker's order"),
    "rerank_pool":          (8, "How many fused candidates go to the reranker (CPU cost is ~linear)"),
    "rerank_chars":         (320, "Characters of each candidate the reranker reads (measured: 20 full docs = 3 s "
                                  "on an i5-8250U, 8 × 320 chars ≈ 0.2 s)"),
    "link_k":               (3, "A-MEM: related links created per new memory"),
    "link_min_sim":         (0.6, "A-MEM: minimum cosine similarity for a related link"),
    "recall_min_score":     (-2.0, "Recall hook: minimum rerank logit to inject a memory (measured on real memories: "
                                   "relevant hits ≥ -1.1, irrelevant ≤ -3.6)"),
    "recall_max":           (3, "Recall hook: max memories injected per prompt"),
    "profile_min_importance": (0.7, "Core profile: include active facts at/above this importance"),
    "profile_max_chars":    (3200, "Core profile size cap (~800 tokens)"),
    "consolidate":          (True, "Run nightly consolidation"),
    "consolidate_hour_utc": (22, "Hour (UTC) for nightly consolidation (22:00 UTC = 03:30 IST)"),
    "dup_threshold":        (0.92, "Consolidation: cosine at/above which notes are merge candidates"),
    "archive_after_days":   (180, "Consolidation: archive untouched low-importance notes after N days"),
    "archive_max_importance": (0.3, "Consolidation: only archive notes below this importance"),
    "web_ui":               (False, "Serve the web console at /ui (applied on restart)"),
}

_cache: dict[str, object] | None = None


def _coerce(default, raw):
    if isinstance(default, bool):
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return str(raw)


def _load() -> dict[str, object]:
    out = {}
    for key, (default, _) in DEFAULTS.items():
        env = os.environ.get(key.upper())
        out[key] = _coerce(default, env) if env is not None else default
    for row in db.query("SELECT key, value FROM settings"):
        if row["key"] in DEFAULTS:
            out[row["key"]] = _coerce(DEFAULTS[row["key"]][0], json.loads(row["value"]))
    return out


def get(key: str):
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache[key]


def all_settings() -> list[dict]:
    get("rrf_k")
    return [{"key": k, "value": _cache[k], "default": d, "description": desc} for k, (d, desc) in DEFAULTS.items()]


def put(key: str, value) -> object:
    global _cache
    if key not in DEFAULTS:
        raise KeyError(f"unknown setting {key!r}")
    value = _coerce(DEFAULTS[key][0], value)
    with db.tx() as c:
        c.execute("INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                  (key, json.dumps(value), db.now()))
    _cache = None
    return value


def reset(key: str) -> None:
    global _cache
    with db.tx() as c:
        c.execute("DELETE FROM settings WHERE key = ?", (key,))
    _cache = None
