"""Write path: entity resolution, key resolution + deterministic supersession, free-text conflict gate,
temporal form, A-MEM links. Slow work (embedding, judge calls) happens before the write transaction;
the transaction re-checks what it relies on, so a concurrent write can't break the one-active-value rule."""
import json
import re
import sqlite3
import uuid

import db
import embeddings
import graph
import judge
import settings

DOMAINS = ("identity", "projects", "code", "general", "diary")
TEMPORAL_FORMS = ("ongoing", "completed", "habitual", "unknown")


def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().strip(".,;:!?\"'").lower())


def norm_relation(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.strip().lower()).strip("_")


def _cos_top(c: sqlite3.Connection, table: str, emb: bytes, where: str, params: tuple, k: int) -> list[sqlite3.Row]:
    return c.execute(
        f"SELECT *, 1 - vec_distance_cosine(embedding, ?) AS sim FROM {table} "
        f"WHERE embedding IS NOT NULL AND {where} ORDER BY sim DESC LIMIT ?",
        (emb, *params, k)).fetchall()


# ── entities ──────────────────────────────────────────────────────────────────────────────────────

def find_entity(name: str) -> sqlite3.Row | None:
    """Exact name or alias match only (deterministic)."""
    n = norm_name(name)
    return db.one("SELECT * FROM entities WHERE norm = ? OR EXISTS "
                  "(SELECT 1 FROM json_each(entities.aliases) WHERE lower(value) = ?)", (n, n))


def _plan_entity(name: str) -> tuple[int | None, dict]:
    """Return (existing entity id or None-for-new, trace). Judge links fuzzy mentions to existing entities."""
    hit = find_entity(name)
    if hit:
        return hit["id"], {"entity": hit["name"], "how": "exact"}
    emb = embeddings.embed_doc(name)  # name-to-name similarity: symmetric, no query prefix
    with db._lock:
        cands = _cos_top(db.conn(), "entities", emb, "1", (), 5)
    cands = [r for r in cands if r["sim"] >= 0.55]
    if not cands:
        return None, {"how": "new"}
    criteria = {f"e{r['id']}": r["name"] + (f" (also: {', '.join(json.loads(r['aliases']))})"
                                            if json.loads(r["aliases"]) else "") for r in cands}
    criteria["new"] = "None of these: a different, new thing"
    pick = judge.choice({"mention": name},
                        "A memory mentions `mention`. Which known entity is it the same real-world thing as?",
                        criteria)
    if pick and pick[0] != "new" and pick[1] >= settings.get("entity_link"):
        eid = int(pick[0][1:])
        return eid, {"how": "judge", "confidence": pick[1], "entity_id": eid}
    return None, {"how": "new", "judge": pick}


def _create_entity(c: sqlite3.Connection, name: str, kind: str | None = None) -> int:
    n = norm_name(name)
    row = c.execute("SELECT id FROM entities WHERE norm = ?", (n,)).fetchone()
    if row:
        return row["id"]
    cur = c.execute("INSERT INTO entities(name, norm, kind, embedding, created_at) VALUES (?,?,?,?,?)",
                    (name.strip(), n, kind, embeddings.embed_doc(name), db.now()))
    eid = cur.lastrowid
    # Facts that named this thing as a plain value before it became an entity now point at it (graph backfill).
    c.execute("UPDATE memories SET object_entity_id = ? WHERE object_entity_id IS NULL AND lower(trim(object_text)) = ?",
              (eid, n))
    return eid


def add_alias(entity_id: int, alias: str) -> None:
    with db.tx() as c:
        row = c.execute("SELECT aliases FROM entities WHERE id = ?", (entity_id,)).fetchone()
        aliases = json.loads(row["aliases"])
        if norm_name(alias) not in (norm_name(a) for a in aliases):
            aliases.append(alias.strip())
            c.execute("UPDATE entities SET aliases = ? WHERE id = ?", (json.dumps(aliases), entity_id))
    graph.invalidate()


