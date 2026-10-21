"""The flight recorder: an append-only, hash-chained, signed event log.

Every entry carries the hash of the entry before it, so deleting, reordering or editing any entry breaks the chain
from that point on. Each entry hash is also signed with a key the agent never sees.

Two kinds of signature:
- HMAC-SHA256 with a local secret key (every entry, since the first run). Only the key holder can check it.
- Ed25519 with a published public key (entries written since public signing was added). Anyone can check it.
Checkpoints: a Merkle root over every entry hash so far, signed with the Ed25519 key and published, so the history
can't be quietly rewritten after the fact; inclusion proofs let an auditor check one entry without seeing the rest.

POC limits, stated plainly:
- Checkpoints are published to this project's own public repository. That makes rewriting visible to anyone who
  kept a copy, but the repository owner could still force-push; production should also post them to an
  independent transparency log (e.g. Sigstore Rekor) or to witnesses that co-sign them.
- The recorder runs in the same process as the harness. Production should run it as a separate service writing to
  WORM storage, so a compromised harness process cannot rewrite history either.

Attribute names follow the OpenTelemetry GenAI semantic conventions (gen_ai.*) where one exists, so entries can be
mapped onto OTel spans and shipped to a SIEM.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from . import ed25519, merkle

GENESIS = "0" * 64


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_or_create_key(path: Path) -> bytes:
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    path.write_text(key.hex())
    os.chmod(path, 0o600)
    return key


class Recorder:
    def __init__(self, log_path: Path, key_path: Path, signing_key_path: Path | None = None):
        self.path = log_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key = load_or_create_key(key_path)
        self.key_id = sha256(self.key)[:16]
        self.signing_key = load_or_create_signing_key(signing_key_path) if signing_key_path else None
        self.public = public_key_record(self.signing_key) if self.signing_key else None
        self.lock = threading.Lock()
        self.seq, self.prev = self._tail()

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0, GENESIS
        last = None
        with self.path.open() as f:
            for line in f:
                if line.strip():
                    last = line
        entry = json.loads(last)
        return entry["seq"], entry["hash"]

    def append(self, kind: str, session: str, attrs: dict[str, Any], body: dict[str, Any],
               versions: dict[str, str] | None = None) -> dict[str, Any]:
        with self.lock:
            entry = {
                "seq": self.seq + 1,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z",
                "session": session,
                "kind": kind,
                "attrs": attrs,
                "body": body,
                "versions": versions or {},
                "prev": self.prev,
            }
            entry["hash"] = sha256(canonical(entry))
            entry["sig"] = {"alg": "HMAC-SHA256", "key_id": self.key_id,
                            "value": hmac.new(self.key, entry["hash"].encode(), hashlib.sha256).hexdigest()}
            if self.signing_key:
                entry["sig_pub"] = {"alg": "Ed25519", "key_id": self.public["key_id"],
                                    "value": ed25519.sign(self.signing_key, entry["hash"].encode()).hex()}
            with self.path.open("a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self.seq, self.prev = entry["seq"], entry["hash"]
            return entry

    def checkpoint(self, cp_path: Path) -> dict[str, Any]:
        """Sign a Merkle root over every entry so far and append it to a file that gets published."""
        if not self.signing_key:
            raise ValueError("checkpoints need a public signing key")
        body = checkpoint_body(self.entries(), self.path.name)
        body["key_id"] = self.public["key_id"]
        cp = {**body, "sig": {"alg": "Ed25519", "value": ed25519.sign(self.signing_key, canonical(body)).hex()}}
        cp_path.parent.mkdir(parents=True, exist_ok=True)
        with cp_path.open("a") as f:
            f.write(json.dumps(cp) + "\n")
        return cp

    def entries(self, session: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]
        return [e for e in out if session is None or e["session"] == session]


def load_or_create_signing_key(path: Path) -> bytes:
    """The Ed25519 secret key. Stays local, like the HMAC key; only its public half is published."""
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    path.write_text(secret.hex())
    os.chmod(path, 0o600)
    return secret


def public_key_record(secret: bytes) -> dict[str, str]:
    pub = ed25519.public_key(secret)
    return {"alg": "Ed25519", "key_id": sha256(pub)[:16], "public_key": pub.hex(),
            "use": "verifies sig_pub on flight-recorder entries and the signature on checkpoints"}


def load_public_key(path: Path) -> bytes:
    return bytes.fromhex(json.loads(path.read_text())["public_key"])


def checkpoint_body(entries: list[dict[str, Any]], log_name: str) -> dict[str, Any]:
    leaves = [merkle.leaf_hash(e["hash"]) for e in entries]
    return {"log": log_name, "tree_size": len(entries), "root": merkle.root(leaves).hex(),
            "head": entries[-1]["hash"] if entries else GENESIS,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def verify_checkpoints(log_path: Path, cp_path: Path, public_key: bytes) -> dict[str, Any]:
    """Every published checkpoint must be signed by the published key and match the log's first tree_size entries."""
    entries = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    leaves = [merkle.leaf_hash(e["hash"]) for e in entries]
    results = []
    for line in cp_path.read_text().splitlines():
        if not line.strip():
            continue
        cp = json.loads(line)
        body = {k: v for k, v in cp.items() if k != "sig"}
        sig_ok = ed25519.verify(public_key, canonical(body), bytes.fromhex(cp["sig"]["value"]))
        n = cp["tree_size"]
        root_ok = n <= len(leaves) and merkle.root(leaves[:n]).hex() == cp["root"]
        results.append({"tree_size": n, "time": cp["time"], "signature_ok": sig_ok, "matches_log": root_ok})
    ok = bool(results) and all(r["signature_ok"] and r["matches_log"] for r in results)
    return {"ok": ok, "checkpoints": results}


