"""Optional web console at /ui. Imported (and routes registered) only when the `web_ui` setting is on.

Auth: the login form takes the MCP key; the server answers with an HttpOnly, SameSite=Strict cookie holding
an HMAC-signed expiry (stdlib only). Every /ui/api route needs it; state-changing calls also need a
same-origin Origin header.
"""
import hashlib
import hmac
import json
import os
import pathlib
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import anyio
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

import db
import graph
import judge
import retrieve
import settings
import write

UI_DIR = pathlib.Path(__file__).parent / "ui"
COOKIE = "ci_session"
TTL_S = 7 * 86400
_KEY = os.environ.get("MCP_API_KEY") or ""
_SECRET = hmac.new(_KEY.encode(), b"ci-web-console-v1", hashlib.sha256).digest()


def _sign(exp: int) -> str:
    return f"{exp}.{hmac.new(_SECRET, str(exp).encode(), hashlib.sha256).hexdigest()}"


def _valid(token: str | None) -> bool:
    if not _KEY:
        return True  # no key configured (local dev): console is open, like the API
    try:
        exp, sig = token.split(".", 1)
        return int(exp) > time.time() and hmac.compare_digest(sig, _sign(int(exp)).split(".", 1)[1])
    except (AttributeError, ValueError):
        return False


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    return origin is None or origin.split("://", 1)[-1] == request.headers.get("host")


