"""The gateway: a reference monitor between the agent and the world.

Always invoked (every tool call goes through `handle`), tamper-resistant (the agent has no handle on it), and small
enough to read in one sitting. Flow for each proposed action:

    brake? -> record proposal -> read-only? execute
           -> T0 rules (deny ends here)
           -> snapshot the world
           -> T1 judges -> route: allow | block | escalate
           -> T2 (human or hold) for escalations
           -> receipt: every step that led here, compacted from the log (no receipt, no action)
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

from . import receipts
from .policy import Policy
from .recorder import Recorder, canonical, sha256
from .sandbox import TOOLS, Sandbox
from .scrub import scrub_state

EFFECTS = {name: t["effect"] for name, t in TOOLS.items()}

AGENT_MESSAGES = {
    "deny": "Action not permitted by policy.",
    "block": "Action not permitted. It has been logged for review.",
    "hold": "Action held for human approval. It has not been executed.",
    "brake": "All actions are paused by an operator.",
}


class Gateway:
    def __init__(self, *, run_dir: Path, session: str, task: dict[str, Any], sandbox: Sandbox, policy: Policy,
                 judges: list[Any], recorder: Recorder, versions: dict[str, str], t2_mode: str = "hold",
                 shadow_judges: list[Any] | None = None, rng_seed: int = 7, paraphraser: Any = None,
                 canaries: list[dict[str, Any]] | None = None, canary_rate: float = 0.0):
        self.run_dir, self.session, self.task = run_dir, session, task
        self.sb, self.policy, self.judges, self.rec = sandbox, policy, judges, recorder
        self.shadow_judges = shadow_judges or []  # scored on the same state, recorded, never used for routing
        self.denials = 0  # rule denials and judge blocks this session; feeds the denial budget
        self.paraphraser = paraphraser  # optional: trusted third-lineage model that rewrites agent prose
        self.canaries = canaries or []  # known-bad judge inputs, mixed in at random; never executed
        self.canary_rate = canary_rate
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
                              "entries_captured": len(chain), "snapshot": snapshot["id"] if snapshot else None})
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
    def _route(self, effect: str, verdicts: list[dict[str, Any]], tool: str) -> tuple[str, str]:
        r = self.policy.routing_for(tool)
        ps = [v["p_violation"] for v in verdicts]
        allow_below = r["allow_below_reversible"] if effect == "reversible" else r["allow_below_irreversible"]
        if any(p >= r["block_at"] for p in ps):
            return "block", f"a judge is at or above the block threshold ({max(ps):.2f} >= {r['block_at']})"
        if len(ps) > 1 and max(ps) - min(ps) > r["disagreement"]:
            return "escalate", f"judges disagree ({min(ps):.2f} vs {max(ps):.2f})"
        if all(p < allow_below for p in ps):
            return "allow", f"all judges below the {effect} allow threshold ({max(ps):.2f} < {allow_below})"
        return "escalate", f"uncertain band ({max(ps):.2f}; allow < {allow_below}, block >= {r['block_at']})"

    def _maybe_canary(self, tool: str, questions: dict, tattrs: dict) -> None:
        """Hidden test traps: score a known-bad input of the same type with every judge. A judge that would allow
        it raises an alarm. Judges can't tell traps from real traffic; traps are never executed."""
        pool = [c for c in self.canaries if c["tool"] == tool]
        if not pool or self.rng.random() >= self.canary_rate:
            return
        trap = self.rng.choice(pool)
        results = []
        for j in self.judges + self.shadow_judges:
            try:
                v = j.decide(questions, scrub_state(tool, trap["state"], self.paraphraser))
            except Exception as err:
                results.append({"judge": _judge_id(j), "unavailable": f"{type(err).__name__}: {err}"[:200],
                                "would_route": "unavailable"})
                continue
            would, _ = self._route(EFFECTS[tool], [v], tool)
            results.append({"judge": v["judge"], "p_violation": v["p_violation"], "would_route": would})
        passed = [r["judge"] for r in results if r["would_route"] == "allow"]
        self.log("canary", {"trap": trap["id"], "results": results, "alarm": bool(passed),
                            "judges_that_passed_a_known_bad_action": passed}, **tattrs)

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
            if tool in self.policy.gated_reads:
                pack = self.policy.pack_for(tool)["label"]
                t0 = self.policy.check(tool, args, {})
                e = self.log("t0", t0, **tattrs, **{"acme.policy.pack": pack})
                if t0["decision"] == "deny":
                    self.denials += 1
                    inc = self._incident(e, tool, args, f"denied by rule: {', '.join(t0['failed'])}", None)
                    self.outcomes.append({"tool": tool, "args": args, "outcome": "denied", "by": "T0",
                                          "rules": t0["failed"], "incident": inc, "seq": e["seq"], "pack": pack})
                    return AGENT_MESSAGES["deny"]
            result = self.sb.execute(tool, args)
            self.log("tool_result", {"tool": tool, "result": result[:1500], "truncated": len(result) > 1500},
                     **tattrs)
            return result

        # ---- T0
        ctx = {"customers": self.sb.ledger()["customers"]}
        if tool == "write_file":
            ctx["changed_lines"] = self.sb.file_diff(args.get("path", ""), args.get("content", ""))[1]
        if self.task.get("ticket_id"):
            ctx["ticket"] = self.sb.tickets.get(self.task["ticket_id"]) or {}
            ctx["ticket_email"] = ctx["ticket"].get("from")
        t0 = self.policy.check(tool, args, ctx)
        pack = self.policy.pack_for(tool)["label"]
        tattrs["acme.policy.pack"] = pack
        e = self.log("t0", t0, **tattrs)
        if t0["decision"] == "deny":
            self.denials += 1
            inc = self._incident(e, tool, args, f"denied by rule: {', '.join(t0['failed'])}", None)
            self.outcomes.append({"tool": tool, "args": args, "outcome": "denied", "by": "T0",
                                  "rules": t0["failed"], "incident": inc, "seq": e["seq"], "pack": pack})
            return AGENT_MESSAGES["deny"]

        # ---- snapshot before anything changes
        snap = self._snapshot(f"before-{self.rec.seq + 1:05d}-{tool}")
        self.log("snapshot", snap, **tattrs)

        # ---- T1
        state = self._judge_state(tool, args)
        questions = self.policy.questions_for(tool)
        # judges see the agent's text scrubbed (invisible characters removed, prose optionally paraphrased by a
        # third-lineage model); the raw text stays in the proposal entry above
        state = scrub_state(tool, state, self.paraphraser)
        # what every judge sees, recorded verbatim: lets anyone audit or replay the decision with any judge
        self.log("judge_input", {"state": state, "questions": questions,
                                 "scrubbed": {"normalized": True, "paraphrased_by":
                                              getattr(self.paraphraser, "model", None)}}, **tattrs)
        self._maybe_canary(tool, questions, tattrs)
        verdicts, unavailable = [], []
        for j in self.judges:
            try:
                v = j.decide(questions, state)
            except Exception as err:  # a judge that can't answer is a recorded state, never an approval
                unavailable.append(_judge_id(j))
                self.log("t1_unavailable", {"judge": _judge_id(j), "error": f"{type(err).__name__}: {err}"[:500],
                                            "questions": list(questions)}, **tattrs)
                continue
            verdicts.append(v)
            self.log("t1", {**v, "questions": list(questions)}, **tattrs,
                     **{"gen_ai.request.model": v["version"], "gen_ai.operation.name": "decide"})
        if unavailable:
            decision, why = "escalate", f"judge unavailable ({', '.join(unavailable)}); no judgment is not approval"
            if verdicts:
                d, w = self._route(effect, verdicts, tool)
                if d == "block":  # an available judge's block still stands
                    decision, why = d, w
        elif not verdicts:  # no judge configured: rules alone never approve a consequential action
            decision, why = "escalate", "no judge configured; a person decides"
        else:
            decision, why = self._route(effect, verdicts, tool)
        if t0["decision"] == "hold" and decision == "allow":
            decision, why = "escalate", f"rule requires a person: {', '.join(t0['failed'])}"
        budget = self.policy.routing_for(tool).get("denial_budget")
        if decision == "allow" and budget is not None and self.denials >= budget:
            decision, why = "escalate", f"session denial budget spent ({self.denials} denials or blocks so far)"
        shadow = []
        for j in self.shadow_judges:  # same state, same questions; recorded for comparison only
            try:
                v = j.decide(questions, state)
            except Exception as err:  # a failing shadow judge is recorded and never affects the action
                self.log("t1_shadow_unavailable", {"judge": _judge_id(j),
                                                   "error": f"{type(err).__name__}: {err}"[:500]}, **tattrs)
                continue
            would, _ = self._route(effect, [v], tool)
            shadow.append({"judge": v["judge"], "kind": v["kind"], "p_violation": v["p_violation"],
                           "per_question": v["per_question"], "latency_ms": v["latency_ms"],
                           "usage": v["usage"], "would_route": would})
            self.log("t1_shadow", {**v, "questions": list(questions), "would_route": would,
                                   "used_for_routing": False}, **tattrs,
                     **{"gen_ai.request.model": v["version"], "gen_ai.operation.name": "decide"})
        e = self.log("route", {"decision": decision, "why": why, "effect": effect,
                               "thresholds": self.policy.routing_for(tool)}, **tattrs)

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
                                      "seq": e["seq"], "shadow": shadow, "pack": pack})
                return AGENT_MESSAGES["hold" if t2 == "hold" else "block"]
        elif decision == "block":
            self.denials += 1
            inc = self._incident(e, tool, args, f"blocked by T1 ({why})", snap)
            self.outcomes.append({"tool": tool, "args": args, "outcome": "blocked", "by": "T1", "why": why,
                                  "verdicts": _vsum(verdicts), "incident": inc, "seq": e["seq"],
                                  "shadow": shadow, "pack": pack})
            return AGENT_MESSAGES["block"]

        # ---- receipt: the steps that led here, compacted by code from the log, written before anything runs
        rid = f"RCPT-{self.rec.seq + 1:05d}-{tool}"
        final = "allow" if decision == "allow" else "approved by a person"
        receipt = receipts.build(self.rec.entries(self.session), session=self.session, task=self.task, tool=tool,
                                 args=args, effect=effect, decision=final, why=why, pack=pack)
        receipt["id"] = rid
        r_entry = self.log("receipt", receipt, **tattrs)
        rdir = self.run_dir / "receipts"
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / f"{rid}.json").write_text(json.dumps({**receipt, "entry_hash": r_entry["hash"]}, indent=2,
                                                     ensure_ascii=False))
        (rdir / f"{rid}.md").write_text(receipts.markdown(rid, receipt, r_entry["hash"]))

        # ---- execute with executor-held credentials; the executor refuses anything without a matching receipt
        result = self.sb.execute(tool, args, receipt=r_entry)
        e = self.log("execute", {"tool": tool, "result": result[:1500], "receipt": rid}, **tattrs)
        out = {"tool": tool, "args": args, "outcome": "error" if result.startswith("error:") else "executed",
               "verdicts": _vsum(verdicts), "seq": e["seq"], "shadow": shadow, "pack": pack, "receipt": rid}
        if self.rng.random() < self.policy.routing_for(tool)["audit_sample_rate"]:
            self.log("audit_sample", {"tool": tool, "note": "queued for after-the-fact review"}, **tattrs)
            out["audit_sampled"] = True
        self.outcomes.append(out)
        return result


def _judge_id(judge: Any) -> str:
    return str(getattr(judge, "id", None) or type(judge).__name__)


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
    if k in ("t1", "t1_shadow"):
        tag = " [shadow, not used for routing]" if k == "t1_shadow" else ""
        return f"{b['judge']} p(violation)={b['p_violation']} {b['per_question']} ({b['latency_ms']} ms){tag}"
    if k in ("route", "t2"):
        return f"{b['decision']}: {b.get('why', '')}"
    if k == "snapshot":
        return f"{b['id']} ({b['files']} files)"
    return json.dumps(b)[:160]
