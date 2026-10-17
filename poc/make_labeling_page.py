"""Build the blind labeling page: 50 decisions, no labels or verdicts shown, shuffled with a fixed seed.

    ~/Models/d3-flash/.venv/bin/python make_labeling_page.py   # writes ../labeling/blind-labeling.html
"""

from __future__ import annotations

import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "labeling" / "blind-labeling.html"


def real_decisions() -> list[dict]:
    out = []
    for run in ("v5-live", "v5-scripted"):
        seen: dict = {}
        for line in (HERE / "runs" / run / "flight-recorder.jsonl").read_text().splitlines():
            e = json.loads(line)
            if e["kind"] != "judge_input":
                continue
            scen = e["session"].rsplit("-", 1)[0]
            tool = e["attrs"]["gen_ai.tool.name"]
            n = seen[(scen, tool)] = seen.get((scen, tool), 0) + 1
            out.append({"id": f"R-{run.split('-')[1][0]}{e['seq']:03d}", "source": f"{run}#{e['seq']}",
                        "key": f"{'live' if 'live' in run else 'scripted'}/{scen}/{tool}/{n}",
                        "tool": tool, "state": e["body"]["state"]})
    return out


def bench_sample(k_per_tool: int = 6) -> list[dict]:
    cases = json.loads((HERE / "runs" / "benchmark-cases.json").read_text())
    rng = random.Random(41)
    out = []
    for tool in ("issue_refund", "send_email", "write_file", "http_post"):
        pool = [c for c in cases if c["tool"] == tool]
        for c in rng.sample(pool, min(k_per_tool, len(pool))):
            out.append({"id": c["id"], "source": "benchmark", "key": c["id"], "tool": tool, "state": c["state"]})
    return out


def main() -> None:
    items = real_decisions() + bench_sample()
    random.Random(7).shuffle(items)
    data = json.dumps([{k: v for k, v in i.items() if k != "source"} for i in items], ensure_ascii=False)
    html = (HERE / "labeling_template.html").read_text().replace("__CASES_JSON__", data.replace("</", "<\\/"))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(html)
    (OUT.parent / "case-index.json").write_text(json.dumps(
        [{"id": i["id"], "source": i["source"], "key": i["key"], "tool": i["tool"]} for i in items], indent=1))
    print(len(items), "cases ->", OUT)


if __name__ == "__main__":
    main()
