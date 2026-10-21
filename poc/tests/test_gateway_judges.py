"""What the gateway does when judges fail or are switched off. No models needed.

    python3 -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from harness.gateway import Gateway  # noqa: E402
from harness.policy import Policy  # noqa: E402
from harness.recorder import Recorder, verify  # noqa: E402
from harness.sandbox import Sandbox  # noqa: E402
from harness.scenarios import seed  # noqa: E402

REFUND = ("issue_refund", {"customer": "C-20931", "amount": 1200.0, "reason": "duplicate charge on order 8812"})


class Fixed:
    kind = "stub"

    def __init__(self, ident: str, p: float):
        self.id, self.version, self.p = ident, ident + "-v0", p

    def decide(self, questions, state):
        return {"judge": self.id, "kind": self.kind, "version": self.version, "answers": {},
                "p_violation": self.p, "per_question": {q: self.p for q in questions}, "latency_ms": 0, "usage": {}}


class Down:
    kind, id, version = "stub", "down", "down-v0"

    def decide(self, questions, state):
        raise TimeoutError("judge service did not answer in 30s")


class GatewayCase(unittest.TestCase):
    """Shared setup: a fresh recorder and sandbox per test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.rec = Recorder(root / "log.jsonl", root / "test.key")
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def gateway(self, judges, shadow=None):
        run = self.root / f"run{len(list(self.root.iterdir()))}"
        sb = Sandbox(run / "world", seed())
        return Gateway(run_dir=run, session="s", task={"summary": "test"}, sandbox=sb, policy=Policy(HERE / "packs"),
                       judges=judges, recorder=self.rec, versions={"test": "1"}, shadow_judges=shadow)

    def kinds(self):
        return [e["kind"] for e in self.rec.entries("s")]


class GatewayJudgeFailures(GatewayCase):
    def test_healthy_judge_allows(self):
        gw = self.gateway([Fixed("ok", 0.01)])
        gw.handle(*REFUND)
        self.assertEqual(gw.outcomes[-1]["outcome"], "executed")

    def test_unavailable_judge_holds_and_is_recorded(self):
        gw = self.gateway([Fixed("ok", 0.01), Down()])
        msg = gw.handle(*REFUND)
        self.assertEqual(gw.outcomes[-1]["outcome"], "held")
        self.assertIn("t1_unavailable", self.kinds())
        self.assertIn("held", msg)

    def test_every_judge_unavailable_holds(self):
        gw = self.gateway([Down()])
        gw.handle(*REFUND)
        self.assertEqual(gw.outcomes[-1]["outcome"], "held")

    def test_block_from_an_available_judge_still_stands(self):
        gw = self.gateway([Fixed("strict", 0.99), Down()])
        gw.handle(*REFUND)
        self.assertEqual(gw.outcomes[-1]["outcome"], "blocked")

    def test_judges_switched_off_never_approve(self):
        gw = self.gateway([])
        gw.handle(*REFUND)
        self.assertEqual(gw.outcomes[-1]["outcome"], "held")

    def test_failing_shadow_judge_does_not_affect_the_action(self):
        gw = self.gateway([Fixed("ok", 0.01)], shadow=[Down()])
        gw.handle(*REFUND)
        self.assertEqual(gw.outcomes[-1]["outcome"], "executed")
        self.assertIn("t1_shadow_unavailable", self.kinds())

    def test_record_stays_verifiable(self):
        gw = self.gateway([Down()])
        gw.handle(*REFUND)
        self.assertTrue(verify(self.rec.path, self.rec.key)["ok"])


if __name__ == "__main__":
    unittest.main()
