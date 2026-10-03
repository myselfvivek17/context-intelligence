"""Core profile: a deterministic, dated summary of what's currently true and important (no LLM).
Stable between ledger changes, so clients can inject it once per session and keep it prompt-cacheable."""
import db
import settings

_cache: tuple[int, str] | None = None


def _ledger_version() -> int:
    r = db.one("SELECT max(id) AS v FROM events WHERE op IN "
               "('store','update','supersede','delete','review','archive','merge')")
    return r["v"] or 0


def build() -> str:
    global _cache
    ver = _ledger_version()
    if _cache and _cache[0] == ver:
        return _cache[1]
    cutoff = settings.get("profile_min_importance")
    cap = settings.get("profile_max_chars")
    facts = db.query(
        "SELECT e.name AS subject, m.relation, m.content, m.object_text, substr(m.valid_from, 1, 10) AS since "
        "FROM memories m JOIN entities e ON e.id = m.subject_id "
        "WHERE m.status = 'active' AND m.relation IS NOT NULL AND m.importance >= ? "
        "ORDER BY e.name COLLATE NOCASE, m.relation", (cutoff,))
    notes = db.query(
        "SELECT domain, content, substr(observed_at, 1, 10) AS since FROM memories "
        "WHERE status = 'active' AND relation IS NULL AND importance >= ? AND domain IN ('identity', 'projects') "
        "ORDER BY importance DESC, observed_at DESC", (cutoff,))
    lines = [f"# Core memory (as of {db.now()[:10]})"]
    subject = None
    for f in facts:
        if f["subject"] != subject:
            subject = f["subject"]
            lines.append(f"\n## {subject}")
        value = f["object_text"] or f["content"]
        lines.append(f"- {f['relation'].replace('_', ' ')}: {value} (since {f['since']})")
    if notes:
        lines.append("\n## Notes")
        lines += [f"- [{n['domain']}] {n['content']} ({n['since']})" for n in notes]
    out, size = [], 0
    for line in lines:
        if size + len(line) + 1 > cap:
            out.append("- … (truncated; search memory for more)")
            break
        out.append(line)
        size += len(line) + 1
    text = "\n".join(out)
    _cache = (ver, text)
    return text
