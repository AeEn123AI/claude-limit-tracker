#!/usr/bin/env python3
"""Claude Code status line command that logs plan-limit utilization samples.

Claude Code pipes a JSON blob to your status line command after every response. For
Pro/Max subscribers it includes `rate_limits.five_hour` / `rate_limits.seven_day`
({used_percentage, resets_at}). This script appends a line to a small log whenever a
value changes, then prints a status line (yours, if you wrap an existing command).

Only the window name, reset time, percentage and a timestamp are written; nothing
about your session, project or prompts.

Setup (~/.claude/settings.json):
  "statusLine": {"type": "command", "command": "python3 /path/to/collector/statusline_sampler.py"}
To keep an existing status line command, wrap it:
  "command": "python3 /path/to/collector/statusline_sampler.py --then 'your existing command'"

Log: $LIMIT_TRACKER_SAMPLES or ~/.local/state/claude-limit-tracker/samples.jsonl
"""
import argparse
import json
import os
import subprocess
import sys
import time

DEFAULT_LOG = "~/.local/state/claude-limit-tracker/samples.jsonl"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=os.environ.get("LIMIT_TRACKER_SAMPLES", DEFAULT_LOG))
    ap.add_argument("--then", help="existing status line command to run with the same input")
    a = ap.parse_args()
    raw = sys.stdin.read()
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        data = {}

    rl = data.get("rate_limits") if isinstance(data, dict) else None
    shown = []
    if isinstance(rl, dict):
        log = os.path.expanduser(a.log)
        state_path = log + ".last"
        try:
            with open(state_path, encoding="utf-8") as fh:
                last = json.load(fh)
        except (OSError, ValueError):
            last = {}
        now = round(time.time(), 3)
        lines = []
        for key, w in sorted(rl.items()):
            if not isinstance(w, dict) or w.get("used_percentage") is None or w.get("resets_at") is None:
                continue
            if key == "spend_limit":
                continue
            sig = [w["resets_at"], w["used_percentage"]]
            if last.get(key) != sig:
                lines.append(json.dumps({"ts": now, "window": key, "resets_at": w["resets_at"],
                                         "used_percentage": w["used_percentage"]}))
                last[key] = sig
            shown.append("%s %d%%" % ({"five_hour": "5h", "seven_day": "week"}.get(key, key),
                                      round(float(w["used_percentage"]))))
        if lines:
            try:
                os.makedirs(os.path.dirname(log), exist_ok=True)
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
                tmp = state_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(last, fh)
                os.replace(tmp, state_path)
            except OSError:
                pass  # never break the status line over logging

    if a.then:
        r = subprocess.run(a.then, shell=True, input=raw, text=True, capture_output=True)
        sys.stdout.write(r.stdout)
    else:
        model = ((data.get("model") or {}).get("display_name") or "") if isinstance(data, dict) else ""
        print(" · ".join(x for x in [model] + shown if x))


if __name__ == "__main__":
    main()