# ── memories ──────────────────────────────────────────────────────────────────────────────────────

def _temporal_form(text: str, given: str | None) -> str:
    if given in TEMPORAL_FORMS:
        return given
    pick = judge.choice({"statement": text},
                        "Is `statement` about something ongoing now, finished in the past, or a habit?",
                        {"ongoing": "ongoing / currently true", "completed": "finished, happened in the past",
                         "habitual": "a recurring habit or routine", "unknown": "a plain fact, none of these"})
    return pick[0] if pick and pick[1] >= 0.5 else "unknown"


def store_memory(content: str, domain: str, type: str = "note", tags: list[str] | None = None,
                 importance: float = 0.5, source: str | None = None, agent: str | None = None,
                 session: str | None = None, subject: str | None = None, relation: str | None = None,
                 object: str | None = None, observed_at: str | None = None, temporal_form: str | None = None,
                 external_id: str | None = None, expires_at: str | None = None, extra: dict | None = None) -> dict:
    if domain not in DOMAINS:
        raise ValueError(f"Invalid domain '{domain}'. Choose from: {', '.join(DOMAINS)}.")
    if bool(subject) != bool(relation):
        raise ValueError("Pass subject and relation together (or neither, for a free-text note).")
    importance = max(0.0, min(1.0, float(importance)))
    tags = [t.strip() for t in (tags or []) if t and t.strip()]
    now = db.now()
    observed_at = observed_at or now

    if external_id:
        existing = db.one("SELECT id, uid FROM memories WHERE external_id = ?", (external_id,))
        if existing:
            return _update_in_place(existing, content, type, tags, importance, source, extra, observed_at)

    emb = embeddings.embed_doc(content)
    trace: dict = {}
    keyed = subject is not None
    subj_id = obj_id = None
    rel = norm_relation(relation) if relation else None
    target = None          # existing active fact this one updates
    target_conf = None
    review_band = False

    if keyed:
        subj_id, trace["subject"] = _plan_entity(subject)
        if object:
            hit = find_entity(object)
            obj_id = hit["id"] if hit else None
        if subj_id is not None:
            same_key = db.one("SELECT * FROM memories WHERE status='active' AND domain=? AND subject_id=? AND relation=?",
                              (domain, subj_id, rel))
            if same_key:
                target, target_conf, trace["key"] = same_key, 1.0, "exact"
            else:
                facts = db.query("SELECT * FROM memories WHERE status='active' AND domain=? AND subject_id=? "
                                 "AND relation IS NOT NULL ORDER BY valid_from DESC LIMIT 12", (domain, subj_id))
                if facts:
                    criteria = {f"m{r['id']}": f"{r['relation'].replace('_', ' ')}: {r['content']}" for r in facts}
                    criteria["none"] = "None of these: it is a different attribute"
                    pick = judge.choice({"new_statement": content},
                                        "`new_statement` was just learned. Which existing fact does it replace "
                                        "with a new value for the same attribute?", criteria)
                    trace["key"] = {"judge": pick}
                    if pick and pick[0] != "none":
                        cand = next(r for r in facts if f"m{r['id']}" == pick[0])
                        if pick[1] >= settings.get("supersede_auto"):
                            target, target_conf, rel = cand, pick[1], cand["relation"]  # adopt the canonical key
                        elif pick[1] >= settings.get("supersede_review"):
                            target, target_conf, review_band = cand, pick[1], True
    tform = _temporal_form(content, temporal_form)

    # Free-text conflict gate: does this note contradict an active fact? (Only when a judge is available.)
    conflict = None
    if not keyed:
        with db._lock:
            near = _cos_top(db.conn(), "memories", emb, "status = 'active'", (), 5)
        best = None
        for r in near:
            if r["sim"] < 0.5:
                continue
            p = judge.noul({"a": r["content"], "b": content},
                           "Do `a` and `b` give different values for the same attribute of the same thing, "
                           "so that `b` makes `a` out of date?")
            if p is not None and (best is None or p > best[1]):
                best = (r, p)
        if best and best[1] >= settings.get("conflict_threshold"):
            conflict = best

    uid = str(uuid.uuid4())
    result = {"id": uid, "status": "active", "domain": domain}
    with db.tx() as c:
        if keyed and subj_id is None:
            subj_id = _create_entity(c, subject)
            trace["subject"]["entity_id"] = subj_id
        if target is not None:
            fresh = c.execute("SELECT status FROM memories WHERE id = ?", (target["id"],)).fetchone()
            if fresh["status"] != "active":
                target = None  # someone else already retired it; insert as a plain new fact

        status, valid_to, superseded_by = "active", None, None
        late_key = None
        if target is not None and not review_band and observed_at < target["valid_from"]:
            # An older observation arriving late: it's history, not the current value. It is valid until the
            # next later value of this key, and it cuts short any earlier value it falls inside.
            late_key = (domain, target["subject_id"], rel)
            nxt = c.execute("SELECT id, valid_from FROM memories WHERE domain=? AND subject_id=? AND relation=? AND "
                            "status IN ('active','superseded') AND valid_from > ? ORDER BY valid_from LIMIT 1",
                            (*late_key, observed_at)).fetchone()
            status, valid_to, superseded_by = "superseded", nxt["valid_from"], nxt["id"]
        elif target is not None and not review_band:
            c.execute("UPDATE memories SET status='superseded', valid_to=? WHERE id=?", (observed_at, target["id"]))
        elif review_band:
            status = "pending"

        cur = c.execute(
            "INSERT INTO memories(uid, external_id, domain, type, subject_id, relation, object_text, object_entity_id, "
            "content, raw_text, temporal_form, tags, importance, source, agent, session, extra, observed_at, valid_from, "
            "valid_to, recorded_at, superseded_by, status, expires_at, embedding) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, external_id, domain, type, subj_id, rel, object, obj_id, content, content, tform, json.dumps(tags),
             importance, source, agent, session, json.dumps(extra or {}), observed_at, observed_at, valid_to, now,
             superseded_by, status, expires_at, emb))
        mid = cur.lastrowid
        if late_key:
            c.execute("UPDATE memories SET valid_to=?, superseded_by=? WHERE domain=? AND subject_id=? AND relation=? "
                      "AND status='superseded' AND id != ? AND valid_from < ? AND valid_to > ?",
                      (observed_at, mid, *late_key, mid, observed_at, observed_at))

        if target is not None and status == "active":
            c.execute("UPDATE memories SET superseded_by=? WHERE id=?", (mid, target["id"]))
            db.log_event(c, "supersede", agent=agent, memory_id=mid, old=target["uid"], old_value=target["content"],
                         confidence=target_conf, key=f"{subject}.{rel}")
            result["superseded"] = [{"id": target["uid"], "content": target["content"]}]
        elif status == "superseded":
            result["status"] = "superseded"
            result["note"] = "Older than the current value, so stored as history."
        elif review_band:
            c.execute("INSERT INTO reviews(kind, memory_id, other_id, confidence, detail, created_at) VALUES "
                      "('supersede', ?, ?, ?, ?, ?)",
                      (mid, target["id"], target_conf, json.dumps({"key": f"{subject}.{target['relation']}"}), now))
            result.update(status="pending", needs_review=True,
                          message=f"Unsure whether this replaces: {target['content']!r}. Queued for review.")

        if conflict:
            old, p = conflict
            c.execute("UPDATE memories SET status='pending' WHERE id=? AND status='active'", (old["id"],))
            c.execute("INSERT INTO reviews(kind, memory_id, other_id, confidence, detail, created_at) VALUES "
                      "('conflict', ?, ?, ?, ?, ?)", (mid, old["id"], p, json.dumps({"old": old["content"]}), now))
            result["conflict"] = {"id": old["uid"], "content": old["content"], "probability": round(p, 3),
                                  "action": "old fact hidden pending review",
                                  "hint": "If this is a changeable fact, re-store it with subject and relation."}

        _link(c, mid, emb)
        db.log_event(c, "store", agent=agent, memory_id=mid, domain=domain, keyed=keyed, trace=trace)
    graph.invalidate()
    if keyed:
        result["key"] = f"{subject}.{rel}"
    return result


