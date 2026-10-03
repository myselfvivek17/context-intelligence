"""Laya backend (opt-in): a judge_worker.py subprocess started on first use, killed after an idle period."""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time

import settings

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_last_used = 0.0


def available() -> bool:
    return importlib.util.find_spec("laya") is not None


def _start() -> subprocess.Popen:
    global _proc
    _proc = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), "judge_worker.py")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    ready = json.loads(_proc.stdout.readline() or "{}")
    if not ready.get("ready"):
        _proc.kill()
        _proc = None
        raise RuntimeError("Laya worker failed to start")
    threading.Thread(target=_reaper, daemon=True, name="laya-reaper").start()
    return _proc


def _reaper():
    global _proc
    while True:
        time.sleep(30)
        with _lock:
            if _proc is None:
                return
            if time.time() - _last_used > settings.get("judge_idle_exit_s"):
                _proc.stdin.close()
                _proc.wait(timeout=30)
                _proc = None
                return


def predict(state: dict, questions: dict) -> dict:
    """One request at a time (concurrent predicts hung Laya in PersonalAI)."""
    global _last_used, _proc
    with _lock:
        proc = _proc if _proc and _proc.poll() is None else _start()
        _last_used = time.time()
        proc.stdin.write(json.dumps({"state": state, "questions": questions}) + "\n")
        line = proc.stdout.readline()
        if not line:
            _proc = None
            raise RuntimeError("Laya worker died")
        out = json.loads(line)
        if "error" in out:
            raise RuntimeError(out["error"])
        return out["answers"]


def status() -> dict:
    running = _proc is not None and _proc.poll() is None
    return {"installed": available(), "running": running,
            "idle_s": round(time.time() - _last_used) if running else None}
