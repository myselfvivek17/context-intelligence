"""Self-check for the ledger, retrieval and review flows. Run: python server/test_ledger.py
Uses a temp SQLite file and the real embedding/rerank models; the judge is disabled or faked."""
import os
import sqlite3
import tempfile

os.environ["JUDGE_BACKEND"] = "none"
os.environ["CONSOLIDATE"] = "false"

import db  # noqa: E402
import graph  # noqa: E402
import judge  # noqa: E402
import profile  # noqa: E402
import retrieve  # noqa: E402
import write  # noqa: E402


def contents(hits):
    return [h["content"] for h in hits]


def main():
    tmp = tempfile.mkdtemp()
    db.connect(os.path.join(tmp, "t.db"))

    # 1. Same key, new value -> old retired, search serves only the new one; as_of sees the old one.
    a = write.store_memory("Vivek's primary database is Postgres", "identity", "preference", importance=0.9,
                           subject="Vivek", relation="primary database", object="Postgres",
                           observed_at="2026-01-01T00:00:00+00:00")
    b = write.store_memory("Vivek's primary database is MySQL", "identity", "preference", importance=0.9,
                           subject="vivek", relation="Primary Database", object="MySQL",
                           observed_at="2026-06-01T00:00:00+00:00")
    assert b["superseded"][0]["id"] == a["id"], b
    hits = retrieve.search("what database does Vivek use", limit=5)
    assert "Vivek's primary database is MySQL" in contents(hits), contents(hits)
    assert "Vivek's primary database is Postgres" not in contents(hits), contents(hits)
    past = retrieve.search("what database does Vivek use", as_of="2026-03-01T00:00:00+00:00")
    assert contents(past)[0] == "Vivek's primary database is Postgres", contents(past)
    hist = retrieve.search("Vivek database", include_history=True, limit=10)
    assert {"Vivek's primary database is MySQL", "Vivek's primary database is Postgres"} <= set(contents(hist))

    # 1b. Restating the current value is a no-op, not a new version.
    again = write.store_memory("Vivek's primary database is MySQL", "identity", "preference", subject="Vivek",
                               relation="primary database", object="mysql")
    assert again["status"] == "unchanged" and again["id"] == b["id"], again

    # 2. The invariant is enforced by the database itself, not just the code path.
    vid = write.find_entity("Vivek")["id"]
    try:
        with db.tx() as c:
            c.execute("INSERT INTO memories(uid, domain, subject_id, relation, content, observed_at, valid_from, "
                      "recorded_at) VALUES ('x', 'identity', ?, 'primary_database', 'dup', 'n', 'n', 'n')", (vid,))
        raise AssertionError("second active value for one key was accepted")
    except sqlite3.IntegrityError:
        pass

    # 3. A late-arriving older observation becomes history, not the current value.
    c3 = write.store_memory("Vivek's primary database was SQLite", "identity", "fact", subject="Vivek",
                            relation="primary database", object="SQLite", observed_at="2025-06-01T00:00:00+00:00")
    assert c3["status"] == "superseded", c3
    assert contents(retrieve.search("Vivek primary database", limit=1))[0] == "Vivek's primary database is MySQL"
    q = "what database does Vivek use"
    assert contents(retrieve.search(q, as_of="2025-09-01T00:00:00+00:00")) == ["Vivek's primary database was SQLite"]
    assert contents(retrieve.search(q, as_of="2026-03-01T00:00:00+00:00")) == ["Vivek's primary database is Postgres"]

    # 4. Multi-hop: Alpha -> Beta -> Gamma. A query naming only Alpha reaches the Beta->Gamma fact via PPR.
    write.store_memory("Gamma is the payments ledger", "projects", "fact", subject="Gamma", relation="role",
                       object="payments ledger")
    write.store_memory("Beta reads from Gamma", "projects", "fact", subject="Beta", relation="reads from",
                       object="Gamma")
    write.store_memory("Alpha depends on Beta", "projects", "fact", subject="Alpha", relation="depends on",
                       object="Beta")
    hits = retrieve.search("Alpha", limit=10, explain=True, rerank=False)
    via_graph = [h for h in hits if h["content"] == "Beta reads from Gamma"]
    assert via_graph and "graph" in via_graph[0]["explain"]["ranks"], [(h["content"], h["explain"]) for h in hits]
    prof = graph.entity_profile(write.find_entity("Alpha")["id"], depth=2)
    assert {n["name"] for n in prof["neighbors"]} == {"Beta", "Gamma"}, prof["neighbors"]

    # 5. Object backfill: a value that later becomes an entity gets linked.
    write.store_memory("The NAS runs TrueNAS", "projects", "fact", subject="NAS", relation="os", object="TrueNAS")
    write.store_memory("TrueNAS version is 25.04", "projects", "fact", subject="TrueNAS", relation="version",
                       object="25.04")
    row = db.one("SELECT object_entity_id FROM memories WHERE content = 'The NAS runs TrueNAS'")
    assert row["object_entity_id"] == write.find_entity("TrueNAS")["id"]

    # 6. external_id makes imports idempotent.
    e1 = write.store_memory("Saw a movie with friends", "diary", "diary", external_id="entry-1")
    e2 = write.store_memory("Saw a movie with friends, it was fun", "diary", "diary", external_id="entry-1")
    assert e2 == {"id": e1["id"], "status": "updated"}, e2
    assert db.one("SELECT count(*) AS n FROM memories WHERE external_id = 'entry-1'")["n"] == 1

    # 7. Judge-uncertain update (renamed relation) -> pending + review; approval adopts the canonical key.
    write.store_memory("Home server RAM is 8GB", "projects", "fact", subject="home server", relation="ram",
                       object="8GB")
    old = db.one("SELECT id FROM memories WHERE content = 'Home server RAM is 8GB'")
    real_choice = judge.choice
    judge.choice = lambda state, instr, criteria: (f"m{old['id']}", 0.65) if f"m{old['id']}" in criteria else None
    try:
        r = write.store_memory("The home server now has 16GB of memory", "projects", "fact", subject="home server",
                               relation="memory size", object="16GB")
    finally:
        judge.choice = real_choice
    assert r["status"] == "pending" and r["needs_review"], r
    rv = db.one("SELECT id FROM reviews WHERE status = 'open' AND kind = 'supersede'")
    write.resolve_review(rv["id"], approve=True)
    now_active = db.query("SELECT content, relation FROM memories WHERE subject_id = ? AND status = 'active'",
                          (write.find_entity("home server")["id"],))
    assert [(x["content"], x["relation"]) for x in now_active] == [("The home server now has 16GB of memory", "ram")]

    # 8. Judge-confident update auto-supersedes and keeps the canonical relation.
    cur = db.one("SELECT id FROM memories WHERE content = 'The home server now has 16GB of memory'")
    judge.choice = lambda state, instr, criteria: (f"m{cur['id']}", 0.93) if f"m{cur['id']}" in criteria else None
    try:
        r = write.store_memory("Home server upgraded to 32GB RAM", "projects", "fact", subject="home server",
                               relation="installed memory", object="32GB")
    finally:
        judge.choice = real_choice
    assert r["key"] == "home server.ram" and r["superseded"], r

    # 9. Free-text contradiction (faked judge) hides the old fact pending review; reject restores it.
    write.store_memory("Deploys go through Portainer stacks", "code", "fact")
    judge.noul = lambda state, instr: 0.95 if "Portainer" in state.get("a", "") else 0.1
    try:
        r = write.store_memory("Deploys now go through docker compose on the CLI", "code", "fact")
    finally:
        judge.noul = lambda state, instr: None
    assert r["conflict"]["content"] == "Deploys go through Portainer stacks", r
    assert "Deploys go through Portainer stacks" not in contents(retrieve.search("how do deploys work", limit=10))
    rv = db.one("SELECT id FROM reviews WHERE status = 'open' AND kind = 'conflict'")
    write.resolve_review(rv["id"], approve=False)
    assert "Deploys go through Portainer stacks" in contents(retrieve.search("how do deploys work", limit=10))

    # 10. Skills upsert by name; delete is soft for memories.
    write.store_skill("deploy", "Deploy to server", "1. build", trigger_tags=["docker"])
    s2 = write.store_skill("deploy", "Deploy to the home server", "1. build 2. run")
    assert s2["version"] == 2 and len(retrieve.list_skills()) == 1
    assert retrieve.find_skill("how do I deploy")[0]["name"] == "deploy"
    gid = write.store_memory("Temporary note", "general", "note")["id"]
    assert write.delete_memory(gid) and "Temporary note" not in contents(retrieve.search("temporary note"))

    # 11. Profile: important current facts, dated, no superseded values.
    p = profile.build()
    assert "primary database: MySQL" in p and "Postgres" not in p, p

    print("test_ledger: all checks passed")


if __name__ == "__main__":
    main()
