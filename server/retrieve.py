"""Read path (no LLM): vector + BM25 + PPR graph channels, adaptive RRF fusion with a small prior,
cross-encoder rerank, temporal filters. `explain=True` returns each hit's per-channel ranks for the console."""
import math
import re
from datetime import datetime, timezone

import db
import embeddings
import graph
import settings
import write

POOL = 30
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "what", "which", "who",
         "how", "do", "does", "did", "i", "my", "me", "it", "with", "about", "that", "this", "be", "at", "by", "as"}


def _tokens(q: str) -> list[str]:
    return [t for t in (m.group(0).lower().strip(".-") for m in _TOKEN.finditer(q)) if t and t not in _STOP]


def _fts_query(tokens: list[str]) -> str:
    return " OR ".join('"' + t.replace('"', '') + '"' for t in tokens)


def _filters(domain, type_filter, tag_filter, min_importance, as_of, include_history, alias="m"):
    w, p = [], []
    if as_of:
        # Valid time ("what was true then"), not transaction time: facts recorded today about the past count.
        w.append(f"{alias}.status IN ('active','superseded','archived') AND {alias}.valid_from <= ? "
                 f"AND ({alias}.valid_to IS NULL OR {alias}.valid_to > ?)")
        p += [as_of, as_of]
    elif include_history:
        w.append(f"{alias}.status IN ('active','superseded','archived')")
    else:
        w.append(f"{alias}.status = 'active' AND ({alias}.expires_at IS NULL OR {alias}.expires_at > ?)")
        p.append(db.now())
    if domain:
        w.append(f"{alias}.domain = ?"); p.append(domain)
    if type_filter:
        w.append(f"{alias}.type = ?"); p.append(type_filter)
    if tag_filter:
        w.append(f"EXISTS (SELECT 1 FROM json_each({alias}.tags) WHERE lower(value) = lower(?))"); p.append(tag_filter)
    if min_importance is not None:
        w.append(f"{alias}.importance >= ?"); p.append(min_importance)
    return " AND ".join(w), tuple(p)


def _fts_weight(tokens: list[str]) -> tuple[float, float]:
    """Adaptive fusion: queries made of rare terms (names, ports, versions) lean on BM25, common-word
    queries lean on vectors. Rarity = mean normalised IDF of the query terms (unknown terms = rarest)."""
    if not settings.get("adaptive_fusion") or not tokens:
        return 1.0, 1.0
    n = db.one("SELECT count(*) AS n FROM memories")["n"] or 1
    max_idf = math.log(n + 1)
    idfs = []
    for t in tokens[:12]:
        df = db.one("SELECT count(*) AS df FROM fts_memories WHERE fts_memories MATCH ?", (_fts_query([t]),))["df"]
        idfs.append(math.log((n + 1) / (df + 0.5)) / max_idf if df else 1.0)
    rarity = min(1.0, max(0.0, sum(idfs) / len(idfs)))
    return 1.0, 0.4 + 0.8 * rarity


def _entity_seeds(query: str, tokens: list[str]) -> dict[tuple, float]:
    seeds: dict[tuple, float] = {}
    qn = " " + write.norm_name(query) + " "
    for r in db.query("SELECT id, norm, aliases FROM entities"):
        names = [r["norm"]] + [write.norm_name(a) for a in __import__("json").loads(r["aliases"])]
        if any(f" {n} " in qn for n in names if n):
            seeds[("e", r["id"])] = 1.0
    if not seeds and tokens:
        for r in db.query("SELECT rowid AS id FROM fts_entities WHERE fts_entities MATCH ? ORDER BY bm25(fts_entities) "
                          "LIMIT 3", (_fts_query(tokens),)):
            seeds[("e", r["id"])] = 0.5
    return seeds


