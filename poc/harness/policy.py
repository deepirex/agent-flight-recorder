"""T0: deterministic rules, loaded from policy packs. The authority envelope. No model can widen it.

A policy pack is a per-use-case template: its rules, the judge questions for each action it covers, and any routing
thresholds that differ from the base pack. Every decision is stamped with the pack name and version that made it.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


class Policy:
    def __init__(self, packs_dir: Path):
        files = sorted(packs_dir.glob("*.yaml"))
        packs = {f.stem: (yaml.safe_load(f.read_bytes()), hashlib.sha256(f.read_bytes()).hexdigest()[:8])
                 for f in files}
        if "base" not in packs:
            raise ValueError(f"no base.yaml in {packs_dir}")
        base, _ = packs["base"]
        self.base = base
        self.cfg: dict[str, Any] = dict(base.get("rules", {}))
        self.packs: dict[str, dict[str, Any]] = {}
        self.tool_pack: dict[str, str] = {}
        for stem, (pack, digest) in packs.items():
            label = f"{pack['name']}@{pack['version']}+{digest}"
            self.packs[pack["name"]] = {**pack, "label": label}
            if stem == "base":
                continue
            for key, value in pack.get("rules", {}).items():
                if key in self.cfg:
                    raise ValueError(f"rule '{key}' defined by more than one pack")
                self.cfg[key] = value
            for tool in pack.get("applies_to", []):
                if tool in self.tool_pack:
                    raise ValueError(f"tool '{tool}' is covered by more than one pack")
                self.tool_pack[tool] = pack["name"]
        self.version = "packs:" + ",".join(sorted(p["label"] for p in self.packs.values()))
        self.gated_reads = {"read_file"} if self.cfg.get("reads", {}).get("sensitive_paths") else set()
        self.secret_res = [re.compile(p) for p in self.cfg["secrets"]["patterns"]]

    def pack_for(self, tool: str) -> dict[str, Any]:
        return self.packs[self.tool_pack.get(tool, "base")]

    def routing_for(self, tool: str) -> dict[str, float]:
        return {**self.base["routing"], **self.pack_for(tool).get("routing", {})}

    @property
    def routing(self) -> dict[str, float]:
        return self.base["routing"]

    def questions_for(self, tool: str) -> dict[str, dict[str, Any]]:
        """The judge questions the covering pack defines for this tool, with shared questions resolved."""
        shared = self.base.get("shared_questions", {})
        out = {}
        for name, q in self.pack_for(tool).get("questions", {}).get(tool, {}).items():
            out[name] = shared[q["use"]] if "use" in q else q
        return out

    def _secrets(self, text: str) -> list[str]:
        return [r.pattern for r in self.secret_res if r.search(text or "")]

    def check(self, tool: str, args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        """Return {"decision": "pass"|"deny", "rules": [...]} with every rule evaluated, so the record shows
        what was checked as well as what failed."""
        checks: list[dict[str, Any]] = []

        def rule(rule_id: str, ok: bool, detail: str) -> None:
            checks.append({"rule": rule_id, "ok": ok, "detail": detail})

        c = self.cfg
        if tool == "read_file":
            path = str(args.get("path", ""))
            hit = [p for p in c.get("reads", {}).get("sensitive_paths", []) if p in path]
            rule("reads.sensitive_path", not hit, f"{path} matches {hit}" if hit else path)

        elif tool == "git_push":
            branch = str(args.get("branch", ""))
            rule("git.no_force_push", not args.get("force"), "force-push is never allowed")
            rule("git.protected_branch", branch not in c["protected_branches"],
                 f"direct push to protected branch '{branch}'" if branch in c["protected_branches"] else branch)

        elif tool == "git_commit":
            rule("git.message_present", bool(str(args.get("message", "")).strip()), "commit message required")

        elif tool == "write_file":
            path = str(args.get("path", ""))
            rule("write.path_allowed", not any(f in path for f in c["writes"]["forbidden_paths"]), path)
            changed = ctx.get("changed_lines", 0)
            rule("write.size", changed <= c["writes"]["max_changed_lines"], f"{changed} changed lines")
            found = self._secrets(str(args.get("content", "")))
            rule("secrets.none_in_content", not found, ", ".join(found) or "no secret patterns")

        elif tool == "issue_refund":
            try:
                amount = float(args.get("amount", 0))
            except (TypeError, ValueError):
                amount = -1
            rule("refund.positive", amount > 0, f"${amount:,.2f}")
            rule("refund.cap", amount <= c["refunds"]["max_amount"],
                 f"${amount:,.2f} against cap ${c['refunds']['max_amount']:,}")
            known = args.get("customer") in ctx.get("customers", {})
            rule("refund.known_customer", known or not c["refunds"]["require_known_customer"],
                 str(args.get("customer")))

        elif tool == "send_email":
            to = str(args.get("to", "")).strip().lower()
            domain = to.rsplit("@", 1)[-1] if "@" in to else ""
            ticket_email = (ctx.get("ticket_email") or "").lower()
            allowed = to == ticket_email or domain in c["email"]["internal_domains"]
            rule("email.recipient_allowed", allowed,
                 f"{to} {'is' if allowed else 'is not'} the ticket's customer or internal staff")
            found = self._secrets(str(args.get("body", "")) + " " + str(args.get("subject", "")))
            rule("secrets.none_in_content", not found, ", ".join(found) or "no secret patterns")

        elif tool == "http_post":
            host = urlparse(str(args.get("url", ""))).hostname or ""
            rule("egress.allowlist", host in c["egress"]["allowlist"], f"host '{host}'")
            found = self._secrets(str(args.get("body", "")))
            rule("secrets.none_in_content", not found, ", ".join(found) or "no secret patterns")

        failed = [x for x in checks if not x["ok"]]
        return {"decision": "deny" if failed else "pass", "rules": checks, "failed": [x["rule"] for x in failed]}
