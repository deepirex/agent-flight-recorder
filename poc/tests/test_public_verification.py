"""Public verification: Ed25519 signatures anyone can check, signed checkpoints, single-entry proofs.

    python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import prove  # noqa: E402
from harness import ed25519, merkle  # noqa: E402
from harness.recorder import Recorder, canonical, sha256, verify, verify_checkpoints  # noqa: E402

RFC8032 = [  # section 7.1, tests 1 and 2
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
]


class Ed25519(unittest.TestCase):
    def test_rfc8032_vectors(self):
        for sk, pk, msg, sig in RFC8032:
            sk, msg = bytes.fromhex(sk), bytes.fromhex(msg)
            self.assertEqual(ed25519.public_key(sk).hex(), pk)
            self.assertEqual(ed25519.sign(sk, msg).hex(), sig)
            self.assertTrue(ed25519.verify(bytes.fromhex(pk), msg, bytes.fromhex(sig)))
            self.assertFalse(ed25519.verify(bytes.fromhex(pk), msg + b"x", bytes.fromhex(sig)))


class Merkle(unittest.TestCase):
    def test_every_entry_proves_in_every_size(self):
        for n in list(range(1, 34)) + [100]:
            leaves = [merkle.leaf_hash(os.urandom(32).hex()) for _ in range(n)]
            root = merkle.root(leaves)
            for i in range(n):
                self.assertTrue(merkle.verify_inclusion(leaves[i], i, n, merkle.inclusion_path(i, leaves), root))

    def test_wrong_leaf_fails(self):
        leaves = [merkle.leaf_hash(os.urandom(32).hex()) for _ in range(9)]
        path = merkle.inclusion_path(3, leaves)
        self.assertFalse(merkle.verify_inclusion(leaves[4], 3, 9, path, merkle.root(leaves)))


class PublicRecorder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def recorder(self, name="run"):
        d = self.root / name
        return Recorder(d / "flight-recorder.jsonl", self.root / "hmac.key", signing_key_path=self.root / "ed.key")

    def fill(self, rec, n=12, amount=1200):
        for i in range(n):
            rec.append("proposal", "s", {}, {"tool": "issue_refund", "args": {"amount": amount + i}})

    def test_anyone_can_check_public_signatures(self):
        rec = self.recorder(); self.fill(rec)
        pub = bytes.fromhex(rec.public["public_key"])
        r = verify(rec.path, None, pub)  # no secret key needed
        self.assertTrue(r["ok"]); self.assertEqual(r["public_signatures_checked"], 12)

    def test_edit_and_rehash_fails_public_signature(self):
        rec = self.recorder(); self.fill(rec)
        lines = rec.path.read_text().splitlines()
        e = json.loads(lines[5]); e["body"]["args"]["amount"] = 4800
        e["hash"] = sha256(canonical({k: v for k, v in e.items() if k not in ("hash", "sig", "sig_pub")}))
        lines[5] = json.dumps(e)
        # relink the rest so the chain itself looks intact
        prev = e["hash"]
        for i in range(6, len(lines)):
            x = json.loads(lines[i]); x["prev"] = prev
            x["hash"] = sha256(canonical({k: v for k, v in x.items() if k not in ("hash", "sig", "sig_pub")}))
            prev = x["hash"]; lines[i] = json.dumps(x)
        rec.path.write_text("\n".join(lines) + "\n")
        r = verify(rec.path, None, bytes.fromhex(rec.public["public_key"]))
        self.assertFalse(r["ok"]); self.assertIn("public signature", r["reason"])

    def test_checkpoint_matches_and_catches_rewritten_history(self):
        rec = self.recorder(); self.fill(rec)
        cp_path = rec.path.parent / "checkpoints.jsonl"
        rec.checkpoint(cp_path)
        pub = bytes.fromhex(rec.public["public_key"])
        self.assertTrue(verify_checkpoints(rec.path, cp_path, pub)["ok"])
        # the operator, who holds every key, rewrites history after publishing the checkpoint
        rec.path.unlink()
        rec2 = self.recorder(); self.fill(rec2, amount=4800)
        self.assertTrue(verify(rec2.path, None, pub)["ok"])          # the new log is internally perfect...
        self.assertFalse(verify_checkpoints(rec2.path, cp_path, pub)["ok"])  # ...but no longer matches what was published

    def test_single_entry_proof(self):
        rec = self.recorder(); self.fill(rec)
        rec.checkpoint(rec.path.parent / "checkpoints.jsonl")
        proof = prove.make(rec.path, 7)
        pub = bytes.fromhex(rec.public["public_key"])
        r = prove.check(proof, pub)
        self.assertTrue(r["ok"]); self.assertTrue(r["entry_publicly_signed"])
        self.assertNotIn("1206", json.dumps(proof["path"]))  # siblings are hashes, not other entries
        proof["entry"]["body"]["args"]["amount"] = 1
        self.assertFalse(prove.check(proof, pub)["ok"])


if __name__ == "__main__":
    unittest.main()
