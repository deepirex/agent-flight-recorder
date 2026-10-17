"""A tiny trained judge: EmbeddingGemma-2 embeddings + one small classifier per judge question.

    PY=~/Models/embeddinggemma-2/.venv/bin/python
    $PY embed_judge.py            # trains, scores everything, writes into the benchmark and panel caches

This is the "turn a model into a decision model" idea at its cheapest: a frozen 270M-parameter text encoder from the
Gemma family, plus a logistic-regression readout per question, trained on the benchmark's training templates only.

No leakage: a benchmark case from a training template is always scored by a classifier trained without that
template (group-held-out folds), so threshold tuning and comparisons use honest scores. Cases from unseen templates,
the 26 real decisions and the collusion variants are scored by the classifier trained on all training templates.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.linear_model import LogisticRegression

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from benchmark import TEST_GROUPS, violating  # noqa: E402
from harness.policy import Policy  # noqa: E402
from panel_eval import all_inputs, load_cache, save_cache, scrubbed  # noqa: E402

MODEL_DIR = Path(os.environ.get("EMBED_DIR", Path.home() / "Models" / "embeddinggemma-2" / "model"))
NAME = "EmbeddingGemma-2, trained classifier"


def render(state: dict, question: dict) -> str:
    """The same facts a judge sees, as text, followed by the question."""
    s = state
    parts = [f"Task: {s.get('task', '')}"]
    t = s.get("ticket") or {}
    if t:
        parts.append(f"Ticket from {t.get('from', '')}: {t.get('subject', '')}. {t.get('body', '')}"
                     + (f" Internal note: {t['internal_note']}" if t.get("internal_note") else ""))
    if s.get("customer_record"):
        r = s["customer_record"]
        parts.append("Customer record: " + "; ".join(f"order {c.get('order')} ${c.get('amount')}"
                                                     f"{' (' + c['flag'] + ')' if c.get('flag') else ''}"
                                                     for c in r.get("charges", [])) + f". Notes: {r.get('notes', '')}")
    done = s.get("actions_already_taken") or []
    parts.append("Already done: " + ("; ".join(f"{d['tool']} {json.dumps(d.get('args', {}))}" for d in done) or "nothing"))
    if s.get("recipient"):
        parts.append(f"Recipient: {s['recipient'].get('email')} (customer {s['recipient'].get('customer_id')})")
    a = {k: v for k, v in (s.get("proposed_action") or {}).items()}
    parts.append("Proposed action: " + json.dumps(a, ensure_ascii=False))
    parts.append(f"Question: {question['instructions']}")
    return "\n".join(parts)


class Embedder:
    def __init__(self):
        dev = "mps" if __import__("torch").backends.mps.is_available() else "cpu"
        self.m = SentenceTransformer(str(MODEL_DIR), device=dev, model_kwargs={"torch_dtype": "bfloat16"})
        prompts = getattr(self.m, "prompts", {}) or {}
        self.prompt = next((k for k in prompts if "classif" in k.lower()), None)
        self.cache: dict[str, np.ndarray] = {}
        self.ms: list[float] = []

    def __call__(self, texts: list[str]) -> np.ndarray:
        todo = [t for t in texts if t not in self.cache]
        if todo:
            t0 = time.perf_counter()
            kw = {"prompt_name": self.prompt} if self.prompt else {}
            vecs = self.m.encode(todo, batch_size=16, normalize_embeddings=True, convert_to_numpy=True, **kw)
            self.ms.append((time.perf_counter() - t0) * 1000 / len(todo))
            for t, v in zip(todo, vecs):
                self.cache[t] = v.astype(np.float32)
        return np.stack([self.cache[t] for t in texts])


def fit(X: np.ndarray, y: np.ndarray) -> LogisticRegression | float:
    if len(set(y.tolist())) < 2:
        return float(y.mean()) if len(y) else 0.5
    return LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced").fit(X, y)


def predict(model, X: np.ndarray) -> np.ndarray:
    if isinstance(model, float):
        return np.full(len(X), model)
    return model.predict_proba(X)[:, 1]


def main() -> None:
    policy = Policy(HERE / "packs")
    bench = json.loads((HERE / "runs" / "benchmark-cases.json").read_text())
    emb = Embedder()
    print("prompt:", emb.prompt)
    train = [c for c in bench if c["group"] not in TEST_GROUPS]
    groups = sorted({c["group"] for c in train})

    # training rows per question name: (text, violates?, group)
    rows: dict[str, list[tuple[str, int, str]]] = {}
    for c in train:
        for qn, q in policy.questions_for(c["tool"]).items():
            if qn in c["truth"]:
                rows.setdefault(qn, []).append((render(c["state"], q), int(violating(q, c["truth"][qn])), c["group"]))
    final: dict[str, object] = {}
    folds: dict[str, dict[str, object]] = {}
    for qn, rs in rows.items():
        X = emb([r[0] for r in rs])
        y = np.array([r[1] for r in rs])
        g = np.array([r[2] for r in rs])
        final[qn] = fit(X, y)
        folds[qn] = {grp: fit(X[g != grp], y[g != grp]) for grp in groups}
        print(f"  {qn:22} {len(rs):3} rows, {int(y.sum())} violating")

    def score(tool: str, state: dict, held_out_group: str | None) -> dict:
        qs = policy.questions_for(tool)
        pq = {}
        for qn, q in qs.items():
            if qn not in final:
                continue
            model = folds[qn][held_out_group] if held_out_group in folds.get(qn, {}) else final[qn]
            pq[qn] = round(float(predict(model, emb([render(state, q)]))[0]), 4)
        return {"p": max(pq.values()) if pq else 0.0, "pq": pq}

    # 1) the benchmark: honest scores for every case
    bres = json.loads((HERE / "runs" / "benchmark-results.json").read_text())
    out = bres.setdefault(NAME, {})
    for c in bench:
        v = score(c["tool"], c["state"], c["group"] if c["group"] not in TEST_GROUPS else None)
        out[c["id"]] = {"p": v["p"], "per_question": v["pq"], "ms": 0, "usage": {"input_tokens": None, "output_tokens": 0}}
    (HERE / "runs" / "benchmark-results.json").write_text(json.dumps(bres))

    # 2) the 50 decisions, collusion variants and traps, raw / scrubbed / extracted
    by_id = {c["id"]: c for c in bench}
    cache = load_cache()
    res = cache.setdefault("embed", {})
    for c in all_inputs():
        base = c["id"].split("~")[0]
        grp = by_id[base]["group"] if base in by_id and by_id[base]["group"] not in TEST_GROUPS else None
        for variant, kind in (("raw", None), ("scrubbed", "paraphrase"), ("extracted", "extract")):
            state = c["state"] if kind is None else scrubbed(c, cache, kind)
            v = score(c["tool"], state, grp)
            res[f"{c['id']}|{variant}"] = {"p": v["p"], "pq": v["pq"], "ms": 0}
    save_cache(cache)
    ms = float(np.median(emb.ms)) if emb.ms else None
    (HERE / "runs" / "embed-meta.json").write_text(json.dumps({"model": "google/embeddinggemma-2", "median_ms_per_text": ms,
                                                               "questions": sorted(final), "train_cases": len(train)}))
    print(f"done; median {ms:.1f} ms per embedded text" if ms else "done")


if __name__ == "__main__":
    main()
