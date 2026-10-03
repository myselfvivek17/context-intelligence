"""Evaluation: stale-fact rate, static recall and multi-hop recall, v1-style baseline vs v2.

    python eval/run_eval.py [--seed 7] [--keys 60]

Three systems on the same seeded data (temp SQLite files; nothing real is touched):
  baseline   v1-equivalent: every statement stored as a note, retrieval = pure cosine top-k (bge-small)
  v2-notes   v2 retrieval (vector + BM25 + graph, adaptive RRF, rerank) but no keys, so no supersession
  v2-keyed   v2 with subject/relation on changeable facts (the writer supplies the key — "keyed" condition)

Not covered here (needs an LLM writer): the "realistic" condition where an agent decides on its own whether
to key a statement. That is the number to quote for real use; this harness gives the keyed upper bound and
isolates what retrieval alone can and cannot do.
"""
import argparse
import os
import random
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
os.environ.setdefault("JUDGE_BACKEND", "none")
os.environ.setdefault("CONSOLIDATE", "false")

import db  # noqa: E402
import embeddings  # noqa: E402
import graph  # noqa: E402
import retrieve  # noqa: E402
import settings  # noqa: E402
import write  # noqa: E402

PEOPLE = ["Asha", "Bruno", "Chen", "Dara", "Elif", "Farid", "Gita", "Hugo", "Ines", "Jonas", "Kofi", "Lena",
          "Mateo", "Nadia", "Omar", "Priya", "Quinn", "Rafa", "Sofia", "Tariq"]
RELATIONS = {
    "editor": ["VS Code", "Zed", "Neovim", "Helix", "Sublime Text", "Emacs", "IntelliJ"],
    "primary database": ["Postgres", "MySQL", "SQLite", "MongoDB", "DuckDB", "Redis", "CockroachDB"],
    "operating system": ["Ubuntu", "Fedora", "macOS", "Windows 11", "Arch Linux", "Debian", "NixOS"],
    "city": ["Lisbon", "Berlin", "Pune", "Toronto", "Nairobi", "Osaka", "Bogota"],
    "main language": ["Python", "Go", "Rust", "TypeScript", "Kotlin", "Elixir", "Java"],
    "deploy tool": ["Portainer", "docker compose", "Kubernetes", "Nomad", "Ansible", "Coolify", "Kamal"],
}
TEMPLATES = {  # (first statement, later statement) phrasings — later ones deliberately vary wording
    "editor": ["{s}'s editor is {v}.", "{s} switched editors and now writes code in {v}.", "{s} moved to {v} for editing."],
    "primary database": ["{s}'s primary database is {v}.", "{s} migrated their main database to {v}.", "{s} now keeps data in {v}."],
    "operating system": ["{s} runs {v} on their machine.", "{s} reinstalled their laptop with {v}.", "{s}'s OS is now {v}."],
    "city": ["{s} lives in {v}.", "{s} relocated to {v}.", "{s} moved house to {v}."],
    "main language": ["{s} mostly codes in {v}.", "{s}'s main language is now {v}.", "{s} switched their day-to-day language to {v}."],
    "deploy tool": ["{s} deploys with {v}.", "{s} now ships deployments through {v}.", "{s} changed deploy tooling to {v}."],
}
QUESTIONS = {
    "editor": "Which editor does {s} use?", "primary database": "What database does {s} use?",
    "operating system": "What operating system does {s} run?", "city": "Where does {s} live?",
    "main language": "What programming language does {s} mostly use?", "deploy tool": "How does {s} deploy?",
}
STATIC = [("The {p} project's CI runs on GitHub Actions with a nightly matrix build.", "Where does {p} CI run?"),
          ("The {p} service exposes metrics on port 9{n}0 for Prometheus.", "Which port has {p} metrics?"),
          ("The {p} team holds its retro every second Thursday.", "When is the {p} retro?"),
          ("Backups for {p} go to a Backblaze B2 bucket named {p}-bk.", "Where are {p} backups stored?")]
PROJECTS = ["atlas", "beacon", "cobalt", "drift", "ember", "fjord", "garnet", "harbor", "iris", "juniper"]


def build_dataset(seed: int, n_keys: int):
    rnd = random.Random(seed)
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    keys = rnd.sample([(s, r) for s in PEOPLE for r in RELATIONS], n_keys)
    evolving = []
    for s, r in keys:
        values = rnd.sample(RELATIONS[r], rnd.randint(2, 4))
        t = base + timedelta(days=rnd.randint(0, 60))
        versions = []
        for i, v in enumerate(values):
            versions.append({"text": rnd.choice(TEMPLATES[r][1:] if i else TEMPLATES[r][:1]).format(s=s, v=v),
                             "value": v, "at": t.isoformat()})
            t += timedelta(days=rnd.randint(20, 120))
        evolving.append({"subject": s, "relation": r, "versions": versions, "question": QUESTIONS[r].format(s=s)})
    static = [{"text": tpl.format(p=p, n=i), "question": q.format(p=p)}
              for i, p in enumerate(PROJECTS) for tpl, q in STATIC]
    hops = []
    for i in range(10):
        a, b, c = f"svc-{chr(97 + i)}lpha", f"svc-{chr(97 + i)}eta", f"host-{chr(97 + i)}ox"
        hops.append({"facts": [(f"{a} depends on {b}.", a, "depends on", b), (f"{b} runs on {c}.", b, "runs on", c)],
                     "question": f"What machine is {a} ultimately running on?", "answer": f"{b} runs on {c}."})
    return evolving, static, hops


