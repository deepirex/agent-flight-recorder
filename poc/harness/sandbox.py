"""Sandboxed world the agent acts on, and the executor that holds the credentials.

Everything lives under one run directory. Git pushes go to a local bare "origin" repo. Payments update a local
ledger. Emails are written to an outbox folder. Outbound HTTP is never sent; attempts are appended to egress.log.
Nothing leaves the machine.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# Tool catalogue. The agent sees names, descriptions and parameters only.
TOOLS: dict[str, dict[str, Any]] = {
    "read_ticket": {"effect": "read", "desc": "Read a support ticket by id.",
                    "params": {"ticket_id": "string"}},
    "lookup_customer": {"effect": "read", "desc": "Look up a customer's profile, charges and account notes.",
                        "params": {"customer": "string"}},
    "list_files": {"effect": "read", "desc": "List files in the repository.", "params": {}},
    "read_file": {"effect": "read", "desc": "Read a file from the repository.", "params": {"path": "string"}},
    "run_tests": {"effect": "read", "desc": "Run the repository's test suite.", "params": {}},
    "write_file": {"effect": "reversible", "desc": "Create or overwrite a file in the repository.",
                   "params": {"path": "string", "content": "string"}},
    "git_commit": {"effect": "reversible", "desc": "Commit all changes to a branch (created if needed).",
                   "params": {"branch": "string", "message": "string"}},
    "git_push": {"effect": "irreversible", "desc": "Push a branch to the shared remote.",
                 "params": {"branch": "string", "force": "boolean"}},
    "issue_refund": {"effect": "irreversible", "desc": "Refund money to a customer's card.",
                     "params": {"customer": "string", "amount": "number", "reason": "string"}},
    "send_email": {"effect": "irreversible", "desc": "Send an email.",
                   "params": {"to": "string", "subject": "string", "body": "string"}},
    "http_post": {"effect": "irreversible", "desc": "POST data to a URL.",
                  "params": {"url": "string", "body": "string"}},
}


def ollama_tool_schema() -> list[dict[str, Any]]:
    out = []
    for name, t in TOOLS.items():
        props = {k: {"type": v} for k, v in t["params"].items()}
        required = [k for k in t["params"] if k != "force"]
        out.append({"type": "function", "function": {
            "name": name, "description": t["desc"],
            "parameters": {"type": "object", "properties": props, "required": required}}})
    return out


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout.strip()


class Sandbox:
    # Credentials live here, in the executor. The agent never receives them.
    _CREDENTIALS = {"payments_token": "pay_tok_demo_executor_only", "smtp_password": "executor-only"}

    def __init__(self, root: Path, seed: dict[str, Any]):
        self.root = root.resolve()  # absolute, so the git remote path works from any working directory
        self.repo = root / "repo"
        self.origin = root / "origin.git"
        self.ledger_path = root / "ledger.json"
        self.outbox = root / "outbox"
        self.egress_log = root / "egress.log"
        self.tickets = seed.get("tickets", {})
        self._seed(seed)

    # ---------- setup ----------
    def _seed(self, seed: dict[str, Any]) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        self.repo.mkdir(parents=True)
        self.outbox.mkdir()
        for rel, content in seed.get("files", {}).items():
            p = self.repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "agent@acme.example")
        _git(self.repo, "config", "user.name", "Acme Agent")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "Initial state")
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        _git(self.repo, "remote", "add", "origin", str(self.origin))
        _git(self.repo, "push", "-q", "origin", "main")
        self.ledger_path.write_text(json.dumps(seed.get("ledger", {"customers": {}, "refunds": []}), indent=2))
        self.egress_log.write_text("")

    # ---------- state helpers for the gateway ----------
    def ledger(self) -> dict[str, Any]:
        return json.loads(self.ledger_path.read_text())

    def safe_path(self, rel: str) -> Path:
        p = (self.repo / rel).resolve()
        if not str(p).startswith(str(self.repo.resolve()) + "/"):
            raise ValueError(f"path escapes the repository: {rel}")
        return p

    def file_diff(self, rel: str, new: str) -> tuple[str, int]:
        try:
            old = self.safe_path(rel).read_text()
        except (FileNotFoundError, ValueError):
            old = ""
        diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{rel}", f"b/{rel}", lineterm=""))
        changed = sum(1 for l in diff if l[:1] in "+-" and not l.startswith(("+++", "---")))
        return "\n".join(diff), changed

    def pending_diff(self) -> str:
        return _git(self.repo, "diff", "HEAD") or _git(self.repo, "diff", "--cached")

    def fingerprint(self) -> dict[str, str]:
        """sha256 of every file in the world, for snapshot manifests."""
        out = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and "/.git/" not in str(p) and "origin.git" not in str(p):
                out[str(p.relative_to(self.root))] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    # ---------- the executor ----------
    def execute(self, tool: str, args: dict[str, Any]) -> str:
        fn = getattr(self, f"_t_{tool}", None)
        if fn is None:
            return f"error: unknown tool {tool}"
        try:
            return fn(**args)
        except TypeError as e:
            return f"error: bad arguments for {tool}: {e}"
        except Exception as e:  # tool errors go back to the agent as text
            return f"error: {e}"

    def _t_read_ticket(self, ticket_id: str) -> str:
        t = self.tickets.get(ticket_id)
        return json.dumps(t, indent=2) if t else f"error: no ticket {ticket_id}"

    def _t_lookup_customer(self, customer: str) -> str:
        c = self.ledger()["customers"].get(customer)
        return json.dumps({customer: c}, indent=2) if c else f"error: no customer {customer}"

    def _t_list_files(self) -> str:
        return _git(self.repo, "ls-files")

    def _t_read_file(self, path: str) -> str:
        return self.safe_path(path).read_text()

    def _t_run_tests(self) -> str:
        r = subprocess.run([sys.executable, "-I", "tests/run.py"], cwd=self.repo, capture_output=True, text=True,
                           timeout=60)
        out = (r.stdout + r.stderr).replace(str(self.repo) + "/", "")  # keep local paths out of the record
        return out.strip()[-2000:] or f"exit {r.returncode}"

    def _t_write_file(self, path: str, content: str) -> str:
        p = self.safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {path} ({len(content)} bytes)"

    def _t_git_commit(self, branch: str, message: str) -> str:
        current = _git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        if current != branch:
            exists = subprocess.run(["git", "rev-parse", "--verify", branch], cwd=self.repo,
                                    capture_output=True).returncode == 0
            _git(self.repo, "checkout", "-q", *([] if exists else ["-b"]), branch)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", message)
        return f"committed to {branch}: {_git(self.repo, 'rev-parse', '--short', 'HEAD')}"

    def _t_git_push(self, branch: str, force: bool = False) -> str:
        _git(self.repo, "push", "-q", *(["--force"] if force else []), "origin", branch)
        return f"pushed {branch} to origin"

    def _t_issue_refund(self, customer: str, amount: float, reason: str = "") -> str:
        led = self.ledger()
        led["refunds"].append({"customer": customer, "amount": float(amount), "reason": reason,
                               "authorized_with": "payments_token(executor)"})
        self.ledger_path.write_text(json.dumps(led, indent=2))
        return f"refunded ${float(amount):,.2f} to {customer}"

    def _t_send_email(self, to: str, subject: str, body: str) -> str:
        n = len(list(self.outbox.iterdir())) + 1
        (self.outbox / f"{n:03d}.eml").write_text(f"To: {to}\nSubject: {subject}\n\n{body}\n")
        return f"email sent to {to}"

    def _t_http_post(self, url: str, body: str) -> str:
        with self.egress_log.open("a") as f:
            f.write(json.dumps({"url": url, "bytes": len(body)}) + "\n")
        return f"POST {url}: 200 OK (simulated; nothing left this machine)"
