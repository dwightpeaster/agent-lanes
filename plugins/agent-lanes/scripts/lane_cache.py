"""Stable launch profiles and observational cache metrics; no provider API calls."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from functools import lru_cache
from pathlib import Path


def digest(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def snapshot_contracts(root: Path) -> dict:
    return {kind: (root / "assets" / name).read_text().strip()
            for kind, name in (("implement", "lane-contract.md"), ("review", "review-contract.md"))}


def contract(data: dict, lane: dict, root: Path) -> str:
    kind = "review" if lane["kind"] == "review" else "implement"
    # Old runs acquire a snapshot on their next launch. Existing sessions retain their own context.
    if "contracts" not in data:
        data["contracts"] = snapshot_contracts(root)
    return data["contracts"][kind]


@lru_cache(maxsize=4)
def cli_info(tool: str) -> tuple[str, str]:
    try:
        version = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=15)
        help_result = subprocess.run([tool, "--help"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"cannot inspect {tool} CLI capabilities") from exc
    if version.returncode or help_result.returncode:
        raise ValueError(f"cannot inspect {tool} CLI capabilities")
    return version.stdout.strip(), help_result.stdout


def claude_cache_flags() -> list[str]:
    _, help_text = cli_info("claude")
    result = []
    if "--exclude-dynamic-system-prompt-sections" in help_text:
        result.append("--exclude-dynamic-system-prompt-sections")
    if "--system-prompt-snapshot" in help_text:
        result += ["--system-prompt-snapshot", "on"]
    return result


def reviewer_codex_flags(cwd: str) -> list[str]:
    # A read-only shell sandbox does not constrain MCP/app side effects. Disable those separately.
    flags = ["--disable", "plugins", "--disable", "apps", "--disable", "hooks",
             "--disable", "multi_agent", "--disable", "computer_use", "--disable", "browser_use",
             "--disable", "browser_use_external", "--disable", "browser_use_full_cdp_access",
             "--disable", "in_app_browser"]
    result = subprocess.run(["codex", *flags, "mcp", "list", "--json"], cwd=cwd,
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise ValueError("cannot enumerate reviewer MCP servers; refusing unsafe launch")
    try:
        servers = json.loads(result.stdout)
        if not isinstance(servers, list):
            raise ValueError("expected server list")
        for server in sorted(servers, key=lambda s: s["name"]):
            name = server["name"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError("unsupported server identifier")
            flags += ["-c", f"mcp_servers.{name}.enabled=false"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("cannot safely disable reviewer MCP servers") from exc
    return flags


def profile(tool: str, lane: dict, command: list[str], shared: str, prompt: str) -> dict:
    version, _ = cli_info(tool)
    mcp = lane.get("mcp_config")
    mcp_hash = mcp_digest(mcp)
    normalized = []
    replacements = {prompt: "<assignment>", shared: "<contract>"}
    skip_next = False
    for token in command:
        if skip_next:
            normalized.append("<lane-specific>")
            skip_next = False
            continue
        normalized.append(replacements.get(token, token))
        if token in ("-p", "-C", "-o", "--resume", "resume", "--name"):
            skip_next = True
    # Permission rules remain in the fingerprint: matching tools alone does not prove matching context.
    metadata = {"tool": tool, "model": lane["model"], "effort": lane["effort"],
                "kind": "review" if lane["kind"] == "review" else "implement",
                "cli": version, "contract_sha256": digest(shared), "mcp_sha256": mcp_hash,
                "command_sha256": digest(normalized)}
    metadata["fingerprint"] = digest(metadata)
    metadata["group_hint"] = digest({"tool": tool, "model": lane["model"], "effort": lane["effort"],
                                     "kind": metadata["kind"], "cli": version,
                                     "contract": digest(shared), "mcp": mcp_hash,
                                     "tools": sorted(lane.get("extra_tools", [])),
                                     "adapter": [t for t in normalized if t.startswith("--")]})
    return metadata


def mcp_digest(value: str | None) -> str | None:
    if not value:
        return None
    text = value
    try:
        if Path(value).is_file():
            text = Path(value).read_text()
    except OSError as exc:
        raise ValueError("cannot inspect MCP configuration") from exc
    return digest(text)


def assistant_usage(event: dict) -> dict | None:
    message = event.get("message", {})
    usage = message.get("usage")
    if not isinstance(usage, dict) or "input_tokens" not in usage:
        return None
    return {"message_id": message.get("id"), "model": message.get("model"),
            "fresh": int(usage.get("input_tokens") or 0),
            "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
            "cache_read": int(usage.get("cache_read_input_tokens") or 0),
            "out": int(usage.get("output_tokens") or 0)}
