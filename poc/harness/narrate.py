"""Live, human-readable narration of the recorder stream, for demos. Reads entries; changes nothing."""

from __future__ import annotations

import sys
from typing import Any

C = {"dim": "\033[2m", "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m", "orange": "\033[38;5;208m",
     "bold": "\033[1m", "cyan": "\033[36m", "reset": "\033[0m"}

READ_VERBS = {"read_ticket": "reads ticket", "lookup_customer": "looks up customer", "list_files": "lists files",
              "read_file": "reads", "run_tests": "runs the tests"}


def _c(color: str, text: str) -> str:
    return f"{C[color]}{text}{C['reset']}"


def _short(args: dict[str, Any], n: int = 90) -> str:
    parts = []
    for k, v in args.items():
        v = str(v).replace("\n", " ")
        parts.append(f"{k}={v[:48] + '…' if len(v) > 48 else v}")
    s = ", ".join(parts)
    return s[:n] + "…" if len(s) > n else s


def narrate(e: dict[str, Any]) -> None:
    k, b = e["kind"], e["body"]
    seq = _c("dim", f"#{e['seq']:<4}")
    out = None
    if k == "agent_turn":
        for c in b.get("tool_calls", []):
            name, args = c["name"], c.get("arguments") or {}
            if name in READ_VERBS:
                arg = next(iter(args.values()), "") if args else ""
                out = f"{seq} {_c('cyan', 'agent')}  {READ_VERBS[name]} {arg}"
            else:
                out = f"{seq} {_c('cyan', 'agent')}  proposes {_c('bold', name)}({_short(args)})"
        if not b.get("tool_calls") and b.get("content"):
            out = f"{seq} {_c('cyan', 'agent')}  done: {b['content'].strip()[:110]}"
    elif k == "t0":
        if b["decision"] == "deny":
            out = f"{seq}   {_c('red', 'RULES   ✗ deny')}  {', '.join(b['failed'])}"
        else:
            out = f"{seq}   {_c('dim', 'RULES   ✓ pass')}"
    elif k == "snapshot":
        out = f"{seq}   {_c('dim', 'SNAPSHOT ' + b['id'])}"
    elif k == "t1":
        name, val = max(b["per_question"].items(), key=lambda kv: kv[1])
        p = _c("bold", "%.2f" % b["p_violation"])
        detail = _c("dim", "%s %.2f · %.1fs" % (name, val, b["latency_ms"] / 1000))
        out = f"{seq}   JUDGE   d3-flash  p(violation) {p}  {detail}"
    elif k == "route":
        col = {"allow": "green", "block": "red", "escalate": "yellow"}[b["decision"]]
        out = f"{seq}   {_c(col, 'ROUTE   ' + b['decision'].upper())}"
    elif k == "t2":
        out = f"{seq}   {_c('yellow', 'HELD    for a human; nothing executed')}"
    elif k == "execute":
        out = f"{seq}   {_c('green', '✓ EXECUTED')}  {b['result'][:80]}"
    elif k == "incident":
        out = f"{seq}   {_c('orange', '⚑ INCIDENT ' + b['id'])}  {_c('dim', str(b['entries_captured']) + ' entries + snapshot saved')}"
    elif k == "brake":
        out = f"{seq}   {_c('red', '■ BRAKE')}  {b['tool']} refused"
    if out:
        print(out, flush=True)
        sys.stdout.flush()


def banner(title: str, summary: str) -> None:
    print(f"\n{_c('orange', '▌')} {_c('bold', title.upper())}\n{_c('orange', '▌')} {_c('dim', summary)}", flush=True)

