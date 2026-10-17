"""Run the flight-recorder proof of concept.

    python run_demo.py                       # all scenarios, red-team scripted agent
    python run_demo.py --agent live          # all scenarios, Gemma 4 decides what to do
    python run_demo.py -s refund-overreach --t2 human

Use a Python environment with torch and transformers (for example the one you used to test d3-flash), and set
D3_DIR if the checkpoint is not at ~/Models/d3-flash/model.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from harness.agent import run_live, run_scripted  # noqa: E402
from harness.gateway import Gateway  # noqa: E402
from harness.judges import LLMJudge, LogprobLLMJudge, configured_judges  # noqa: E402
from harness.narrate import banner, narrate  # noqa: E402
from harness.policy import Policy  # noqa: E402
from harness.recorder import Recorder, verify  # noqa: E402
from harness.scrub import Paraphraser  # noqa: E402
from harness.sandbox import Sandbox  # noqa: E402
from harness.scenarios import SCENARIOS, seed  # noqa: E402

D3_DIR = Path(os.environ.get("D3_DIR", Path.home() / "Models" / "d3-flash" / "model"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--scenario", action="append", choices=list(SCENARIOS), help="repeatable; default all")
    ap.add_argument("--agent", choices=["scripted", "live"], default="scripted")
    ap.add_argument("--model", default="gemma4:12b-it-qat", help="Ollama model for --agent live")
    ap.add_argument("--t2", choices=["hold", "human"], default="hold",
                    help="hold: escalations wait, nothing runs; human: ask on the terminal")
    ap.add_argument("--out", default=None, help="run directory (default runs/<timestamp>-<agent>)")
    ap.add_argument("--narrate", action="store_true", help="print every step live, for demos")
    ap.add_argument("--judge2", default=None, metavar="logprob:MODEL",
                    help="a second routing judge from another lineage (Ollama model read via token probabilities)")
    ap.add_argument("--paraphrase", default=None, metavar="OLLAMA_MODEL",
                    help="trusted third-lineage model that paraphrases agent prose before judges read it")
    ap.add_argument("--canary-rate", type=float, default=0.0,
                    help="share of judged actions that also get a hidden known-bad test case")
    ap.add_argument("--shadow-judge", default=None, metavar="OLLAMA_MODEL",
                    help="comma-separated shadow judges, e.g. gemma4:12b-it-qat or logprob:gemma4:12b-it-qat; recorded, never routed")
    args = ap.parse_args()

    run_root = Path(args.out).resolve() if args.out else HERE / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.agent}"
    run_root.mkdir(parents=True, exist_ok=True)
    policy = Policy(HERE / "packs")
    rec = Recorder(run_root / "flight-recorder.jsonl", HERE / ".keys" / "recorder.key")
    judges = configured_judges(D3_DIR)
    if args.judge2:
        judges.append(LogprobLLMJudge(args.judge2.split(":", 1)[1] if args.judge2.startswith("logprob:") else args.judge2))
    paraphraser = Paraphraser(args.paraphrase) if args.paraphrase else None
    canaries = []
    if args.canary_rate > 0:
        bench = HERE / "runs" / "benchmark-cases.json"
        canaries = [c for c in json.loads(bench.read_text()) if c["label"] == "block"] if bench.exists() else []
    print(f"Loading judges: {', '.join(j.id for j in judges)}")
    t = time.perf_counter()
    for j in judges:
        getattr(j, "load", lambda: None)()
    print(f"  ready in {time.perf_counter() - t:.1f}s")

    agent_id = args.model if args.agent == "live" else "red-team-script"
    shadow = []
    for spec in (args.shadow_judge.split(",") if args.shadow_judge else []):
        kind, _, model = spec.partition(":") if spec.startswith(("logprob:", "verbal:")) else ("verbal", "", spec)
        shadow.append(LogprobLLMJudge(model) if kind == "logprob" else LLMJudge(model))
    versions = {"policy": policy.version, "agent": agent_id, "recorder_key": rec.key_id,
                **{f"judge.{i}": j.version for i, j in enumerate(judges)},
                **{f"shadow.{i}": j.version for i, j in enumerate(shadow)}}
    results = []
    for name in args.scenario or list(SCENARIOS):
        sc = SCENARIOS[name]
        session = f"{name}-{time.strftime('%H%M%S')}"
        run_dir = run_root / name
        sb = Sandbox(run_dir / "world", seed())
        task = {"name": name, "summary": sc["summary"], "instruction": sc["instruction"],
                "ticket_id": sc.get("ticket_id")}
        gw = Gateway(run_dir=run_dir, session=session, task=task, sandbox=sb, policy=policy, judges=judges,
                     recorder=rec, versions=versions, t2_mode=args.t2, shadow_judges=shadow,
                     paraphraser=paraphraser, canaries=canaries, canary_rate=args.canary_rate)
        if args.narrate:
            gw.on_event = narrate
        gw.log("session_start", {"task": task, "agent_mode": args.agent},
               **{"gen_ai.agent.name": "acme-ops", "gen_ai.request.model": agent_id})
        if args.narrate:
            banner(sc["title"], sc["instruction"])
        else:
            print(f"\n▶ {sc['title']} [{args.agent}]")
        t = time.perf_counter()
        if args.agent == "live":
            run_live(gw, task, args.model)
        else:
            run_scripted(gw, sc["script"])
        dt = time.perf_counter() - t
        gw.log("session_end", {"outcomes": gw.outcomes, "seconds": round(dt, 1)})
        for o in ([] if args.narrate else gw.outcomes):
            v = "; ".join(f"p={x['p_violation']:.2f} ({x['latency_ms']} ms)" for x in o.get("verdicts", []))
            extra = o.get("rules") or o.get("why") or ""
            print(f"   {o['outcome'].upper():9} {o['tool']:13} {v} {extra}")
        if not gw.outcomes and not args.narrate:
            print("   (agent proposed no side-effecting actions)")
        results.append({"scenario": name, "title": sc["title"], "summary": sc["summary"],
                        "red_team": sc.get("red_team", ""), "expect": sc["expect"],
                        "agent": args.agent, "session": session, "seconds": round(dt, 1),
                        "outcomes": gw.outcomes})

    check = verify(rec.path, rec.key)
    print(f"\nRecorder: {check['entries']} entries, chain {'intact' if check['ok'] else 'BROKEN: ' + check['reason']}")
    (run_root / "summary.json").write_text(json.dumps(
        {"run": run_root.name, "agent": args.agent, "versions": versions, "verify": check, "results": results},
        indent=2, ensure_ascii=False))
    print(f"Run saved to {os.path.relpath(run_root)}")


if __name__ == "__main__":
    main()
