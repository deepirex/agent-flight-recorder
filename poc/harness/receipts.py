"""Decision receipts: one short, readable record per consequential action, built before it runs.

The receipt is assembled by code from the recorder, never written by a model: a model's summary can be wrong, or
steered by the same text that fooled the agent. It lists every step in the session that led to the action, the
rules and judges that cleared it, and the hash of the last log entry it covers, so anyone can check it against the
chained log. The executor refuses a consequential action that has no matching receipt.
"""

from __future__ import annotations

import json
from typing import Any

from .recorder import canonical, sha256


def action_digest(tool: str, args: dict[str, Any]) -> str:
    return sha256(canonical({"tool": tool, "args": args}))


def _short(value: Any, n: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return _short(line.strip(), 110)
    return ""


def _step(e: dict[str, Any]) -> dict[str, Any] | None:
    """One compact line per log entry that explains the path to the action; plumbing entries are skipped."""
    k, b = e["kind"], e["body"]
    line = None
    if k == "agent_turn" and b.get("reasoning"):
        line = "agent's stated reasoning: " + _short(b["reasoning"], 200)
    elif k == "proposal":
        line = f"agent proposed {b.get('tool')}({_short(b.get('args', {}), 100)})"
    elif k == "tool_result":
        line = f"{b.get('tool')} returned: {_first_line(b.get('result', ''))}"
    elif k == "t0":
        line = "rules: " + ("pass" if b["decision"] == "pass" else f"{b['decision']} {', '.join(b.get('failed', []))}")
    elif k == "t1":
        line = f"judge {b['judge']}: p(violation) {b['p_violation']}"
    elif k == "t1_unavailable":
        line = f"judge {b['judge']}: unavailable"
    elif k == "route":
        line = f"route: {b['decision']} ({_short(b.get('why', ''), 90)})"
    elif k == "t2":
        line = f"person: {b['decision']}"
    elif k == "execute":
        line = f"executed {b.get('tool')}: {_first_line(b.get('result', ''))}"
    elif k in ("brake", "incident"):
        line = f"{k}: {_short(b.get('reason') or b.get('note') or '', 100)}"
    if line is None:
        return None
    return {"seq": e["seq"], "step": line, "entry_hash": e["hash"]}


def build(entries: list[dict[str, Any]], *, session: str, task: dict[str, Any], tool: str, args: dict[str, Any],
          effect: str, decision: str, why: str, pack: str) -> dict[str, Any]:
    """Compact every step of the session so far into a receipt for the action about to run."""
    steps = [s for s in (_step(e) for e in entries) if s]
    checks = {
        "rules": next((e["body"] for e in reversed(entries) if e["kind"] == "t0"), None),
        "judges": [{"judge": e["body"]["judge"], "p_violation": e["body"]["p_violation"],
                    "per_question": e["body"].get("per_question", {})}
                   for e in entries[_last_proposal(entries):] if e["kind"] == "t1"],
        "judges_unavailable": [e["body"]["judge"] for e in entries[_last_proposal(entries):]
                               if e["kind"] == "t1_unavailable"],
        "person": next((e["body"]["decision"] for e in reversed(entries[_last_proposal(entries):])
                        if e["kind"] == "t2"), None),
    }
    last = entries[-1] if entries else None
    return {
        "action": {"tool": tool, "args": args, "effect": effect},
        "action_sha256": action_digest(tool, args),
        "task": task.get("summary") or task.get("instruction", ""),
        "decision": decision,
        "why": why,
        "policy_pack": pack,
        "steps": steps,
        "checks": checks,
        "covers": {"session": session, "from_seq": entries[0]["seq"] if entries else None,
                   "to_seq": last["seq"] if last else None, "to_hash": last["hash"] if last else None,
                   "entries": len(entries)},
        "built_by": "harness (deterministic, from the recorder; no model wrote this)",
    }


def _last_proposal(entries: list[dict[str, Any]]) -> int:
    for i in range(len(entries) - 1, -1, -1):
        if entries[i]["kind"] == "proposal":
            return i
    return 0


def markdown(rid: str, r: dict[str, Any], entry_hash: str) -> str:
    a = r["action"]
    lines = [f"# {rid}", "",
             f"**Action:** `{a['tool']}` {_short(a['args'], 300)}  ",
             f"**Effect:** {a['effect']} · **Decision:** {r['decision']} ({r['why']})  ",
             f"**Task:** {r['task']}  ",
             f"**Policy:** {r['policy_pack']}", "",
             f"## How it got here ({len(r['steps'])} steps, log entries #{r['covers']['from_seq']}–#{r['covers']['to_seq']})", ""]
    lines += [f"- `#{s['seq']}` {s['step']}" for s in r["steps"]]
    c = r["checks"]
    lines += ["", "## Checks on this action", ""]
    if c["rules"]:
        lines.append(f"- Rules: {c['rules']['decision']}" + (f" ({', '.join(c['rules'].get('failed', []))})"
                                                             if c['rules'].get('failed') else ""))
    for j in c["judges"]:
        lines.append(f"- Judge {j['judge']}: p(violation) {j['p_violation']}")
    for j in c["judges_unavailable"]:
        lines.append(f"- Judge {j}: unavailable (never counted as approval)")
    if c["person"]:
        lines.append(f"- Person: {c['person']}")
    lines += ["", "## Evidence", "",
              f"Covers {r['covers']['entries']} entries of session `{r['covers']['session']}`, ending at entry "
              f"#{r['covers']['to_seq']} (hash `{r['covers']['to_hash']}`).",
              f"Action digest: `{r['action_sha256']}`. This receipt is itself log entry hash `{entry_hash}`.",
              "Built by the harness from the recorder, not written by a model. Check it with "
              "`python3 verify.py <run>/flight-recorder.jsonl --chain-only`.", ""]
    return "\n".join(lines)