def _link(c: sqlite3.Connection, mid: int, emb: bytes) -> None:
    near = _cos_top(c, "memories", emb, "status IN ('active','pending') AND id != ?", (mid,), settings.get("link_k"))
    for r in near:
        if r["sim"] >= settings.get("link_min_sim"):
            a, b = sorted((mid, r["id"]))
            c.execute("INSERT OR IGNORE INTO links(src, dst, weight, created_at) VALUES (?,?,?,?)",
                      (a, b, round(r["sim"], 4), db.now()))


def _update_in_place(existing, content, type, tags, importance, source, extra, observed_at) -> dict:
    """Idempotent re-import / edit of a caller-keyed note (external_id). Not a new version."""
    with db.tx() as c:
        old = c.execute("SELECT content FROM memories WHERE id = ?", (existing["id"],)).fetchone()
        emb = embeddings.embed_doc(content) if old["content"] != content else None
        c.execute("UPDATE memories SET content=?, raw_text=?, type=?, tags=?, importance=?, source=COALESCE(?, source), "
                  "extra=?, observed_at=?, embedding=COALESCE(?, embedding) WHERE id=?",
                  (content, content, type, json.dumps(tags), importance, source, json.dumps(extra or {}), observed_at,
                   emb, existing["id"]))
        db.log_event(c, "update", memory_id=existing["id"])
    graph.invalidate()
    return {"id": existing["uid"], "status": "updated"}