def _api(fn, write_op: bool = False):
    async def handler(request: Request):
        if not _valid(request.cookies.get(COOKIE)):
            return JSONResponse({"error": "Sign in again."}, status_code=401)
        if write_op and not _same_origin(request):
            return JSONResponse({"error": "Cross-origin request refused."}, status_code=403)
        try:
            body = await request.json() if request.method in ("POST", "PUT", "DELETE") else {}
            params = {**dict(request.query_params), **request.path_params, **(body or {})}
            return JSONResponse(await anyio.to_thread.run_sync(lambda: fn(params)))
        except (ValueError, KeyError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    return handler


# ── data endpoints ────────────────────────────────────────────────────────────────────────────────

def _rss_mb() -> float | None:
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None


def overview(_p):
    since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    days = [(datetime.now(timezone.utc) - timedelta(days=i)).date().isoformat() for i in range(13, -1, -1)]
    reads, writes = defaultdict(int), defaultdict(int)
    agents = defaultdict(lambda: {"reads": 0, "writes": 0})
    lat = defaultdict(list)
    for r in db.query("SELECT ts, op, tool, agent, latency_ms FROM events WHERE ts >= ? AND op IN "
                      "('tool_call','recall','store')", (since,)):
        day = r["ts"][:10]
        tool = r["tool"] or r["op"]
        is_read = r["op"] == "recall" or (r["tool"] or "").startswith(("search", "find", "list", "profile",
                                                                         "entity", "timeline"))
        is_write = r["op"] == "store"
        if is_read:
            reads[day] += 1
            agents[r["agent"] or "unknown"]["reads"] += 1
        if is_write:
            writes[day] += 1
            agents[r["agent"] or "unknown"]["writes"] += 1
        if r["latency_ms"] is not None and r["op"] == "tool_call":
            lat[tool].append(r["latency_ms"])
    latency = {t: {"n": len(v), "p50": statistics.median(v),
                   "p95": sorted(v)[max(0, int(len(v) * 0.95) - 1)]} for t, v in lat.items() if v}
    counts = {r["status"]: r["n"] for r in db.query("SELECT status, count(*) AS n FROM memories GROUP BY status")}
    return {
        "days": days, "reads": [reads[d] for d in days], "writes": [writes[d] for d in days],
        "agents": dict(agents), "latency": latency, "status_counts": counts,
        "domains": [dict(r) for r in db.query("SELECT domain, count(*) AS n FROM memories WHERE status='active' "
                                              "GROUP BY domain ORDER BY n DESC")],
        "entities": db.one("SELECT count(*) AS n FROM entities")["n"],
        "facts": db.one("SELECT count(*) AS n FROM memories WHERE relation IS NOT NULL AND status='active'")["n"],
        "links": db.one("SELECT count(*) AS n FROM links")["n"],
        "skills": db.one("SELECT count(*) AS n FROM skills")["n"],
        "open_reviews": db.one("SELECT count(*) AS n FROM reviews WHERE status='open'")["n"],
        "supersessions_30d": db.one("SELECT count(*) AS n FROM events WHERE op='supersede' "
                                    "AND ts >= datetime('now','-30 days')")["n"],
        "judge": judge.status(), "rss_mb": _rss_mb(),
        "db_bytes": os.path.getsize(db.DB_PATH) if os.path.exists(db.DB_PATH) else None,
    }


def graph_data(p):
    return graph.snapshot(domain=p.get("domain") or None, include_history=p.get("history") == "1",
                          include_notes=p.get("notes") == "1")


def entity(p):
    eid = int(p["id"])
    return {**graph.entity_profile(eid, depth=2), "timeline": graph.timeline(eid)}


def memories(p):
    if p.get("q"):
        return {"items": retrieve.search(p["q"], domain=p.get("domain") or None, limit=int(p.get("limit", 20)),
                                         include_history=p.get("history") == "1", as_of=p.get("as_of") or None,
                                         rerank=None if p.get("rerank", "") == "" else p["rerank"] == "1",
                                         explain=True, touch=False),
                "next_offset": None}
    return retrieve.list_memories(p.get("domain") or None, int(p.get("limit", 50)), p.get("offset") or None,
                                  include_history=p.get("history") == "1")


def memory_detail(p):
    row = write.resolve_uid(p["id"])
    if row is None:
        raise ValueError("No such memory.")
    d = retrieve._public(row)
    if row["subject_id"]:
        d["subject"] = db.one("SELECT name FROM entities WHERE id = ?", (row["subject_id"],))["name"]
    d["related"] = [dict(r) for r in db.query(
        "SELECT m.uid, m.content, l.weight FROM links l JOIN memories m ON m.id = (CASE WHEN l.src = ? THEN l.dst "
        "ELSE l.src END) WHERE (l.src = ? OR l.dst = ?) AND m.status = 'active' ORDER BY l.weight DESC LIMIT 6",
        (row["id"], row["id"], row["id"]))]
    d["history"] = [dict(r) for r in db.query(
        "SELECT ts, op, agent, detail FROM events WHERE memory_id = ? ORDER BY ts", (row["id"],))]
    return d


def delete_memory(p):
    return {"deleted": write.delete_memory(p["id"], agent="web-console")}


def reviews(_p):
    out = []
    for r in db.query("SELECT * FROM reviews WHERE status = 'open' ORDER BY created_at DESC LIMIT 100"):
        d = db.row_dict(r)
        if r["kind"] == "alias":
            d["names"] = json.loads(r["detail"]).get("names")
        else:
            for side in ("memory_id", "other_id"):
                m = db.one("SELECT uid, content, observed_at FROM memories WHERE id = ?", (r[side],)) if r[side] else None
                d[side.replace("_id", "")] = dict(m) if m else None
        out.append(d)
    return out


def resolve(p):
    return write.resolve_review(int(p["id"]), bool(p["approve"]))


def skills(_p):
    return retrieve.list_skills()


def save_skill(p):
    lst = lambda v: [x.strip() for x in v.split(",")] if isinstance(v, str) else (v or [])
    return write.store_skill(p["name"], p["description"], p["instructions"], p.get("domain", "general"),
                             lst(p.get("trigger_tags")), lst(p.get("examples")))


def delete_skill(p):
    return {"deleted": write.delete_skill(p["name"])}


def events(p):
    op = p.get("op") or None
    rows = db.query("SELECT e.*, m.uid AS memory_uid FROM events e LEFT JOIN memories m ON m.id = e.memory_id "
                    f"{'WHERE e.op = ?' if op else ''} ORDER BY e.id DESC LIMIT ?",
                    ((op,) if op else ()) + (min(int(p.get("limit", 200)), 1000),))
    return [db.row_dict(r) for r in rows]


def get_settings(_p):
    return settings.all_settings()


def put_setting(p):
    if p.get("reset"):
        settings.reset(p["key"])
        return {"key": p["key"], "reset": True}
    return {"key": p["key"], "value": settings.put(p["key"], p["value"])}


def run_consolidation(_p):
    import consolidate
    return consolidate.run_once()


# ── wiring ────────────────────────────────────────────────────────────────────────────────────────

def register(mcp) -> None:
    @mcp.custom_route("/ui", methods=["GET"])
    async def index(request: Request):
        return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @mcp.custom_route("/ui/{asset:path}", methods=["GET"])
    async def asset(request: Request):
        name = request.path_params["asset"]
        path = (UI_DIR / name).resolve()
        if name.startswith("api/") or UI_DIR.resolve() not in path.parents or not path.is_file():
            return Response(status_code=404)
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    @mcp.custom_route("/ui/login", methods=["POST"])
    async def login(request: Request):
        if not _same_origin(request):
            return JSONResponse({"error": "Cross-origin request refused."}, status_code=403)
        body = await request.json()
        if _KEY and not hmac.compare_digest(str(body.get("key", "")), _KEY):
            await anyio.sleep(1)  # slow down guessing
            return JSONResponse({"error": "That key doesn't match this server's MCP_API_KEY."}, status_code=401)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, _sign(int(time.time()) + TTL_S), max_age=TTL_S, httponly=True, samesite="strict",
                        path="/ui", secure=request.url.scheme == "https")
        return resp

    @mcp.custom_route("/ui/logout", methods=["POST"])
    async def logout(request: Request):
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/ui")
        return resp

    routes = [
        ("/ui/api/session", ["GET"], lambda p: {"ok": True}, False),
        ("/ui/api/overview", ["GET"], overview, False),
        ("/ui/api/graph", ["GET"], graph_data, False),
        ("/ui/api/entity/{id}", ["GET"], entity, False),
        ("/ui/api/memories", ["GET"], memories, False),
        ("/ui/api/memory/{id}", ["GET"], memory_detail, False),
        ("/ui/api/memory/{id}/delete", ["POST"], delete_memory, True),
        ("/ui/api/reviews", ["GET"], reviews, False),
        ("/ui/api/reviews/{id}", ["POST"], resolve, True),
        ("/ui/api/skills", ["GET"], skills, False),
        ("/ui/api/skills", ["POST"], save_skill, True),
        ("/ui/api/skills/delete", ["POST"], delete_skill, True),
        ("/ui/api/events", ["GET"], events, False),
        ("/ui/api/settings", ["GET"], get_settings, False),
        ("/ui/api/settings", ["POST"], put_setting, True),
        ("/ui/api/consolidate", ["POST"], run_consolidation, True),
    ]
    # API routes must win over the /ui/{asset:path} catch-all, so register them first in the router.
    for path, methods, fn, is_write in routes:
        mcp.custom_route(path, methods=methods)(_api(fn, is_write))
    mcp._additional_http_routes[:] = sorted(mcp._additional_http_routes,
                                            key=lambda r: "{asset:path}" in getattr(r, "path", ""))
