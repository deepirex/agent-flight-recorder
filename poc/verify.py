"""Verify a flight-recorder log, or demonstrate tamper detection.

    python verify.py runs/<run>/flight-recorder.jsonl
    python verify.py runs/<run>/flight-recorder.jsonl --tamper-demo

--tamper-demo never touches the original. It makes three copies and alters each one the way an insider might:
edits a refund amount, deletes an entry, and rewrites an entry and recomputes its hash without the signing key.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from harness.recorder import canonical, load_or_create_key, sha256, verify  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--tamper-demo", action="store_true")
    ap.add_argument("--chain-only", action="store_true",
                    help="check hashes, links and sequence without the signing key (anyone can run this)")
    args = ap.parse_args()
    log = Path(args.log)
    key_path = HERE / ".keys" / "recorder.key"
    if args.chain_only or not key_path.exists():
        result = verify(log, None)
        print(json.dumps({"original (chain only, signatures not checked)": result}, indent=2))
        sys.exit(0 if result["ok"] else 1)
    key = load_or_create_key(key_path)
    result = verify(log, key)
    print(json.dumps({"original": result}, indent=2))
    if not args.tamper_demo:
        sys.exit(0 if result["ok"] else 1)

    lines = log.read_text().splitlines()
    # pick an entry worth tampering with: a t1 verdict if there is one
    idx = next((i for i, l in enumerate(lines) if json.loads(l)["kind"] == "t1"), len(lines) // 2)
    out = {}
    with tempfile.TemporaryDirectory() as td:
        # 1. edit content in place
        e = json.loads(lines[idx]); e["body"]["p_violation"] = 0.01
        p1 = Path(td, "edited.jsonl"); p1.write_text("\n".join(lines[:idx] + [json.dumps(e)] + lines[idx + 1:]) + "\n")
        out["edit a judge verdict to 0.01"] = verify(p1, key)
        # 2. delete an entry
        p2 = Path(td, "deleted.jsonl"); p2.write_text("\n".join(lines[:idx] + lines[idx + 1:]) + "\n")
        out["delete one entry"] = verify(p2, key)
        # 3. edit and recompute the hash, but without the signing key
        e = json.loads(lines[idx]); e["body"]["p_violation"] = 0.01
        e["hash"] = sha256(canonical({k: v for k, v in e.items() if k not in ("hash", "sig")}))
        p3 = Path(td, "rehashed.jsonl"); p3.write_text("\n".join(lines[:idx] + [json.dumps(e)] + lines[idx + 1:]) + "\n")
        out["edit, then recompute the hash"] = verify(p3, key)
    print(json.dumps({"tamper_demo": out}, indent=2))


if __name__ == "__main__":
    main()
