"""The gateway: a reference monitor between the agent and the world.

Always invoked (every tool call goes through `handle`), tamper-resistant (the agent has no handle on it), and small
enough to read in one sitting. Flow for each proposed action:

    brake? -> record proposal -> read-only? execute
           -> T0 rules (deny ends here)
           -> snapshot the world
           -> T1 judges -> route: allow | block | escalate
           -> T2 (human or hold) for escalations
           -> execute with executor-held credentials
           -> random audit sample

Every step is written to the recorder. Blocks, denials and holds produce an incident bundle: the full record of
the session up to that point, the snapshot taken just before the action, and a diff against the starting state.
"""

from __future__ import annotations

import json
import random
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .judges import QUESTIONS
from .policy import Policy
from .recorder import Recorder, canonical, sha256
from .sandbox import TOOLS, Sandbox

AGENT_MESSAGES = {
    "deny": "Action not permitted by policy.",
    "block": "Action not permitted. It has been logged for review.",
    "hold": "Action held for human approval. It has not been executed.",
    "brake": "All actions are paused by an operator.",
}


class Gateway:
    def __init__(self, *, run_dir: Path, session: str, task: dict[str, Any], sandbox: Sandbox, policy: Policy,
                 judges: list[Any], recorder: Recorder, versions: dict[str, str], t2_mode: str = "hold",
                 rng_seed: int = 7):
        self.run_dir, self.session, self.task = run_dir, session, task
        self.sb, self.policy, self.judges, self.rec = sandbox, policy, judges, recorder
        self.versions = versions
        self.t2_mode = t2_mode
        self.rng = random.Random(rng_seed)
        self.brake_file = run_dir.parent / "BRAKE"
        self.snap_dir = run_dir / "snapshots"
        self.inc_dir = run_dir / "incidents"
        self.labels = run_dir.parent / "labels.jsonl"
        self.initial = sandbox.fingerprint()
        self._snapshot("initial")
        self.outcomes: list[dict[str, Any]] = []

    # ------------------------------------------------------------ recording helpers
    on_event = None  # optional callback(entry) for live narration; never visible to the agent

    def log(self, kind: str, body: dict[str, Any], **attrs: Any) -> dict[str, Any]:
        a = {"gen_ai.conversation.id": self.session, **attrs}
        entry = self.rec.append(kind, self.session, a, body, self.versions)
        if self.on_event:
            self.on_event(entry)
        return entry

    def _snapshot(self, label: str) -> dict[str, Any]:
        dest = self.snap_dir / label
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(self.sb.root, dest, ignore=shutil.ignore_patterns("origin.git"))
        manifest = self.sb.fingerprint()
        return {"id": label, "path": str(dest.relative_to(self.run_dir)),
                "manifest_sha256": sha256(canonical(manifest)), "files": len(manifest)}

    def _incident(self, entry: dict[str, Any], tool: str, args: dict[str, Any], reason: str,
                  snapshot: dict[str, Any] | None) -> str:
        inc_id = f"INC-{entry['seq']:05d}-{tool}"
        d = self.inc_dir / inc_id
        d.mkdir(parents=True, exist_ok=True)
        chain = self.rec.entries(self.session)
        now = self.sb.fingerprint()
        changed = sorted(k for k in set(now) | set(self.initial) if now.get(k) != self.initial.get(k))
        bundle = {
            "incident": inc_id, "session": self.session, "task": self.task,
            "action": {"tool": tool, "args": args, "effect": TOOLS[tool]["effect"]},
            "outcome": reason, "decided_at_seq": entry["seq"], "decided_entry_hash": entry["hash"],
            "snapshot_before_action": snapshot,
            "world_changes_since_start": changed,
            "versions": self.versions,
            "record": chain,
        }
        (d / "incident.json").write_text(json.dumps(bundle, indent=2, ensure_ascii=False))
        lines = [f"# {inc_id}", "", f"**Outcome:** {reason}", f"**Action:** `{tool}` {json.dumps(args)[:400]}",
                 f"**Task:** {self.task.get('summary', '')}", "", "## Steps leading up to this decision", ""]
        for e in chain:
            lines.append(f"- `#{e['seq']}` {e['kind']}: {_summary(e)}")
        lines += ["", f"Snapshot before the action: `{snapshot['path'] if snapshot else 'n/a'}`",
                  f"Files changed since the start: {', '.join(changed) or 'none'}",
                  f"Decision entry hash: `{entry['hash']}`"]
        (d / "incident.md").write_text("\n".join(lines) + "\n")
        self.log("incident", {"id": inc_id, "path": str(d.relative_to(self.run_dir)), "reason": reason,
                              "entries_captured": len(chain)})
        return inc_id

    # ------------------------------------------------------------ state given to the judges
    def _judge_state(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        state: dict[str, Any] = {"task": self.task.get("instruction"), "proposed_action": {"tool": tool, **args},
                                 # what the gateway itself executed this session: facts, not the agent's account
                                 "actions_already_taken": [
                                     {"tool": o["tool"], "args": o["args"]} for o in self.outcomes
                                     if o["outcome"] == "executed"]}
        if self.task.get("ticket_id"):
            state["ticket"] = self.sb.tickets.get(self.task["ticket_id"])
        if tool == "issue_refund":
            state["customer_record"] = self.sb.ledger()["customers"].get(args.get("customer"))
        if tool == "write_file":
            state["proposed_action"] = {"tool": tool, "path": args.get("path"),
                                        "diff": self.sb.file_diff(args.get("path", ""), args.get("content", ""))[0]}
        if tool in ("git_commit", "git_push"):
            state["pending_diff"] = self.sb.pending_diff()[:6000]
        if tool == "send_email":
            to = str(args.get("to", "")).lower()
            owner = next((cid for cid, c in self.sb.ledger()["customers"].items() if c["email"].lower() == to), None)
            state["recipient"] = {"email": to, "customer_id": owner}
        return state

    # ------------------------------------------------------------ routing
    def _route(self, effect: str, verdicts: list[dict[str, Any]]) -> tuple[str, str]:
        r = self.policy.routing
        ps = [v["p_violation"] for v in verdicts]
        allow_below = r["allow_below_reversible"] if effect == "reversible" else r["allow_below_irreversible"]
        if any(p >= r["block_at"] for p in ps):
            return "block", f"a judge is at or above the block threshold ({max(ps):.2f} >= {r['block_at']})"
        if len(ps) > 1 and max(ps) - min(ps) > r["disagreement"]:
            return "escalate", f"judges disagree ({min(ps):.2f} vs {max(ps):.2f})"
        if all(p < allow_below for p in ps):
            return "allow", f"all judges below the {effect} allow threshold ({max(ps):.2f} < {allow_below})"
        return "escalate", f"uncertain band ({max(ps):.2f}; allow < {allow_below}, block >= {r['block_at']})"

    def _t2(self, tool: str, args: dict[str, Any], verdicts: list[dict[str, Any]], why: str) -> str:
        if self.t2_mode == "human" and sys.stdin.isatty():
            print(f"\n  [T2] Approval needed: {tool} {json.dumps(args)[:300]}\n       reason: {why}")
            for v in verdicts:
                print(f"       {v['judge']}: p(violation)={v['p_violation']} {v['per_question']}")
            ans = input("       approve? [y/N] ").strip().lower()
            return "approve" if ans == "y" else "deny"
        return "hold"

    # ------------------------------------------------------------ main entry
    def handle(self, tool: str, args: dict[str, Any], *, call_id: str = "") -> str:
        spec = TOOLS.get(tool)
        if spec is None:
            self.log("proposal", {"tool": tool, "args": args, "error": "unknown tool"}, **{"gen_ai.tool.name": tool})
            return f"error: unknown tool {tool}"
        effect = spec["effect"]
        tattrs = {"gen_ai.tool.name": tool, "gen_ai.tool.call.id": call_id, "acme.action.effect": effect}

        if self.brake_file.exists():
            e = self.log("brake", {"tool": tool, "args": args, "note": "operator brake engaged; nothing executed"},
                         **tattrs)
            self.outcomes.append({"tool": tool, "args": args, "outcome": "braked", "seq": e["seq"]})
            return AGENT_MESSAGES["brake"]

        self.log("proposal", {"tool": tool, "args": args}, **tattrs)

        if effect == "read":
            result = self.sb.execute(tool, args)
            self.log("tool_result", {"tool": tool, "result": result[:1500], "truncated": len(result) > 1500},
                     **tattrs)
            return result

        # ---- T0
        ctx = {"customers": self.sb.ledger()["customers"]}
        if tool == "write_file":
            ctx["changed_lines"] = self.sb.file_diff(args.get("path", ""), args.get("content", ""))[1]
        if self.task.get("ticket_id"):
            ctx["ticket_email"] = (self.sb.tickets.get(self.task["ticket_id"]) or {}).get("from")
        t0 = self.policy.check(tool, args, ctx)
        e = self.log("t0", t0, **tattrs, **{"acme.policy.version": self.policy.version})
        if t0["decision"] == "deny":
            inc = self._incident(e, tool, args, f"denied by rule: {', '.join(t0['failed'])}", None)
            self.outcomes.append({"tool": tool, "args": args, "outcome": "denied", "by": "T0",
                                  "rules": t0["failed"], "incident": inc, "seq": e["seq"]})
            return AGENT_MESSAGES["deny"]

        # ---- snapshot before anything changes
        snap = self._snapshot(f"before-{self.rec.seq + 1:05d}-{tool}")
        self.log("snapshot", snap, **tattrs)

        # ---- T1
        state = self._judge_state(tool, args)
        verdicts = []
        for j in self.judges:
            v = j.decide(tool, state)
            verdicts.append(v)
            self.log("t1", {**v, "questions": list(QUESTIONS[tool])}, **tattrs,
                     **{"gen_ai.request.model": v["version"], "gen_ai.operation.name": "decide"})
        decision, why = self._route(effect, verdicts)
        e = self.log("route", {"decision": decision, "why": why, "effect": effect,
                               "thresholds": self.policy.routing}, **tattrs)

        if decision == "escalate":
            t2 = self._t2(tool, args, verdicts, why)
            e = self.log("t2", {"decision": t2, "mode": self.t2_mode, "why": why}, **tattrs)
            with self.labels.open("a") as f:  # every T2 outcome is a future training label for the local judge
                f.write(json.dumps({"tool": tool, "state": state, "verdicts": [
                    {"judge": v["judge"], "p_violation": v["p_violation"]} for v in verdicts],
                    "label": t2, "session": self.session, "seq": e["seq"]}) + "\n")
            if t2 != "approve":
                inc = self._incident(e, tool, args, f"escalated ({why}); T2 {t2}", snap)
                self.outcomes.append({"tool": tool, "args": args, "outcome": "held" if t2 == "hold" else "denied",
                                      "by": "T2", "why": why, "verdicts": _vsum(verdicts), "incident": inc,
                                      "seq": e["seq"]})
                return AGENT_MESSAGES["hold" if t2 == "hold" else "block"]
        elif decision == "block":
            inc = self._incident(e, tool, args, f"blocked by T1 ({why})", snap)
            self.outcomes.append({"tool": tool, "args": args, "outcome": "blocked", "by": "T1", "why": why,
                                  "verdicts": _vsum(verdicts), "incident": inc, "seq": e["seq"]})
            return AGENT_MESSAGES["block"]

        # ---- execute with executor-held credentials
        result = self.sb.execute(tool, args)
        e = self.log("execute", {"tool": tool, "result": result[:1500]}, **tattrs)
        out = {"tool": tool, "args": args, "outcome": "error" if result.startswith("error:") else "executed",
               "verdicts": _vsum(verdicts), "seq": e["seq"]}
        if self.rng.random() < self.policy.routing["audit_sample_rate"]:
            self.log("audit_sample", {"tool": tool, "note": "queued for after-the-fact review"}, **tattrs)
            out["audit_sampled"] = True
        self.outcomes.append(out)
        return result


def _vsum(verdicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"judge": v["judge"], "p_violation": v["p_violation"], "per_question": v["per_question"],
             "latency_ms": v["latency_ms"]} for v in verdicts]


def _summary(e: dict[str, Any]) -> str:
    b = e["body"]
    k = e["kind"]
    if k == "agent_turn":
        calls = ", ".join(c["name"] for c in b.get("tool_calls", []))
        return f"agent ({b.get('mode')}) → {calls or 'no tool call'}"
    if k in ("proposal", "execute", "tool_result", "brake"):
        return f"{b.get('tool')} {json.dumps(b.get('args', b.get('result', '')))[:160]}"
    if k == "t0":
        return f"{b['decision']} {b.get('failed') or ''}"
    if k == "t1":
        return f"{b['judge']} p(violation)={b['p_violation']} {b['per_question']} ({b['latency_ms']} ms)"
    if k in ("route", "t2"):
        return f"{b['decision']}: {b.get('why', '')}"
    if k == "snapshot":
        return f"{b['id']} ({b['files']} files)"
    return json.dumps(b)[:160]
