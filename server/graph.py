"""Memory graph: entity and memory nodes, Personalized PageRank (HippoRAG-style) retrieval, neighbours, timelines.

Nodes are ("e", entity_id) and ("m", memory_id). Edges: memory—subject entity, memory—object entity,
memory—memory (A-MEM links, weighted by similarity). Only active memories are in the walk graph.
"""
import threading

import db

_lock = threading.Lock()
_adj: dict[tuple, dict[tuple, float]] | None = None


def invalidate() -> None:
    global _adj
    with _lock:
        _adj = None


def _add(adj, a, b, w):
    adj.setdefault(a, {})[b] = adj.get(a, {}).get(b, 0.0) + w
    adj.setdefault(b, {})[a] = adj.get(b, {}).get(a, 0.0) + w


def _graph() -> dict[tuple, dict[tuple, float]]:
    global _adj
    with _lock:
        if _adj is not None:
            return _adj
        adj: dict = {}
        for r in db.query("SELECT id, subject_id, object_entity_id FROM memories WHERE status = 'active'"):
            if r["subject_id"]:
                _add(adj, ("m", r["id"]), ("e", r["subject_id"]), 1.0)
            if r["object_entity_id"]:
                _add(adj, ("m", r["id"]), ("e", r["object_entity_id"]), 1.0)
        for r in db.query("SELECT l.src, l.dst, l.weight FROM links l JOIN memories a ON a.id = l.src "
                          "JOIN memories b ON b.id = l.dst WHERE a.status = 'active' AND b.status = 'active'"):
            _add(adj, ("m", r["src"]), ("m", r["dst"]), r["weight"])
        _adj = adj
        return adj


def ppr(seeds: dict[tuple, float], damping: float = 0.5, iters: int = 20) -> dict[int, float]:
    """Personalized PageRank from seed nodes; returns {memory_id: mass} for memory nodes.
    ponytail: dict-based power iteration, fine to ~100k edges; scipy.sparse if the graph outgrows it."""
    adj = _graph()
    seeds = {n: w for n, w in seeds.items() if n in adj and w > 0}
    if not seeds:
        return {}
    total = sum(seeds.values())
    reset = {n: w / total for n, w in seeds.items()}
    deg = {n: sum(nb.values()) for n, nb in adj.items()}
    p = dict(reset)
    for _ in range(iters):
        nxt = {n: (1 - damping) * w for n, w in reset.items()}
        for n, mass in p.items():
            if not mass or not deg.get(n):
                continue
            share = damping * mass / deg[n]
            for nb, w in adj[n].items():
                nxt[nb] = nxt.get(nb, 0.0) + share * w
        p = nxt
    return {n[1]: m for n, m in p.items() if n[0] == "m"}


def entity_profile(entity_id: int, depth: int = 1) -> dict:
    ent = db.row_dict(db.one("SELECT * FROM entities WHERE id = ?", (entity_id,)))
    if ent is None:
        return {}
    facts = [db.row_dict(r) for r in db.query(
        "SELECT uid, domain, relation, object_text, content, valid_from, importance FROM memories "
        "WHERE subject_id = ? AND status = 'active' ORDER BY relation", (entity_id,))]
    mentioned_in = [db.row_dict(r) for r in db.query(
        "SELECT m.uid, e.name AS subject, m.relation, m.content FROM memories m JOIN entities e ON e.id = m.subject_id "
        "WHERE m.object_entity_id = ? AND m.status = 'active'", (entity_id,))]
    # Entity neighbourhood through facts, both directions, up to `depth` hops.
    rows = db.query(
        """WITH RECURSIVE hop(eid, d) AS (
             SELECT ?, 0
             UNION
             SELECT CASE WHEN m.subject_id = hop.eid THEN m.object_entity_id ELSE m.subject_id END, hop.d + 1
             FROM memories m JOIN hop ON (m.subject_id = hop.eid OR m.object_entity_id = hop.eid)
             WHERE m.status = 'active' AND m.subject_id IS NOT NULL AND m.object_entity_id IS NOT NULL AND hop.d < ?
           )
           SELECT DISTINCT e.id, e.name, MIN(hop.d) AS hops FROM hop JOIN entities e ON e.id = hop.eid
           WHERE e.id != ? GROUP BY e.id ORDER BY hops, e.name""", (entity_id, min(depth, 3), entity_id))
    return {"entity": ent, "facts": facts, "mentioned_in": mentioned_in,
            "neighbors": [dict(r) for r in rows]}


def timeline(entity_id: int) -> list[dict]:
    """Every value every relation of this entity has had, with validity windows (deleted rows excluded)."""
    return [db.row_dict(r) for r in db.query(
        "SELECT uid, domain, relation, object_text, content, status, valid_from, valid_to, recorded_at, agent "
        "FROM memories WHERE subject_id = ? AND status != 'deleted' ORDER BY relation, valid_from", (entity_id,))]


def snapshot(domain: str | None = None, include_history: bool = False, include_notes: bool = False,
             limit: int = 2000) -> dict:
    """Graph for the web console: entity nodes, fact edges (entity→entity), value facts, optional notes + links."""
    statuses = "('active','superseded')" if include_history else "('active')"
    dom = "AND m.domain = ?" if domain else ""
    params = (domain,) if domain else ()
    facts = [db.row_dict(r) for r in db.query(
        f"SELECT m.id, m.uid, m.domain, m.subject_id, m.object_entity_id, m.relation, m.object_text, m.content, "
        f"m.status, m.valid_from, m.valid_to, m.agent FROM memories m "
        f"WHERE m.subject_id IS NOT NULL AND m.status IN {statuses} {dom} LIMIT ?", (*params, limit))]
    ent_ids = {f["subject_id"] for f in facts} | {f["object_entity_id"] for f in facts if f["object_entity_id"]}
    entities = [db.row_dict(r) for r in db.query(
        f"SELECT id, name, kind, aliases FROM entities WHERE id IN ({','.join('?' * len(ent_ids)) or 'NULL'})",
        tuple(ent_ids))]
    out = {"entities": entities, "facts": facts}
    if include_notes:
        out["notes"] = [db.row_dict(r) for r in db.query(
            f"SELECT m.id, m.uid, m.domain, m.content, m.importance, m.observed_at FROM memories m "
            f"WHERE m.subject_id IS NULL AND m.status = 'active' {dom} LIMIT ?", (*params, limit))]
        out["links"] = [dict(r) for r in db.query(
            "SELECT l.src, l.dst, l.weight FROM links l JOIN memories a ON a.id = l.src JOIN memories b ON b.id = l.dst "
            "WHERE a.status = 'active' AND b.status = 'active' LIMIT ?", (limit * 3,))]
    return out
