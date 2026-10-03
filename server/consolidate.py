"""Sleep-time consolidation (nightly, no LLM): propose merges of near-duplicate notes and entity aliases,
archive stale low-importance notes. Proposals go to the review queue — nothing is merged or deleted here."""
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import db
import graph
import judge
import settings

log = logging.getLogger("consolidate")


def _reviewed(c, kind: str, a: int, b: int) -> bool:
    return c.execute("SELECT 1 FROM reviews WHERE kind = ? AND ((memory_id = ? AND other_id = ?) OR "
                     "(memory_id = ? AND other_id = ?))", (kind, a, b, b, a)).fetchone() is not None


def run_once() -> dict:
    t0 = time.perf_counter()
    report = {"merge_proposals": 0, "alias_proposals": 0, "archived": 0}
    thr = settings.get("dup_threshold")

    # 1. Near-duplicate notes (free text only; keyed facts are already deduplicated by the ledger).
    notes = db.query("SELECT id, content, embedding, recorded_at FROM memories WHERE status = 'active' "
                     "AND relation IS NULL AND embedding IS NOT NULL")
    pairs = []
    for n in notes:
        for r in db.query("SELECT id, content, recorded_at, 1 - vec_distance_cosine(embedding, ?) AS sim FROM memories "
                          "WHERE status = 'active' AND relation IS NULL AND id > ? AND embedding IS NOT NULL "
                          "ORDER BY sim DESC LIMIT 3", (n["embedding"], n["id"])):
            if r["sim"] >= thr:
                pairs.append((n, r))
    for a, b in pairs:
        newer, older = (a, b) if a["recorded_at"] >= b["recorded_at"] else (b, a)
        with db.tx() as c:
            if _reviewed(c, "merge", newer["id"], older["id"]):
                continue
        p = judge.noul({"a": older["content"], "b": newer["content"]},
                       "Do `a` and `b` say the same thing, so one of them is redundant?")
        if p is not None and p < 0.5:
            continue
        with db.tx() as c:
            c.execute("INSERT INTO reviews(kind, memory_id, other_id, confidence, detail, created_at) VALUES "
                      "('merge', ?, ?, ?, ?, ?)", (newer["id"], older["id"], p if p is not None else b["sim"],
                                                   json.dumps({"similarity": round(b["sim"], 4)}), db.now()))
        report["merge_proposals"] += 1

    # 2. Entity aliases ("postgres" vs "PostgreSQL").
    ents = db.query("SELECT id, name, embedding FROM entities WHERE embedding IS NOT NULL")
    for e in ents:
        for r in db.query("SELECT id, name, 1 - vec_distance_cosine(embedding, ?) AS sim FROM entities "
                          "WHERE id > ? AND embedding IS NOT NULL ORDER BY sim DESC LIMIT 3", (e["embedding"], e["id"])):
            if r["sim"] < 0.88:
                continue
            with db.tx() as c:
                if _reviewed(c, "alias", e["id"], r["id"]):
                    continue
            p = judge.noul({"a": e["name"], "b": r["name"]}, "Are `a` and `b` names for the same real-world thing?")
            if p is not None and p < 0.5:
                continue
            with db.tx() as c:
                c.execute("INSERT INTO reviews(kind, memory_id, other_id, confidence, detail, created_at) VALUES "
                          "('alias', NULL, ?, ?, ?, ?)",
                          (r["id"], p if p is not None else r["sim"],
                           json.dumps({"keep": e["id"], "drop": r["id"], "names": [e["name"], r["name"]]}), db.now()))
            report["alias_proposals"] += 1

    # 3. Archive untouched, unimportant notes (still searchable with include_history; never deleted).
    cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.get("archive_after_days"))).isoformat()
    with db.tx() as c:
        cur = c.execute("UPDATE memories SET status = 'archived' WHERE status = 'active' AND relation IS NULL "
                        "AND importance < ? AND coalesce(last_accessed, recorded_at) < ? AND domain != 'diary'",
                        (settings.get("archive_max_importance"), cutoff))
        report["archived"] = cur.rowcount
        db.log_event(c, "consolidate", latency_ms=round((time.perf_counter() - t0) * 1000), **report)
        if cur.rowcount:
            db.log_event(c, "archive", n=cur.rowcount)
    graph.invalidate()
    return report


def _loop():
    while True:
        now = datetime.now(timezone.utc)
        nxt = now.replace(hour=settings.get("consolidate_hour_utc"), minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        time.sleep((nxt - now).total_seconds())
        if settings.get("consolidate"):
            try:
                log.info("consolidation: %s", run_once())
            except Exception:
                log.exception("consolidation failed")


def start_scheduler() -> None:
    threading.Thread(target=_loop, daemon=True, name="consolidate").start()
