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


@lru_cache(maxsize=8)
def cli_info(executable: str) -> tuple[str, str]:
    """Version and help text of the executable a lane actually runs. ("unknown", "") when it can't be
    inspected: these only tune caching and labels, so they never block a launch or dry run."""
    try:
        version = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=15,
                                 stdin=subprocess.DEVNULL)
        help_result = subprocess.run([executable, "--help"], capture_output=True, text=True, timeout=15,
                                     stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown", ""
    if version.returncode or help_result.returncode:
        return "unknown", ""
    return version.stdout.strip(), help_result.stdout


def claude_cache_flags(executable: str = "claude") -> list[str]:
    _, help_text = cli_info(executable)
    result = []
    if "--exclude-dynamic-system-prompt-sections" in help_text:
        result.append("--exclude-dynamic-system-prompt-sections")
    if "--system-prompt-snapshot" in help_text:
        result += ["--system-prompt-snapshot", "on"]
    return result


REVIEWER_CODEX_DISABLE = ("plugins", "apps", "hooks", "multi_agent", "computer_use", "browser_use",
                          "browser_use_external", "browser_use_full_cdp_access", "in_app_browser")


@lru_cache(maxsize=4)
def codex_features(executable: str = "codex") -> frozenset:
    try:
        result = subprocess.run([executable, "features", "list"], capture_output=True, text=True, timeout=20,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    if result.returncode:
        return frozenset()
    return frozenset(line.split()[0] for line in result.stdout.splitlines() if line.strip())


def reviewer_codex_flags(cwd: str, executable: str = "codex") -> tuple[list[str], list[str]]:
    """Best-effort extra isolation for read-only Codex reviewers, on top of --sandbox read-only.

    Only features this Codex version knows are disabled, and only configured MCP servers with plain
    names are switched off. Anything that can't be applied becomes a warning, never a failed launch."""
    warnings = []
    known = codex_features(executable)
    flags = [part for name in REVIEWER_CODEX_DISABLE if name in known for part in ("--disable", name)]
    if not known:
        warnings.append("could not list Codex features; reviewer relies on the read-only sandbox")
    try:
        result = subprocess.run([executable, *flags, "mcp", "list", "--json"], cwd=cwd, capture_output=True,
                                text=True, timeout=20, stdin=subprocess.DEVNULL)
        servers = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        servers = None
    if not isinstance(servers, list):
        warnings.append("could not list Codex MCP servers; they stay as configured for this reviewer")
        return flags, warnings
    for server in sorted((item for item in servers if isinstance(item, dict)), key=lambda item: str(item.get("name"))):
        name = str(server.get("name", ""))
        if server.get("enabled") is False:
            continue
        if re.fullmatch(r"[A-Za-z0-9_-]+", name):
            flags += ["-c", f"mcp_servers.{name}.enabled=false"]
        else:
            warnings.append(f"MCP server {name!r} left enabled (name can't be addressed with -c)")
    return flags, warnings


def profile(tool: str, lane: dict, command: list[str], shared: str, prompt: str) -> dict:
    version, _ = cli_info(command[0] if command else tool)
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
