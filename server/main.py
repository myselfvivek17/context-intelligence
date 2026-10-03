"""Context-Intelligence v2 — MCP server + REST API (+ optional web console)."""
import functools
import hmac
import json
import logging
import os
import sys
import time
from typing import Optional

import anyio
from fastmcp import FastMCP
from fastmcp.server.auth.providers.debug import DebugTokenVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse

import db
import embeddings
import graph
import judge
import profile
import retrieve
import settings
import write

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
MCP_API_KEY = os.environ.get("MCP_API_KEY") or None
MAX_SEARCH_LIMIT = int(os.environ.get("MAX_SEARCH_LIMIT", 50))

auth = DebugTokenVerifier(validate=lambda t: hmac.compare_digest(t, MCP_API_KEY), client_id="mcp-client",
                          scopes=["full"]) if MCP_API_KEY else None
mcp = FastMCP("context-intelligence", auth=auth)


def _csv(s: str | None) -> list[str]:
    return [t.strip() for t in (s or "").split(",") if t.strip()]


def logged(fn):
    """Every tool call lands in `events` (usage + latency for the console) and as a JSON log line."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        detail = {"domain": kwargs.get("domain")}
        try:
            out = fn(*args, **kwargs)
            items = out.get("items") if isinstance(out, dict) else out
            if isinstance(items, list):
                detail["n_results"] = len(items)
            return out
        except Exception as e:
            detail["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            ms = round((time.perf_counter() - t0) * 1000)
            try:
                with db.tx() as c:
                    db.log_event(c, "tool_call", tool=fn.__name__, agent=kwargs.get("agent"), latency_ms=ms, **detail)
            except Exception:
                logging.exception("event log failed")
            print(json.dumps({"evt": "tool_call", "tool": fn.__name__, "ms": ms, **detail}), flush=True)
    return wrapper


# ── memory tools (v1-compatible names and parameters; v2 parameters are optional) ─────────────────

@mcp.tool()
@logged
def store_memory_tool(content: str, domain: str, type: str, tags: str = "", importance: float = 0.5,
                      source: str = "user_stated", expires_at: Optional[str] = None,
                      subject: Optional[str] = None, relation: Optional[str] = None, object: Optional[str] = None,
                      observed_at: Optional[str] = None, temporal_form: Optional[str] = None,
                      agent: Optional[str] = None) -> dict:
    """
    Store a memory. domain: identity | projects | code | general | diary. type: preference | fact | decision | goal | note.
    importance 0.0-1.0. tags: comma-separated keywords.

    For any fact that can CHANGE later (a preference, a current tool/stack/version, a config value, where
    something runs, who owns what), also pass `subject` + `relation` (+ `object` for the value), e.g.
    subject="Vivek", relation="primary database", object="Postgres". Storing the same subject+relation later
    automatically retires the old value, so memory never serves a stale fact. Call list_keys_tool(subject)
    first to reuse an existing relation name. Leave subject/relation empty for one-off notes.
    observed_at: ISO time the fact became true, if not now. temporal_form: ongoing | completed | habitual.
    """
    return write.store_memory(content, domain, type=type, tags=_csv(tags), importance=importance, source=source,
                              agent=agent, subject=subject, relation=relation, object=object,
                              observed_at=observed_at, temporal_form=temporal_form, expires_at=expires_at)


@mcp.tool()
@logged
def search_memory_tool(query: str, domain: Optional[str] = None, limit: int = 5, type_filter: Optional[str] = None,
                       tag_filter: Optional[str] = None, min_importance: Optional[float] = None,
                       as_of: Optional[str] = None, include_history: bool = False, rerank: Optional[bool] = None,
                       agent: Optional[str] = None) -> list[dict]:
    """
    Search memories (semantic + keyword + knowledge-graph, reranked). Returns only what is currently true.
    domain: identity | projects | code | general | diary, or omit to search all. as_of: ISO time to ask what was true then.
    include_history: also return superseded/archived values.
    """
    return retrieve.search(query, domain=domain, limit=min(limit, MAX_SEARCH_LIMIT), type_filter=type_filter,
                           tag_filter=tag_filter, min_importance=min_importance, as_of=as_of,
                           include_history=include_history, rerank=rerank)


@mcp.tool()
@logged
def list_memories_tool(domain: Optional[str] = None, limit: int = 100, offset: Optional[str] = None,
                       include_history: bool = False) -> dict:
    """Browse memories newest-first without a query. domain: identity | projects | code | general | diary or omit for all.
    Returns {"items": [...], "next_offset": cursor or null}; pass next_offset back to page."""
    return retrieve.list_memories(domain, min(limit, 500), offset, include_history)


@mcp.tool()
@logged
def delete_memory_tool(point_id: str, domain: Optional[str] = None) -> str:
    """Retire a memory by id (kept in history, hidden from search). domain is accepted for v1 compatibility."""
    return f"Deleted {point_id}" if write.delete_memory(point_id) else f"No memory with id {point_id}"


@mcp.tool()
@logged
def list_keys_tool(subject: str) -> dict:
    """The relation names (keys) already used for a subject, with their current values. Use before storing a
    changeable fact so the same attribute keeps the same name."""
    ent = write.find_entity(subject)
    if not ent:
        return {"subject": subject, "known": False, "keys": []}
    rows = db.query("SELECT domain, relation, content, valid_from FROM memories WHERE subject_id = ? AND "
                    "status = 'active' AND relation IS NOT NULL ORDER BY relation", (ent["id"],))
    return {"subject": ent["name"], "known": True, "keys": [dict(r) for r in rows]}


@mcp.tool()
@logged
def profile_tool() -> str:
    """The core profile: a short, dated summary of the most important things currently true."""
    return profile.build()


@mcp.tool()
@logged
def entity_tool(name: str, depth: int = 1) -> dict:
    """Everything currently known about an entity (person, project, tool, machine), plus connected entities."""
    ent = write.find_entity(name)
    return graph.entity_profile(ent["id"], depth) if ent else {"error": f"Unknown entity '{name}'"}


@mcp.tool()
@logged
def timeline_tool(name: str) -> dict:
    """How an entity's facts changed over time: every value each relation has had, with validity windows."""
    ent = write.find_entity(name)
    return {"entity": ent["name"], "history": graph.timeline(ent["id"])} if ent else {"error": f"Unknown entity '{name}'"}


