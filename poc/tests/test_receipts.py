"""Decision receipts: built from the log before a consequential action runs; no receipt, no action.

    python3 -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness.recorder import Recorder, verify  # noqa: E402
from harness.sandbox import Sandbox  # noqa: E402
from harness.scenarios import seed  # noqa: E402
from test_gateway_judges import REFUND, Fixed, GatewayCase  # noqa: E402


class Receipts(GatewayCase):
    def run_refund(self):
        gw = self.gateway([Fixed("ok", 0.01)])
        gw.handle("read_ticket", {"ticket_id": "T-88"})
        gw.handle(*REFUND)
        return gw

    def test_receipt_is_logged_before_the_action_runs(self):
        gw = self.run_refund()
        kinds = self.kinds()
        self.assertIn("receipt", kinds)
        self.assertLess(kinds.index("receipt"), kinds.index("execute"))
        self.assertTrue(gw.outcomes[-1]["receipt"].startswith("RCPT-"))

    def test_receipt_lists_the_steps_that_led_here(self):
        self.run_refund()
        r = next(e["body"] for e in self.rec.entries("s") if e["kind"] == "receipt")
        text = " ".join(s["step"] for s in r["steps"])
        self.assertIn("read_ticket", text)          # the earlier read is part of the trail
        self.assertIn("rules: pass", text)
        self.assertIn("judge ok", text)
        self.assertEqual(r["covers"]["to_hash"], self.rec.entries("s")[-3]["hash"])  # last entry before receipt
        self.assertIn("no model wrote this", r["built_by"])

    def test_receipt_files_are_written(self):
        gw = self.run_refund()
        rid = gw.outcomes[-1]["receipt"]
        md = (gw.run_dir / "receipts" / f"{rid}.md").read_text()
        data = json.loads((gw.run_dir / "receipts" / f"{rid}.json").read_text())
        self.assertIn("How it got here", md)
        self.assertEqual(data["id"], rid)

    def test_executor_refuses_without_a_receipt(self):
        sb = Sandbox(self.root / "bare" / "world", seed())
        self.assertIn("refused", sb.execute(*REFUND))
        fake = {"kind": "receipt", "body": {"action_sha256": "0" * 64}}
        self.assertIn("refused", sb.execute(*REFUND, receipt=fake))

    def test_reads_need_no_receipt(self):
        sb = Sandbox(self.root / "bare2" / "world", seed())
        self.assertNotIn("refused", sb.execute("list_files", {}))

    def test_held_action_gets_no_receipt_and_does_not_run(self):
        gw = self.gateway([Fixed("unsure", 0.5)])
        gw.handle(*REFUND)
        self.assertNotIn("receipt", self.kinds())
        self.assertEqual(gw.outcomes[-1]["outcome"], "held")

    def test_chain_still_verifies(self):
        self.run_refund()
        self.assertTrue(verify(self.rec.path, self.rec.key)["ok"])


if __name__ == "__main__":
    unittest.main()
