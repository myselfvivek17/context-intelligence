"""Jev backend: TypeSafe's hosted System One model (POST /v1/systemone). Stdlib HTTP, short timeout."""
import json
import os
import urllib.error
import urllib.request

URL = os.environ.get("TYPESAFE_URL", "https://api.typesafe.ai/v1/systemone")
MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")


def available() -> bool:
    return bool(os.environ.get("TYPESAFE_API_KEY"))


def predict(state: dict, questions: dict) -> dict:
    body = json.dumps({"model": MODEL, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)["answers"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Jev HTTP {e.code}: {e.read()[:200]!r}") from e