@mcp.tool()
@logged
def review_queue_tool(limit: int = 20) -> list[dict]:
    """Open items the server did not auto-resolve (uncertain updates, contradictions, merge/alias proposals)."""
    return _reviews(limit)


@mcp.tool()
@logged
def resolve_review_tool(review_id: int, approve: bool) -> dict:
    """Approve or reject an open review item."""
    return write.resolve_review(review_id, approve)


@mcp.tool()
@logged
def memory_stats_tool() -> dict:
    """Usage and health: counts per domain/status, reads/writes per agent (7/30 days), judge status."""
    return stats()


# ── skills ────────────────────────────────────────────────────────────────────────────────────────

@mcp.tool()
@logged
def store_skill_tool(name: str, description: str, instructions: str, domain: str = "general",
                     trigger_tags: str = "", examples: str = "") -> dict:
    """Store or update (by name) a reusable skill. domain: identity | projects | code | general | diary. trigger_tags / examples:
    comma-separated. instructions: step-by-step markdown."""
    return write.store_skill(name, description, instructions, domain, _csv(trigger_tags), _csv(examples))


@mcp.tool()
@logged
def find_skill_tool(query: str, limit: int = 3) -> list[dict]:
    """Find the skill(s) that fit a request. The best match comes first."""
    return retrieve.find_skill(query, min(limit, MAX_SEARCH_LIMIT))


@mcp.tool()
@logged
def list_skills_tool() -> list[dict]:
    """List all skills."""
    return retrieve.list_skills()


@mcp.tool()
@logged
def delete_skill_tool(name_or_id: str) -> str:
    """Delete a skill by name or id."""
    return "Deleted" if write.delete_skill(name_or_id) else "Not found"


# ── shared helpers ────────────────────────────────────────────────────────────────────────────────

def _reviews(limit: int = 50) -> list[dict]:
    out = []
    for r in db.query("SELECT * FROM reviews WHERE status = 'open' ORDER BY created_at DESC LIMIT ?", (limit,)):
        d = db.row_dict(r)
        for side in ("memory_id", "other_id"):
            if r[side] and r["kind"] != "alias":
                m = db.one("SELECT uid, content, status FROM memories WHERE id = ?", (r[side],))
                d[side.replace("_id", "")] = dict(m) if m else None
        out.append(d)
    return out


def stats() -> dict:
    by = lambda sql, p=(): [dict(r) for r in db.query(sql, p)]
    return {
        "memories": by("SELECT domain, status, count(*) AS n FROM memories GROUP BY domain, status ORDER BY domain"),
        "entities": db.one("SELECT count(*) AS n FROM entities")["n"],
        "links": db.one("SELECT count(*) AS n FROM links")["n"],
        "skills": db.one("SELECT count(*) AS n FROM skills")["n"],
        "open_reviews": db.one("SELECT count(*) AS n FROM reviews WHERE status = 'open'")["n"],
        "calls_7d": by("SELECT tool, coalesce(agent, '?') AS agent, count(*) AS n, round(avg(latency_ms)) AS avg_ms "
                       "FROM events WHERE op = 'tool_call' AND ts >= datetime('now', '-7 days') "
                       "GROUP BY tool, agent ORDER BY n DESC"),
        "writes_30d": db.one("SELECT count(*) AS n FROM events WHERE op = 'store' "
                             "AND ts >= datetime('now', '-30 days')")["n"],
        "supersessions_30d": db.one("SELECT count(*) AS n FROM events WHERE op = 'supersede' "
                                    "AND ts >= datetime('now', '-30 days')")["n"],
        "judge": judge.status(),
        "db_bytes": os.path.getsize(db.DB_PATH) if os.path.exists(db.DB_PATH) else None,
    }