def resolve_uid(ref: str) -> sqlite3.Row | None:
    """Accept a v2 uid or a caller external_id (v1 ids after import)."""
    return db.one("SELECT * FROM memories WHERE uid = ? OR external_id = ?", (ref, ref))


def delete_memory(ref: str, agent: str | None = None) -> bool:
    row = resolve_uid(ref)
    if row is None:
        return False
    with db.tx() as c:
        c.execute("UPDATE memories SET status='deleted', valid_to=COALESCE(valid_to, ?) WHERE id=?", (db.now(), row["id"]))
        db.log_event(c, "delete", agent=agent, memory_id=row["id"])
    graph.invalidate()
    return True


def resolve_review(review_id: int, approve: bool) -> dict:
    with db.tx() as c:
        rv = c.execute("SELECT * FROM reviews WHERE id = ? AND status = 'open'", (review_id,)).fetchone()
        if rv is None:
            raise ValueError("No open review with that id.")
        now = db.now()
        new = c.execute("SELECT * FROM memories WHERE id = ?", (rv["memory_id"],)).fetchone() if rv["memory_id"] else None
        old = c.execute("SELECT * FROM memories WHERE id = ?", (rv["other_id"],)).fetchone() \
            if rv["kind"] in ("supersede", "conflict", "merge") else None
        if rv["kind"] == "supersede":
            if approve:
                c.execute("UPDATE memories SET status='superseded', valid_to=?, superseded_by=? WHERE id=?",
                          (new["valid_from"], new["id"], old["id"]))
                c.execute("UPDATE memories SET status='active', relation=? WHERE id=?", (old["relation"], new["id"]))
            else:
                c.execute("UPDATE memories SET status='active' WHERE id=?", (new["id"],))  # a separate attribute
        elif rv["kind"] == "conflict":
            if approve:   # the new note replaces the old fact
                c.execute("UPDATE memories SET status='superseded', valid_to=?, superseded_by=? WHERE id=?",
                          (new["valid_from"], new["id"], old["id"]))
            else:
                c.execute("UPDATE memories SET status='active' WHERE id=? AND status='pending'", (old["id"],))
        elif rv["kind"] == "merge":
            if approve:   # keep the newer one, retire the duplicate
                c.execute("UPDATE memories SET status='superseded', valid_to=?, superseded_by=? WHERE id=?",
                          (now, new["id"], old["id"]))
        elif rv["kind"] == "alias" and approve:
            detail = json.loads(rv["detail"])
            keep, drop = detail["keep"], detail["drop"]
            drop_row = c.execute("SELECT name, aliases FROM entities WHERE id = ?", (drop,)).fetchone()
            keep_row = c.execute("SELECT aliases FROM entities WHERE id = ?", (keep,)).fetchone()
            aliases = json.loads(keep_row["aliases"]) + [drop_row["name"]] + json.loads(drop_row["aliases"])
            c.execute("UPDATE entities SET aliases = ? WHERE id = ?", (json.dumps(sorted(set(aliases))), keep))
            c.execute("UPDATE memories SET subject_id = ? WHERE subject_id = ?", (keep, drop))
            c.execute("UPDATE memories SET object_entity_id = ? WHERE object_entity_id = ?", (keep, drop))
        c.execute("UPDATE reviews SET status=?, resolved_at=? WHERE id=?", ("approved" if approve else "rejected", now, rv["id"]))
        db.log_event(c, "review", memory_id=rv["memory_id"], review=rv["id"], kind=rv["kind"], approved=approve)
    graph.invalidate()
    return {"review": review_id, "approved": approve}