def search(query: str, domain: str | None = None, limit: int = 5, type_filter: str | None = None,
           tag_filter: str | None = None, min_importance: float | None = None, as_of: str | None = None,
           include_history: bool = False, rerank: bool | None = None, explain: bool = False,
           touch: bool = True) -> list[dict]:
    where, params = _filters(domain, type_filter, tag_filter, min_importance, as_of, include_history)
    tokens = _tokens(query)
    k = settings.get("rrf_k")
    channels: dict[str, list[int]] = {}

    qemb = embeddings.embed_query(query)
    channels["vector"] = [r["id"] for r in db.query(
        f"SELECT m.id FROM memories m WHERE m.embedding IS NOT NULL AND {where} "
        f"ORDER BY vec_distance_cosine(m.embedding, ?) LIMIT ?", (*params, qemb, POOL))]
    if tokens:
        channels["bm25"] = [r["id"] for r in db.query(
            f"SELECT m.id FROM fts_memories JOIN memories m ON m.id = fts_memories.rowid "
            f"WHERE fts_memories MATCH ? AND {where} ORDER BY bm25(fts_memories) LIMIT ?",
            (_fts_query(tokens), *params, POOL))]
    seeds = _entity_seeds(query, tokens)
    if seeds:
        mass = graph.ppr(seeds)
        if mass:
            ranked = sorted(mass, key=mass.get, reverse=True)[:POOL * 3]
            ok = {r["id"] for r in db.query(
                f"SELECT m.id FROM memories m WHERE m.id IN ({','.join('?' * len(ranked))}) AND {where}",
                (*ranked, *params))}
            channels["graph"] = [i for i in ranked if i in ok][:POOL]

    w_vec, w_fts = _fts_weight(tokens)
    weights = {"vector": w_vec, "bm25": w_fts, "graph": settings.get("w_graph")}
    fused: dict[int, float] = {}
    ranks: dict[int, dict[str, int]] = {}
    for ch, ids in channels.items():
        for rank, mid in enumerate(ids, 1):
            fused[mid] = fused.get(mid, 0.0) + weights[ch] / (k + rank)
            ranks.setdefault(mid, {})[ch] = rank
    if not fused:
        return []

    rows = {r["id"]: r for r in db.query(
        f"SELECT m.*, s.name AS subject_name FROM memories m LEFT JOIN entities s ON s.id = m.subject_id "
        f"WHERE m.id IN ({','.join('?' * len(fused))})", tuple(fused))}
    now = datetime.now(timezone.utc)
    max_acc = max((r["access_count"] for r in rows.values()), default=0)
    half = settings.get("recency_half_life_d")
    pi, pr, pa = settings.get("prior_importance"), settings.get("prior_recency"), settings.get("prior_access")
    prior = {}
    for mid, r in rows.items():
        age_d = max(0.0, (now - datetime.fromisoformat(r["observed_at"])).total_seconds() / 86400)
        acc = math.log1p(r["access_count"]) / math.log1p(max_acc) if max_acc else 0.0
        # Scaled to RRF magnitude so a prior never outweighs a genuine top-ranked hit.
        prior[mid] = (pi * r["importance"] + pr * 0.5 ** (age_d / half) + pa * acc) / (k + 1)
        fused[mid] += prior[mid]

    order = sorted(fused, key=fused.get, reverse=True)
    do_rerank = settings.get("rerank") if rerank is None else rerank
    rr: dict[int, float] = {}
    if do_rerank:
        pool = order[:settings.get("rerank_pool")]
        scores = embeddings.rerank(query, [rows[i]["content"] for i in pool])
        rr = dict(zip(pool, scores))
        order = sorted(pool, key=lambda i: (rr[i], fused[i]), reverse=True) + order[len(pool):]
    top = order[:max(1, limit)]

    if touch and not as_of:
        with db.tx() as c:
            c.executemany("UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                          [(db.now(), i) for i in top])

    out = []
    for i in top:
        r = rows[i]
        d = _public(r)
        d["score"] = round(rr[i], 4) if i in rr else round(fused[i], 5)
        if explain:
            d["explain"] = {"ranks": ranks.get(i, {}), "fused": round(fused[i], 5), "prior": round(prior[i], 5),
                            "rerank": round(rr[i], 4) if i in rr else None,
                            "weights": {c: round(w, 3) for c, w in weights.items() if c in channels}}
        out.append(d)
    return out


def _public(r) -> dict:
    """v1-compatible shape (id/content/type/tags/importance/source/created_at/...) plus v2 fields."""
    d = db.row_dict(r, drop=("embedding", "id", "subject_id", "object_entity_id", "uid", "raw_text"))
    d["id"] = r["uid"]
    d["subject"] = r["subject_name"] if "subject_name" in r.keys() else None
    d["created_at"] = r["recorded_at"]
    d["updated_at"] = r["recorded_at"]
    if r["raw_text"] and r["raw_text"] != r["content"]:
        d["raw_text"] = r["raw_text"]
    d.pop("subject_name", None)
    return d


def list_memories(domain: str | None, limit: int = 100, offset: str | None = None,
                  include_history: bool = False) -> dict:
    """Cursor-paged browse, newest first. Cursor = last row id seen."""
    where, params = _filters(domain, None, None, None, None, include_history)
    cur = int(offset) if offset else None
    rows = db.query(
        f"SELECT m.*, s.name AS subject_name FROM memories m LEFT JOIN entities s ON s.id = m.subject_id "
        f"WHERE {where} {'AND m.id < ?' if cur else ''} ORDER BY m.id DESC LIMIT ?",
        (*params, *((cur,) if cur else ()), limit + 1))
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": [_public(r) for r in rows], "next_offset": str(rows[-1]["id"]) if more and rows else None}


def find_skill(query: str, limit: int = 3) -> list[dict]:
    qemb = embeddings.embed_query(query)
    rows = db.query("SELECT *, 1 - vec_distance_cosine(embedding, ?) AS sim FROM skills ORDER BY sim DESC LIMIT ?",
                    (qemb, max(limit, 5)))
    if not rows:
        return []
    import judge
    criteria = {f"s{r['id']}": f"{r['name']}: {r['description']}" for r in rows}
    criteria["none"] = "None of these skills fits the request"
    pick = judge.choice({"request": query}, "Which skill should be used for `request`?", criteria)
    ranked = sorted(rows, key=lambda r: r["sim"], reverse=True)
    if pick and pick[0] != "none":
        chosen = next(r for r in rows if f"s{r['id']}" == pick[0])
        ranked = [chosen] + [r for r in ranked if r["id"] != chosen["id"]]
    out = []
    for r in ranked[:limit]:
        d = db.row_dict(r, drop=("embedding", "id", "sim"))
        d["id"] = r["uid"]
        d["score"] = round(r["sim"], 4)
        if pick and f"s{r['id']}" == pick[0]:
            d["judge_pick"] = round(pick[1], 3)
        out.append(d)
    return out


def list_skills() -> list[dict]:
    out = []
    for r in db.query("SELECT * FROM skills ORDER BY name"):
        d = db.row_dict(r, drop=("embedding", "id"))
        d["id"] = r["uid"]
        out.append(d)
    return out