def fresh_db():
    db.connect(os.path.join(tempfile.mkdtemp(), "eval.db"))
    graph.invalidate()
    settings._cache = None


def load(evolving, static, hops, keyed: bool):
    fresh_db()
    events = [(v["at"], e, v) for e in evolving for v in e["versions"]]
    for at, e, v in sorted(events, key=lambda x: x[0]):
        if keyed:
            write.store_memory(v["text"], "identity", "fact", subject=e["subject"], relation=e["relation"],
                               object=v["value"], observed_at=at)
        else:
            write.store_memory(v["text"], "identity", "fact", observed_at=at)
    for s in static:
        write.store_memory(s["text"], "projects", "fact")
    for h in hops:
        for text, subj, rel, obj in h["facts"]:
            if keyed:
                write.store_memory(text, "projects", "fact", subject=subj, relation=rel, object=obj)
            else:
                write.store_memory(text, "projects", "fact")
        if keyed:  # second pass so objects that became entities link up (writer order shouldn't matter)
            pass


def baseline_search(query: str, k: int) -> list[str]:
    q = embeddings.embed_query(query)
    return [r["content"] for r in db.query("SELECT content FROM memories WHERE status='active' "
                                           "ORDER BY vec_distance_cosine(embedding, ?) LIMIT ?", (q, k))]


def v2_search(query: str, k: int) -> list[str]:
    return [h["content"] for h in retrieve.search(query, limit=k, touch=False)]


def score(search, evolving, static, hops):
    lat, stale, correct, miss = [], 0, 0, 0
    for e in evolving:
        t = time.perf_counter()
        top = search(e["question"], 1)
        lat.append((time.perf_counter() - t) * 1000)
        latest = e["versions"][-1]["value"]
        olds = {v["value"] for v in e["versions"][:-1]}
        text = top[0] if top else ""
        if latest in text:
            correct += 1
        elif any(o in text for o in olds):
            stale += 1
        else:
            miss += 1
    hits = 0
    for s in static:
        t = time.perf_counter()
        hits += s["text"] in search(s["question"], 5)
        lat.append((time.perf_counter() - t) * 1000)
    mh = sum(h["answer"] in search(h["question"], 5) for h in hops)
    n = len(evolving)
    return {"current@1": correct / n, "stale@1": stale / n, "miss@1": miss / n,
            "static_recall@5": hits / len(static), "multihop_recall@5": mh / len(hops),
            "p50_ms": statistics.median(lat), "p95_ms": sorted(lat)[int(len(lat) * 0.95) - 1]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--keys", type=int, default=60)
    a = ap.parse_args()
    evolving, static, hops = build_dataset(a.seed, a.keys)
    embeddings.warm()
    rows = []
    load(evolving, static, hops, keyed=False)
    rows.append(("baseline (v1-style vector only)", score(baseline_search, evolving, static, hops)))
    rows.append(("v2 retrieval, unkeyed notes", score(v2_search, evolving, static, hops)))
    load(evolving, static, hops, keyed=True)
    rows.append(("v2 keyed ledger", score(v2_search, evolving, static, hops)))
    if os.environ.get("ABLATE"):
        for name, overrides in [("  ablation: rerank replaces order", {"rerank_blend": False}),
                                ("  ablation: no rerank", {"rerank": False}),
                                ("  ablation: no graph channel", {"w_graph": 0.0}),
                                ("  ablation: fixed fusion weights", {"adaptive_fusion": False})]:
            settings._cache = None
            settings.get("rrf_k")
            settings._cache.update(overrides)
            rows.append((name, score(v2_search, evolving, static, hops)))
        settings._cache = None
    versions = sum(len(e["versions"]) for e in evolving)
    print(f"\n{a.keys} changing facts ({versions} statements over time), {len(static)} static facts, "
          f"{len(hops)} two-hop chains; seed {a.seed}\n")
    cols = ["current@1", "stale@1", "miss@1", "static_recall@5", "multihop_recall@5", "p50_ms", "p95_ms"]
    print("| system | " + " | ".join(cols) + " |")
    print("|---|" + "---|" * len(cols))
    for name, r in rows:
        print(f"| {name} | " + " | ".join(f"{r[c]:.0f}" if c.endswith("ms") else f"{r[c]:.0%}" for c in cols) + " |")


if __name__ == "__main__":
    main()
