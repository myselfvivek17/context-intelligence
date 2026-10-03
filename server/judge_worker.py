"""Laya worker process: loads the model once, answers JSON lines on stdin, exits when stdin closes.
Runs out-of-process so that exiting actually returns torch's ~2.5 GB to the OS (an in-process
`del model` would not shrink the server's RSS)."""
import json
import sys

import laya

agent = laya.load("convaiinnovations/laya", subfolder="typed-decisions", device="cpu")
agent.predict({"s": "warm up"}, {"q": {"type": "noul", "instructions": "Is `s` a greeting?"}})
print(json.dumps({"ready": True}), flush=True)

for line in sys.stdin:
    try:
        req = json.loads(line)
        out = {"answers": agent.predict(req["state"], req["questions"])["answers"]}
    except Exception as e:  # report, keep serving
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out, default=float), flush=True)
