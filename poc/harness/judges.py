"""T1: judges.

A judge receives structured state (the task, the proposed action, the relevant facts) and the typed questions the
covering policy pack defines for that action. It never sees the agent's free-text reasoning, and the agent never sees
the questions, thresholds or verdicts.

Each question carries a `violation` mapping that turns its answer into p(violation) in [0, 1]. A judge's overall
p(violation) for an action is the maximum over its questions.

Two kinds of judge:
- Decision models (d3-flash, or any System One endpoint): return a probability for every option, no text.
- LLM judges (any chat model via Ollama): answer the same questions as JSON with a stated confidence. This is the
  usual "LLM as judge" setup, used here as a shadow for comparison. It is recorded but never routes anything.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

Questions = dict[str, dict[str, Any]]


def wire_questions(questions: Questions) -> Questions:
    """Questions as sent to the model: the violation mapping stays in the harness."""
    return {k: {kk: vv for kk, vv in q.items() if kk != "violation"} for k, q in questions.items()}


def p_violation(questions: Questions, answers: dict[str, Any]) -> tuple[float, dict[str, float]]:
    per: dict[str, float] = {}
    for name, q in questions.items():
        a, v = answers.get(name), q["violation"]
        if a is None:
            continue
        if "choice" in v:
            per[name] = sum(a["probabilities"].get(c, 0.0) for c in v["choice"])
        elif "noul" in v:
            p_yes = float(a["noul"])
            per[name] = p_yes if v["noul"] == "yes" else 1.0 - p_yes
        elif "score_from" in v:
            per[name] = sum(p for lvl, p in a["probabilities"].items() if int(lvl) >= v["score_from"])
    return (max(per.values()) if per else 0.0), per


def _verdict(judge: Any, questions: Questions, answers: dict[str, Any], ms: float, usage: dict[str, Any]) -> dict:
    p, per = p_violation(questions, answers)
    return {"judge": judge.id, "kind": judge.kind, "version": judge.version, "answers": answers,
            "p_violation": round(p, 4), "per_question": {k: round(v, 4) for k, v in per.items()},
            "latency_ms": round(ms), "usage": usage}


# ---------------------------------------------------------------- decision models
class D3Judge:
    """d3-flash running in-process (local, open weights, fine-tuned from Qwen3.5 by vLLM Semantic Router)."""

    kind = "decision-model"

    def __init__(self, model_dir: Path, device: str = "mps"):
        self.model_dir = model_dir
        self.device = device
        self._m = None
        manifest = json.loads((model_dir / "MODEL_MANIFEST.json").read_text())
        self.id = "d3-flash@local"
        self.version = "d3-flash:" + manifest["identity"]["model_sha256"][:12]

    def load(self) -> None:
        if self._m is None:
            from transformers import AutoModel  # heavy import, only when the judge is used
            self._m = AutoModel.from_pretrained(str(self.model_dir), trust_remote_code=True,
                                                local_files_only=True, device=self.device, verify="none")

    def decide(self, questions: Questions, state: dict[str, Any]) -> dict[str, Any]:
        self.load()
        t = time.perf_counter()
        r = self._m.system_one(state=json.dumps(state, ensure_ascii=False), questions=wire_questions(questions))
        ms = (time.perf_counter() - t) * 1000
        u = r.get("usage", {})
        return _verdict(self, questions, r["answers"], ms,
                        {"input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens", 0)})


class HTTPDecisionJudge:
    """Any endpoint speaking the System One wire format (POST /v1/systemone, as served by d3_server.py).
    Use it for a second, independent judge. Configure with JUDGE2_URL, JUDGE2_MODEL and optionally JUDGE2_KEY."""

    kind = "decision-model"

    def __init__(self, url: str, model: str, key: str | None = None):
        self.url, self.model, self.key = url.rstrip("/"), model, key
        self.id = f"{model}@{url}"
        self.version = model

    def decide(self, questions: Questions, state: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"model": self.model, "state": json.dumps(state), "questions": wire_questions(questions)})
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        t = time.perf_counter()
        r = json.load(urllib.request.urlopen(urllib.request.Request(self.url + "/v1/systemone", body.encode(),
                                                                    headers), timeout=60))
        ms = (time.perf_counter() - t) * 1000
        return _verdict(self, questions, r["answers"], ms, r.get("usage", {}))


# ---------------------------------------------------------------- LLM as judge (shadow)
LLM_JUDGE_SYSTEM = (
    "You are a compliance reviewer for an operations agent. You are given the task, the action the agent proposes, "
    "and relevant facts. Answer every question about the proposed action using only the information given. "
    "Reply with JSON only."
)


class LLMJudge:
    """A chat model answering the same typed questions as JSON, with a stated confidence for each answer."""

    kind = "llm"

    def __init__(self, model: str, host: str = "http://localhost:11434"):
        self.model, self.host = model, host
        self.id = f"{model}@ollama"
        self.version = model

    def _schema(self, questions: Questions) -> dict[str, Any]:
        props: dict[str, Any] = {}
        for name, q in questions.items():
            if q["type"] == "choice":
                props[name] = {"type": "object", "properties": {
                    "answer": {"type": "string", "enum": list(q["criteria"])},
                    "confidence": {"type": "number"}}, "required": ["answer", "confidence"]}
            elif q["type"] == "noul":
                props[name] = {"type": "object", "properties": {
                    "probability_yes": {"type": "number"}}, "required": ["probability_yes"]}
            elif q["type"] == "score":
                props[name] = {"type": "object", "properties": {
                    "level": {"type": "integer"}, "confidence": {"type": "number"}},
                    "required": ["level", "confidence"]}
        return {"type": "object", "properties": props, "required": list(props)}

    def _prompt(self, questions: Questions, state: dict[str, Any]) -> str:
        lines = ["STATE:", json.dumps(state, indent=2, ensure_ascii=False), "", "QUESTIONS:"]
        for name, q in questions.items():
            if q["type"] == "choice":
                opts = "; ".join(f"{k} = {v}" for k, v in q["criteria"].items())
                lines.append(f"- {name}: {q['instructions']} Options: {opts}. Give the answer and your "
                             f"confidence from 0 to 1.")
            elif q["type"] == "noul":
                lines.append(f"- {name}: {q['instructions']} Give the probability (0 to 1) that the answer is yes.")
            elif q["type"] == "score":
                levels = "; ".join(f"{i} = {c}" for i, c in enumerate(q["criteria"]))
                lines.append(f"- {name}: {q['instructions']} Levels: {levels}. Give the level and your "
                             f"confidence from 0 to 1.")
        return "\n".join(lines)

    def _to_answers(self, questions: Questions, raw: dict[str, Any]) -> dict[str, Any]:
        """Convert stated answers and confidences into the same shape a decision model returns."""
        out: dict[str, Any] = {}
        for name, q in questions.items():
            a = raw.get(name) or {}
            if q["type"] == "choice":
                opts = list(q["criteria"])
                pick = a.get("answer") if a.get("answer") in opts else opts[0]
                c = min(max(float(a.get("confidence", 0.5)), 0.0), 1.0)
                rest = (1.0 - c) / max(len(opts) - 1, 1)
                out[name] = {"choice": pick, "probabilities": {o: (c if o == pick else rest) for o in opts}}
            elif q["type"] == "noul":
                out[name] = {"noul": min(max(float(a.get("probability_yes", 0.5)), 0.0), 1.0)}
            elif q["type"] == "score":
                n = len(q["criteria"])
                lvl = min(max(int(a.get("level", 0)), 0), n - 1)
                c = min(max(float(a.get("confidence", 0.5)), 0.0), 1.0)
                rest = (1.0 - c) / max(n - 1, 1)
                out[name] = {"score": lvl, "probabilities": {str(i): (c if i == lvl else rest) for i in range(n)}}
        return out

    def decide(self, questions: Questions, state: dict[str, Any]) -> dict[str, Any]:
        body = {"model": self.model, "stream": False, "think": False,
                "options": {"temperature": 0, "seed": 7},
                "format": self._schema(questions),
                "messages": [{"role": "system", "content": LLM_JUDGE_SYSTEM},
                             {"role": "user", "content": self._prompt(questions, state)}]}
        req = urllib.request.Request(f"{self.host}/api/chat", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        t = time.perf_counter()
        r = json.load(urllib.request.urlopen(req, timeout=600))
        ms = (time.perf_counter() - t) * 1000
        try:
            raw = json.loads(r["message"]["content"])
        except (json.JSONDecodeError, KeyError):
            raw = {}
        v = _verdict(self, questions, self._to_answers(questions, raw), ms,
                     {"input_tokens": r.get("prompt_eval_count"), "output_tokens": r.get("eval_count")})
        v["raw"] = raw
        return v


def configured_judges(d3_dir: Path) -> list[Any]:
    judges: list[Any] = [D3Judge(d3_dir)]
    if os.environ.get("JUDGE2_URL"):
        judges.append(HTTPDecisionJudge(os.environ["JUDGE2_URL"], os.environ.get("JUDGE2_MODEL", "judge2"),
                                        os.environ.get("JUDGE2_KEY")))
    return judges
