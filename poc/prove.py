"""Prove one flight-recorder entry is in a published checkpoint, without revealing any other entry.

    python3 prove.py runs/<run>/flight-recorder.jsonl <seq>            # writes runs/<run>/proofs/entry-<seq>.json
    python3 prove.py --check runs/<run>/proofs/entry-<seq>.json         # anyone: needs only the proof and the public key

The proof holds the one entry, its position, the sibling hashes up to the Merkle root, and the signed checkpoint.
An auditor can confirm the entry is unaltered and part of the published history while seeing nothing else.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from harness import ed25519, merkle  # noqa: E402
from harness.recorder import canonical, load_public_key, sha256  # noqa: E402

PUB = HERE / "keys" / "recorder-ed25519.pub.json"


def make(log: Path, seq: int) -> dict:
    entries = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    cps = [json.loads(l) for l in (log.parent / "checkpoints.jsonl").read_text().splitlines() if l.strip()]
    cp = next((c for c in cps if c["tree_size"] >= seq), None)
    if cp is None:
        raise SystemExit(f"no published checkpoint covers entry {seq} yet")
    leaves = [merkle.leaf_hash(e["hash"]) for e in entries[: cp["tree_size"]]]
    index = seq - 1
    return {"entry": entries[index], "leaf_index": index, "tree_size": cp["tree_size"],
            "path": [h.hex() for h in merkle.inclusion_path(index, leaves)], "checkpoint": cp}


def check(proof: dict, public_key: bytes) -> dict:
    e, cp = proof["entry"], proof["checkpoint"]
    body = {k: v for k, v in e.items() if k not in ("hash", "sig", "sig_pub")}
    content_ok = sha256(canonical(body)) == e["hash"]
    entry_sig_ok = ("sig_pub" in e and ed25519.verify(public_key, e["hash"].encode(), bytes.fromhex(e["sig_pub"]["value"])))
    cp_body = {k: v for k, v in cp.items() if k != "sig"}
    cp_sig_ok = ed25519.verify(public_key, canonical(cp_body), bytes.fromhex(cp["sig"]["value"]))
    included = merkle.verify_inclusion(merkle.leaf_hash(e["hash"]), proof["leaf_index"], proof["tree_size"],
                                       [bytes.fromhex(h) for h in proof["path"]], bytes.fromhex(cp["root"]))
    return {"ok": content_ok and cp_sig_ok and included and proof["tree_size"] == cp["tree_size"],
            "entry_unaltered": content_ok, "entry_publicly_signed": bool(entry_sig_ok),
            "checkpoint_signed_by_published_key": cp_sig_ok, "entry_in_checkpoint": included,
            "entry": {"seq": e["seq"], "kind": e["kind"]}, "checkpoint_time": cp["time"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("log_or_proof")
    ap.add_argument("seq", nargs="?", type=int)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--pubkey", default=str(PUB))
    a = ap.parse_args()
    if a.check:
        result = check(json.loads(Path(a.log_or_proof).read_text()), load_public_key(Path(a.pubkey)))
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["ok"] else 1)
    log = Path(a.log_or_proof)
    proof = make(log, a.seq)
    out = log.parent / "proofs" / f"entry-{a.seq}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(proof, indent=2, ensure_ascii=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