# ── REST (hooks, LifeHub, dashboard). Custom routes bypass MCP auth, so check the bearer here. ─────

def _authed(request: Request) -> bool:
    if not MCP_API_KEY:
        return True
    h = request.headers.get("authorization", "")
    return h.startswith("Bearer ") and hmac.compare_digest(h[7:], MCP_API_KEY)


def _api(fn):
    @functools.wraps(fn)
    async def handler(request: Request):
        if not _authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json() if request.method == "POST" else dict(request.query_params)
            return JSONResponse(await anyio.to_thread.run_sync(lambda: fn(body)))
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    return handler


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    n = await anyio.to_thread.run_sync(lambda: db.one("SELECT count(*) AS n FROM memories")["n"])
    return JSONResponse({"ok": True, "memories": n})


@mcp.custom_route("/v1/memories", methods=["POST"])
@_api
def api_store(b: dict):
    tags = b.get("tags") or []
    return write.store_memory(b["content"], b["domain"], type=b.get("type", "note"),
                              tags=_csv(tags) if isinstance(tags, str) else tags,
                              importance=b.get("importance", 0.5), source=b.get("source"), agent=b.get("agent"),
                              session=b.get("session"), subject=b.get("subject"), relation=b.get("relation"),
                              object=b.get("object"), observed_at=b.get("observed_at"),
                              temporal_form=b.get("temporal_form"), external_id=b.get("external_id"),
                              expires_at=b.get("expires_at"), extra=b.get("extra"))


@mcp.custom_route("/v1/skills", methods=["POST"])
@_api
def api_store_skill(b: dict):
    def lst(v):
        return _csv(v) if isinstance(v, str) else (v or [])
    return write.store_skill(b["name"], b["description"], b["instructions"], b.get("domain", "general"),
                             lst(b.get("trigger_tags")), lst(b.get("examples")), b.get("external_id"))


@mcp.custom_route("/v1/search", methods=["POST"])
@_api
def api_search(b: dict):
    return retrieve.search(b["query"], domain=b.get("domain"), limit=min(int(b.get("limit", 5)), MAX_SEARCH_LIMIT),
                           as_of=b.get("as_of"), include_history=bool(b.get("include_history")),
                           rerank=b.get("rerank"), explain=bool(b.get("explain")))


@mcp.custom_route("/v1/recall", methods=["POST"])
@_api
def api_recall(b: dict):
    """For prompt hooks: the few memories relevant to this prompt that the core profile doesn't already say."""
    prompt = (b.get("prompt") or "")[:2000]
    if len(prompt.split()) < 3:
        return {"context": "", "items": []}
    core = profile.build()
    cutoff = settings.get("profile_min_importance")

    def in_profile(h):  # mirror profile.build()'s selection so the session-start block isn't repeated per prompt
        if h["relation"]:
            return h["importance"] >= cutoff
        return h["importance"] >= cutoff and h["domain"] == "identity"

    hits = retrieve.search(prompt, limit=settings.get("recall_max") + 3, rerank=True)
    keep = [h for h in hits if h["score"] >= settings.get("recall_min_score") and not in_profile(h)]
    keep = keep[:settings.get("recall_max")]
    with db.tx() as c:
        db.log_event(c, "recall", agent=b.get("agent"), n=len(keep))
    lines = [f"- {h['content']} ({h['domain']}, {h['observed_at'][:10]})" for h in keep]
    return {"context": ("Relevant memory:\n" + "\n".join(lines)) if lines else "",
            "items": [{"id": h["id"], "content": h["content"], "score": h["score"]} for h in keep]}


@mcp.custom_route("/v1/profile", methods=["GET"])
@_api
def api_profile(_b: dict):
    return {"profile": profile.build()}


@mcp.custom_route("/v1/stats", methods=["GET"])
@_api
def api_stats(_b: dict):
    return stats()


def main():
    db.connect()
    embeddings.warm()
    if settings.get("web_ui"):
        import web  # only imported (and routes only registered) when the console is enabled
        web.register(mcp)
    if settings.get("consolidate"):
        import consolidate
        consolidate.start_scheduler()
    mcp.run(transport="http", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", 8084)),
            show_banner=False)


if __name__ == "__main__":
    main()
