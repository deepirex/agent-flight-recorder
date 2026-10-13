"""The flight recorder: an append-only, hash-chained, signed event log.

Every entry carries the hash of the entry before it, so deleting, reordering or editing any entry breaks the chain
from that point on. Each entry hash is also signed with a key the agent never sees.

POC limits, stated plainly:
- Signatures are HMAC-SHA256 with a local key (Python stdlib only). Production should use asymmetric signatures
  (e.g. Ed25519) so auditors can verify without holding the signing key, and anchor periodic checkpoints in a
  transparency log such as Sigstore Rekor.
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
    def __init__(self, log_path: Path, key_path: Path):
        self.path = log_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key = load_or_create_key(key_path)
        self.key_id = sha256(self.key)[:16]
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
            with self.path.open("a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self.seq, self.prev = entry["seq"], entry["hash"]
            return entry

    def entries(self, session: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]
        return [e for e in out if session is None or e["session"] == session]


def verify(log_path: Path, key: bytes | None) -> dict[str, Any]:
    """Recompute every hash, link and signature (signatures only when a key is given).
    Returns the first broken entry, if any."""
    prev = GENESIS
    expected_seq = 1
    count = 0
    for lineno, line in enumerate(log_path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return {"ok": False, "entries": count, "broken_at_line": lineno, "reason": "entry is not valid JSON"}
        body = {k: v for k, v in entry.items() if k not in ("hash", "sig")}
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
        prev = entry["hash"]
        expected_seq += 1
        count += 1
    return {"ok": True, "entries": count, "head": prev, "signatures_checked": key is not None}
