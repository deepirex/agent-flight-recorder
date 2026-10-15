"""Compare the routing judge (d3-flash) with the shadow LLM judge, decision by decision, from a run's record.

    python3 bench.py runs/v3-live runs/v3-scripted

Both judges saw identical state and identical questions for every decision. Only the routing judge's verdict was
used; the shadow verdict is what an LLM-as-judge setup would have done. Writes bench.json and prints a summary.
"""

from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path


def load(run: Path) -> list[dict]:
    rows, pending = [], {}
    for line in (run / "flight-recorder.jsonl").read_text().splitlines():
        e = json.loads(line)
        k, b = e["kind"], e["body"]
        key = (e["session"], e["attrs"].get("gen_ai.tool.call.id"), e["attrs"].get("gen_ai.tool.name"))
        if k == "proposal":
            pending[key] = {"run": run.name, "scenario": e["session"].rsplit("-", 1)[0], "tool": b["tool"],
                            "seq": e["seq"], "args": b.get("args")}
        elif k == "t1" and key in pending:
            pending[key]["judge"] = {"id": b["judge"], "p": b["p_violation"], "ms": b["latency_ms"],
                                     "per_question": b["per_question"], "usage": b.get("usage", {})}
        elif k == "t1_shadow" and key in pending:
            pending[key]["shadow"] = {"id": b["judge"], "p": b["p_violation"], "ms": b["latency_ms"],
                                      "per_question": b["per_question"], "usage": b.get("usage", {}),
                                      "would_route": b["would_route"]}
        elif k == "route" and key in pending:
            r = pending.pop(key)
            r["route"] = b["decision"]
            if "judge" in r and "shadow" in r:
                rows.append(r)
    return rows


def main() -> None:
    runs = [Path(a) for a in sys.argv[1:]] or [Path("runs/v3-live"), Path("runs/v3-scripted")]
    rows = [r for run in runs for r in load(run)]
    if not rows:
        sys.exit("no paired decisions found (was the run made with --shadow-judge?)")
    agree = sum(r["route"] == r["shadow"]["would_route"] for r in rows)
    d_ms = [r["judge"]["ms"] for r in rows]
    s_ms = [r["shadow"]["ms"] for r in rows]
    d_in = [r["judge"]["usage"].get("input_tokens") or 0 for r in rows]
    s_in = [r["shadow"]["usage"].get("input_tokens") or 0 for r in rows]
    s_out = [r["shadow"]["usage"].get("output_tokens") or 0 for r in rows]
    extreme = sum(1 for r in rows for v in r["shadow"]["per_question"].values() if v in (0.0, 1.0))
    total_q = sum(len(r["shadow"]["per_question"]) for r in rows)
    d_extreme = sum(1 for r in rows for v in r["judge"]["per_question"].values() if v in (0.0, 1.0))
    summary = {
        "decisions": len(rows),
        "same_route": agree,
        "routing_judge": rows[0]["judge"]["id"], "shadow_judge": rows[0]["shadow"]["id"],
        "latency_ms_median": {"routing": st.median(d_ms), "shadow": st.median(s_ms)},
        "tokens_median": {"routing_in": st.median(d_in), "routing_out": 0,
                          "shadow_in": st.median(s_in), "shadow_out": st.median(s_out)},
        "answers_at_exactly_0_or_1": {"routing": f"{d_extreme}/{total_q}", "shadow": f"{extreme}/{total_q}"},
    }
    print(json.dumps(summary, indent=2))
    print(f"\n{'run':13} {'scenario':17} {'tool':13} {'route':9} {'d3 p':>6} {'llm p':>6} {'llm would':10} "
          f"{'d3 ms':>7} {'llm ms':>7}")
    for r in rows:
        mark = "" if r["route"] == r["shadow"]["would_route"] else "  ← differs"
        print(f"{r['run']:13} {r['scenario'][:17]:17} {r['tool']:13} {r['route']:9} {r['judge']['p']:6.2f} "
              f"{r['shadow']['p']:6.2f} {r['shadow']['would_route']:10} {r['judge']['ms']:7} "
              f"{r['shadow']['ms']:7}{mark}")
    Path("bench.json").write_text(json.dumps({"summary": summary, "decisions": rows}, indent=2))


if __name__ == "__main__":
    main()