def verify(log_path: Path, key: bytes | None, public_key: bytes | None = None) -> dict[str, Any]:
    """Recompute every hash, link and signature (signatures only when a key is given).
    Returns the first broken entry, if any."""
    prev = GENESIS
    expected_seq = 1
    count = 0
    pub_signed = 0
    for lineno, line in enumerate(log_path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return {"ok": False, "entries": count, "broken_at_line": lineno, "reason": "entry is not valid JSON"}
        body = {k: v for k, v in entry.items() if k not in ("hash", "sig", "sig_pub")}
        if entry.get("seq") != expected_seq:
            return {"ok": False, "entries": count, "broken_at_seq": entry.get("seq"),
                    "reason": f"sequence gap: expected {expected_seq}, found {entry.get('seq')} (entry deleted or reordered)"}
        if entry.get("prev") != prev:
            return {"ok": False, "entries": count, "broken_at_seq": entry["seq"],
                    "reason": "link broken: prev does not match the hash of the previous entry"}
        if sha256(canonical(body)) != entry.get("hash"):
            return {"ok": False, "entries": count, "broken_at_seq": entry["seq"],
                    "reason": "content altered: recomputed hash does not match the recorded hash"}
        good_sig = hmac.new(key, entry["hash"].encode(), hashlib.sha256).hexdigest() if key else None
        if key and not hmac.compare_digest(good_sig, entry.get("sig", {}).get("value", "")):
            return {"ok": False, "entries": count, "broken_at_seq": entry["seq"],
                    "reason": "signature invalid: entry was not written by the recorder key"}
        if public_key is not None and "sig_pub" in entry:
            if not ed25519.verify(public_key, entry["hash"].encode(), bytes.fromhex(entry["sig_pub"]["value"])):
                return {"ok": False, "entries": count, "broken_at_seq": entry["seq"],
                        "reason": "public signature invalid: entry was not signed by the published key"}
            pub_signed += 1
        prev = entry["hash"]
        expected_seq += 1
        count += 1
    out = {"ok": True, "entries": count, "head": prev, "signatures_checked": key is not None}
    if public_key is not None:
        out["public_signatures_checked"] = pub_signed
    return out
