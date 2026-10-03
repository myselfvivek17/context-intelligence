"""System One judge: calibrated choice / yes-no decisions for the write path.

Backends (setting `judge_backend`): laya (local worker subprocess) | jev (TypeSafe API) | none.
Every call returns None when no backend is usable, and callers then fall back to deterministic behaviour
(exact-key supersession only), so a missing judge never blocks a write.
"""
import logging

import settings

log = logging.getLogger("judge")


def _backend():
    # Default is Jev: the Phase 0 spike measured Laya fp32 at 2.46 GB peak RSS on the server CPU (int8 broke
    # accuracy, bf16 isn't supported), too much for a box with ~2.6 GB free. Laya stays opt-in.
    name = settings.get("judge_backend")
    if name == "laya":
        import judge_laya
        if judge_laya.available():
            return judge_laya
        name = "jev"  # fall through to the hosted fallback
    if name == "jev":
        import judge_jev
        if judge_jev.available():
            return judge_jev
    return None


def ask(state: dict, questions: dict) -> dict | None:
    """Raw System One call: {"qid": {"type": "choice"|"noul"|"score", ...}} -> answers dict, or None."""
    b = _backend()
    if b is None:
        return None
    try:
        return b.predict(state, questions)
    except Exception:
        log.exception("judge backend %s failed; falling back to deterministic path", b.__name__)
        return None


def choice(state: dict, instructions: str, criteria: dict[str, str]) -> tuple[str, float] | None:
    """Returns (picked key, probability of that pick). We gate on the pick's own probability, not the
    backend's `confidence` field: Jev's is a distribution-concentration summary and Laya's is defined
    differently (measured: Laya confidence 0.03 for a 0.60 pick), so only p(pick) means the same on both."""
    a = ask(state, {"q": {"type": "choice", "instructions": instructions, "criteria": criteria}})
    if not a:
        return None
    q = a["q"]
    p = (q.get("probabilities") or {}).get(q["choice"])
    if p is None:
        p = q.get("answer_confidence", q.get("confidence", 0.0))
    return q["choice"], float(p)


def noul(state: dict, instructions: str) -> float | None:
    a = ask(state, {"q": {"type": "noul", "instructions": instructions}})
    return None if not a else float(a["q"]["noul"])


def status() -> dict:
    out = {"backend": settings.get("judge_backend")}
    try:
        import judge_laya
        out["laya"] = judge_laya.status()
    except ImportError:
        out["laya"] = {"installed": False}
    try:
        import judge_jev
        out["jev"] = {"configured": judge_jev.available()}
    except ImportError:
        out["jev"] = {"configured": False}
    return out