# ── skills ────────────────────────────────────────────────────────────────────────────────────────

def store_skill(name: str, description: str, instructions: str, domain: str = "general",
                trigger_tags: list[str] | None = None, examples: list[str] | None = None,
                external_id: str | None = None) -> dict:
    trigger_tags = [t.strip() for t in (trigger_tags or []) if t.strip()]
    examples = [e.strip() for e in (examples or []) if e.strip()]
    emb = embeddings.embed_doc(f"{name} {description} {' '.join(trigger_tags)} {' '.join(examples)}")
    now = db.now()
    with db.tx() as c:
        row = c.execute("SELECT * FROM skills WHERE name = ?", (name,)).fetchone()
        if row and (row["description"], row["domain"], row["instructions"], json.loads(row["trigger_tags"]),
                    json.loads(row["examples"])) == (description, domain, instructions, trigger_tags, examples):
            return {"id": row["uid"], "name": name, "version": row["version"], "status": "unchanged"}
        if row:
            c.execute("UPDATE skills SET description=?, domain=?, trigger_tags=?, instructions=?, examples=?, "
                      "version=version+1, updated_at=?, embedding=?, external_id=COALESCE(external_id, ?) WHERE id=?",
                      (description, domain, json.dumps(trigger_tags), instructions, json.dumps(examples), now, emb,
                       external_id, row["id"]))
            db.log_event(c, "skill_update", name=name)
            return {"id": row["uid"], "name": name, "version": row["version"] + 1, "status": "updated"}
        uid = str(uuid.uuid4())
        c.execute("INSERT INTO skills(uid, external_id, name, description, domain, trigger_tags, instructions, examples, "
                  "created_at, updated_at, embedding) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (uid, external_id, name, description, domain, json.dumps(trigger_tags), instructions,
                   json.dumps(examples), now, now, emb))
        db.log_event(c, "skill_store", name=name)
    return {"id": uid, "name": name, "version": 1, "status": "created"}


def delete_skill(ref: str) -> bool:
    with db.tx() as c:
        cur = c.execute("DELETE FROM skills WHERE uid = ? OR name = ? OR external_id = ?", (ref, ref, ref))
        if cur.rowcount:
            db.log_event(c, "skill_delete", ref=ref)
        return cur.rowcount > 0
