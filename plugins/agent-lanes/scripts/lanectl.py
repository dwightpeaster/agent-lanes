#!/usr/bin/env python3
"""lanectl: deterministic state and process control for agent-lanes.

Python 3.10+ standard library only. All state lives outside the repository in
$AGENT_LANES_HOME (default ~/.agent-lanes)/<repo-key>/<run-id>/.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
try:
    import fcntl
except ImportError:
    raise SystemExit("Agent Lanes currently supports POSIX systems (tested on macOS and Linux), not Windows")
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import lane_cache
import lane_review
import lane_runtime
import lane_planning
import lane_integration

VERSION = "0.2.1"
SCRIPT = Path(__file__).resolve()
PLUGIN_ROOT = SCRIPT.parents[1]
# The contract is identical for every lane, so it goes first (Claude: system prompt) where prompt
# caches can reuse it across lanes. Lane-specific values follow in the assignment.
CONTRACT = PLUGIN_ROOT / "assets" / "lane-contract.md"
ASSIGNMENT_TEMPLATE = PLUGIN_ROOT / "assets" / "lane-assignment.md"

EFFORT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "ultracode"]
DEFAULT_CEILING = "high"
TOOLS = ("claude", "codex")
KINDS = ("implement", "spec", "review", "research")
ACTIVE = {"running"}
LANE_STATES = (
    "planned", "queued", "running", "turn-ended", "failed", "crashed", "stopped",
    "in-review", "changes-requested", "merge-queued", "blocked", "merged", "closed",
)
# Claude lanes load only these tools, no MCP servers and no skills. Each model call re-sends
# every loaded tool definition, so this is the largest fixed cost per lane step.
LANE_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]
NO_MCP = '{"mcpServers":{}}'
MANAGED_START = "<!-- agent-lanes:start -->"
MANAGED_END = "<!-- agent-lanes:end -->"
PROFILE_KEYS = (
    "work_source", "base", "branch", "worktrees", "validate", "smoke", "pr", "merge", "review",
    "tracker", "protected", "resources", "deploy", "cleanup", "mode", "risk_patterns",
    "setup", "setup_not_required", "lane_env", "ticket_command", "integration", "secret_scan",
)
FAILURE_LINE = re.compile(r"error|fail|assert|exception|traceback|expected|panic|not ok|\bE\s", re.IGNORECASE)
DEFINITION = re.compile(r"^\s*(export\s+)?(default\s+)?(async\s+)?(def|class|function|interface|type|struct|"
                        r"enum|fn|func|module|trait|impl|const|public|private|protected)\b")
DEFAULT_DENY = {
    "claude": [
        "Bash(git push *)", "Bash(gh *)", "Bash(git reset --hard *)", "Bash(git clean *)",
        "Bash(git checkout -- *)", "Bash(rm -rf *)", "Read(**/.env*)",
    ],
}
DEFAULT_ALLOW = {
    "claude": [
        "Read", "Edit", "Write", "Glob", "Grep",
        "Bash(git status *)", "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)",
        "Bash(git add *)", "Bash(git commit *)", "Bash(git rebase *)", "Bash(git merge-base *)",
        "Bash(ls *)", "Bash(rg *)", "Bash(grep *)",
    ],
}
DEFAULT_ADAPTERS = {
    "claude": {
        "new": ["claude", "-p", "{prompt}", "--model", "{model}", "--effort", "{effort}",
                "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits",
                "--name", "{name}", "--append-system-prompt", "{contract}", "--tools", "{tools}",
                "--strict-mcp-config", "--mcp-config", "{mcp_config}", "--disable-slash-commands",
                "--settings", '{"attribution":{"commit":"","pr":"","sessionUrl":false}}',
                "{allow_args}", "{deny_args}", "{budget_args}"],
        "resume": ["claude", "-p", "{prompt}", "--resume", "{session}", "--model", "{model}",
                   "--effort", "{effort}", "--output-format", "stream-json", "--verbose",
                   "--permission-mode", "acceptEdits", "--append-system-prompt", "{contract}",
                   "--tools", "{tools}", "--strict-mcp-config",
                   "--mcp-config", "{mcp_config}", "--disable-slash-commands",
                   "--settings", '{"attribution":{"commit":"","pr":"","sessionUrl":false}}',
                   "{allow_args}", "{deny_args}", "{budget_args}"],
    },
    "codex": {
        "new": ["codex", "exec", "--json", "-o", "{last}", "-m", "{model}",
                "-c", "model_reasoning_effort={effort}", "--sandbox", "workspace-write",
                "--add-dir", "{gitdir}", "-C", "{worktree}", "{prompt}"],
        "resume": ["codex", "exec", "--json", "-o", "{last}", "-m", "{model}",
                   "-c", "model_reasoning_effort={effort}", "--sandbox", "workspace-write",
                   "--add-dir", "{gitdir}", "-C", "{worktree}", "resume", "{session}", "{prompt}"],
    },
}


class LaneError(Exception):
    pass


# ---------------------------------------------------------------- utilities

def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def home() -> Path:
    return Path(os.environ.get("AGENT_LANES_HOME", Path.home() / ".agent-lanes")).expanduser()


def git(*args: str, cwd: Path | str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise LaneError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def repo_root(path: str) -> Path:
    return Path(git("rev-parse", "--show-toplevel", cwd=path)).resolve()


def repo_key(root: Path) -> str:
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:8]
    return f"{re.sub(r'[^A-Za-z0-9._-]', '-', root.name)}-{digest}"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "run"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def split_commands(value: str | None) -> list[str]:
    """Split separators outside shell quotes, preserving command text and legacy commas."""
    if not value:
        return []
    positions = []; quote = None; escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False; continue
        if char == "\\" and quote != "'":
            escaped = True; continue
        if quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char in ";\n,":
            positions.append((index, char))
    if quote:
        raise LaneError("command list has an unterminated shell quote")
    primary = any(char in ";\n" for _, char in positions)
    cuts = [index for index, char in positions if char in ";\n" or not primary]
    result = []; start = 0
    for end in cuts + [len(value)]:
        part = value[start:end].strip()
        if part:
            result.append(part)
        start = end + 1
    return result


def effort_rank(level: str) -> int:
    if level not in EFFORT_ORDER:
        raise LaneError(f"unknown effort '{level}'; use one of {', '.join(EFFORT_ORDER)}")
    return EFFORT_ORDER.index(level)


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------- run state

class Run:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.file = self.path / "run.json"
        if not self.file.exists():
            raise LaneError(f"no run at {self.path}")

    @contextlib.contextmanager
    def locked(self):
        with open(self.path / ".lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            data = json.loads(self.file.read_text(encoding="utf-8"))
            yield data
            data["updated"] = now()
            atomic_write(self.file, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def read(self) -> dict:
        return json.loads(self.file.read_text(encoding="utf-8"))

    def lane_dir(self, lane_id: str) -> Path:
        return self.path / "lanes" / lane_id

    def note(self, kind: str, text: str) -> None:
        ledger = self.path / "ledger.md"
        line = f"- {now()} **{kind}**: {text.strip()}\n"
        with open(ledger, "a", encoding="utf-8") as handle:
            handle.write(line)


def resolve_run(value: str) -> Run:
    path = Path(value).expanduser()
    if path.exists():
        return Run(path)
    matches = list(home().glob(f"*/{value}"))
    if len(matches) == 1:
        return Run(matches[0])
    raise LaneError(f"run '{value}' not found (pass the run directory printed by 'run new')")


def get_lane(data: dict, lane_id: str) -> dict:
    lane = data["lanes"].get(lane_id)
    if lane is None:
        raise LaneError(f"unknown lane '{lane_id}'")
    return lane


# ---------------------------------------------------------------- stream parsing

def parse_stream(path: Path, tool: str) -> dict:
    info = {"session": None, "final": None, "cost_usd": None, "tokens_in": 0, "tokens_out": 0,
            "fresh": 0, "cache_write": 0 if tool == "claude" else None, "cache_read": 0,
            "first_call": None, "model_calls": {}, "response_started": False,
            "last": None, "error": None, "done": False}
    if not path.exists():
        return info
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if tool == "claude":
            if event.get("session_id"):
                info["session"] = event["session_id"]
            if etype == "assistant":
                info["response_started"] = True
                sample = lane_cache.assistant_usage(event)
                if sample and sample["message_id"]:
                    info["model_calls"][sample["message_id"]] = sample
                    if info["first_call"] is None:
                        info["first_call"] = sample
                    elif info["first_call"]["message_id"] == sample["message_id"]:
                        info["first_call"] = sample
                for block in event.get("message", {}).get("content", []) or []:
                    if block.get("type") == "tool_use":
                        info["last"] = block.get("name")
            elif etype == "result":
                info["response_started"] |= not bool(event.get("is_error"))
                info["done"] = True
                info["final"] = event.get("result")
                info["cost_usd"] = event.get("total_cost_usd")
                usage = event.get("usage") or {}
                info["fresh"] = int(usage.get("input_tokens") or 0)
                info["cache_write"] = int(usage.get("cache_creation_input_tokens") or 0)
                info["cache_read"] = int(usage.get("cache_read_input_tokens") or 0)
                info["tokens_in"] = info["fresh"] + info["cache_write"] + info["cache_read"]
                info["tokens_out"] = int(usage.get("output_tokens") or 0)
                if event.get("is_error"):
                    info["error"] = event.get("subtype") or "error"
        else:
            if etype == "thread.started":
                info["session"] = event.get("thread_id")
            elif etype in ("item.started", "item.completed"):
                item = event.get("item") or {}
                itype = item.get("type")
                if itype == "agent_message" and etype == "item.completed":
                    info["final"] = item.get("text")
                elif itype:
                    info["last"] = itype
            elif etype == "turn.completed":
                info["done"] = True
                usage = event.get("usage") or {}
                total = int(usage.get("input_tokens") or 0)
                cached = int(usage.get("cached_input_tokens") or 0)
                info["tokens_in"] += total
                info["fresh"] += total - cached
                info["cache_read"] += cached
                info["tokens_out"] += int(usage.get("output_tokens") or 0)
            elif etype in ("turn.failed", "error"):
                err = event.get("error") or {}
                info["error"] = err.get("message") if isinstance(err, dict) else event.get("message")
                info["done"] = True
    return info


def report_status(final: str | None) -> str | None:
    if not final:
        return None
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        value = json.loads(final).get("status")
        if value in ("ready", "blocked"):
            return value
    match = re.search(r"^\s*Status:\s*([A-Za-z-]+)", final, re.MULTILINE)
    return match.group(1).lower() if match else None


def refresh(run: Run, data: dict, lane: dict) -> dict:
    """Update a lane's observed state from its process and stream. Mutates lane."""
    attempt = lane.get("attempt", 0)
    if not attempt:
        return lane
    ldir = run.lane_dir(lane["id"])
    stream = ldir / f"attempt-{attempt}.jsonl"
    info = parse_stream(stream, lane["tool"])
    if info["session"]:
        lane["session"] = info["session"]
    last_file = ldir / f"attempt-{attempt}.last.md"
    final = info["final"]
    if lane["tool"] == "codex" and last_file.exists():
        final = last_file.read_text(encoding="utf-8").strip() or final
    if final:
        atomic_write(ldir / "report.md", final + "\n")
    lane["last_activity"] = info["last"]
    lane["report_status"] = report_status(final)
    exit_file = ldir / f"attempt-{attempt}.exit"
    secs = None
    if exit_file.exists() and lane.get("started_ts"):
        secs = max(0, round(exit_file.stat().st_mtime - lane["started_ts"]))
    usage = lane.setdefault("usage", {})
    usage[str(attempt)] = {"in": info["tokens_in"], "out": info["tokens_out"], "cost_usd": info["cost_usd"],
                           "fresh": info["fresh"], "cache_write": info["cache_write"],
                           "cache_read": info["cache_read"], "secs": secs,
                           "first_call": info["first_call"], "model_calls": list(info["model_calls"].values())[:3],
                           "cache_profile": lane.get("cache_profile"), "resumed": attempt > 1}
    if lane.get("state") == "running":
        if exit_file.exists():
            code = int((exit_file.read_text().strip() or "1"))
            lane["exit_code"] = code
            lane["state"] = "turn-ended" if code == 0 and not info["error"] else "failed"
            lane["error"] = info["error"]
            guard = ldir / f"attempt-{attempt}.guard"
            if guard.exists():
                lane.update({"state": "blocked", "error": guard.read_text(), "report_status": "blocked"})
            if lane["kind"] == "review":
                try:
                    lane_review.verify_checkout(sys.modules[__name__], lane)
                except LaneError as exc:
                    lane.update({"state": "failed", "error": str(exc), "report_status": "blocked"})
        elif not pid_alive(lane.get("pid")):
            lane["state"] = "crashed"
    return lane


# ---------------------------------------------------------------- commands: runs

def cmd_doctor(args) -> None:
    print(f"lanectl {VERSION}")
    print(f"home: {home()}")
    for binary in ("git", "claude", "codex"):
        found = shutil.which(binary)
        version = ""
        if found:
            with contextlib.suppress(Exception):
                version = subprocess.run([binary, "--version"], text=True, capture_output=True,
                                         timeout=15).stdout.strip().splitlines()[0]
        print(f"{binary}: {found or 'NOT FOUND'} {version}".rstrip())
    adapters = home() / "adapters.json"
    print(f"adapter overrides: {adapters if adapters.exists() else 'none (defaults)'}")
    print(f"claude lanes: tools {','.join(LANE_TOOLS)}; no MCP servers; no skills (per-lane opt-in)")
    print("verify flags if a CLI changed: claude --help ; codex exec --help ; codex exec resume --help")


def cmd_run_new(args) -> None:
    root = repo_root(args.repo)
    if args.codex_contract == "developer" and not args.codex_developer_prefix_file:
        raise LaneError("developer experiment requires --codex-developer-prefix-file preserving existing instructions")
    profile = read_profile(root)
    args.base = args.base or profile.get("base")
    if not args.base:
        raise LaneError("no --base given and no base in the AGENTS.md profile")
    mode = args.mode or profile.get("mode", "review")
    args.mode = mode if mode in ("review", "merge-on-green") else "review"
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    run_id = f"{stamp}-{slug(args.name)}"
    path = home() / repo_key(root) / run_id
    if path.exists():
        raise LaneError(f"run already exists: {path}")
    data = {
        "schema": 2, "id": run_id, "name": args.name, "repo": str(root), "base": args.base,
        "initial_base_sha": git("rev-parse", "--verify", "--quiet", args.base, cwd=root, check=False) or None,
        "environment_templates": json.loads(Path(args.env_template_file).read_text()) if args.env_template_file
                                 else json.loads(profile.get("lane_env", "{}")),
        "integration_required": (args.integration or profile.get("integration", "required")) == "required",
        "mode": args.mode, "effort_ceiling": DEFAULT_CEILING, "state": "open",
        "created": now(), "updated": now(), "contracts": lane_cache.snapshot_contracts(PLUGIN_ROOT),
        "profile": profile, "codex_contract": args.codex_contract,
        "codex_developer_prefix": Path(args.codex_developer_prefix_file).read_text()
                                  if args.codex_developer_prefix_file else "",
        "rules": {"validate": split_commands(profile.get("validate")),
                  "protected": split_commands(profile.get("protected")), "notes": [],
                  "risk_patterns": csv(profile.get("risk_patterns")),
                  "setup": split_commands(args.setup if args.setup is not None else profile.get("setup")),
                  "setup_not_required": args.setup_not_required or profile.get("setup_not_required") == "yes",
                  "secret_scan": profile.get("secret_scan")},
        "lanes": {},
    }
    if not isinstance(data["environment_templates"], dict):
        raise LaneError("lane_env must be a JSON object of environment templates")
    (path / "lanes").mkdir(parents=True)
    atomic_write(path / "run.json", json.dumps(data, indent=2, sort_keys=True) + "\n")
    atomic_write(path / "ledger.md", (
        f"# Ledger: {args.name}\n\nRepo: {root}\nBase: {args.base}\nMode: {args.mode}\n"
        f"Created: {data['created']}\n\n## Entries\n\n"
    ))
    print(path)
    if profile:
        print("profile: loaded from AGENTS.md")


def cmd_run_list(args) -> None:
    root = repo_root(args.repo)
    base = home() / repo_key(root)
    rows = []
    for run_file in sorted(base.glob("*/run.json")):
        data = json.loads(run_file.read_text(encoding="utf-8"))
        if data.get("state") == "closed" and not args.all:
            continue
        lanes = data.get("lanes", {})
        open_lanes = sum(1 for lane in lanes.values() if lane.get("state") not in ("merged", "closed"))
        rows.append(f"{run_file.parent}  {data['state']}  lanes={len(lanes)} open={open_lanes}  {data['name']}")
    print("\n".join(rows) if rows else "no open runs for this repo")


def cmd_run_set(args) -> None:
    run = resolve_run(args.run)
    with run.locked() as data:
        if any(lane["state"] == "running" for lane in data["lanes"].values()) and any(
                getattr(args, name) is not None for name in ("setup", "env_template_file", "validate", "protected", "base")):
            raise LaneError("do not change run gates/environment while lanes are running")
        if args.mode:
            data["mode"] = args.mode
        if args.base:
            data["base"] = args.base
        if args.validate is not None:
            data["rules"]["validate"] = split_commands(args.validate)
        if args.protected is not None:
            data["rules"]["protected"] = csv(args.protected)
        if args.risk_patterns is not None:
            data["rules"]["risk_patterns"] = csv(args.risk_patterns)
        if args.state:
            if args.state == "closed" and any(lane["state"] not in ("merged", "closed") for lane in data["lanes"].values()):
                raise LaneError("close every lane before closing the run")
            data["state"] = args.state
            if args.state == "closed":
                data["closed_at"] = now()
                atomic_write(run.path / "summary.json", json.dumps(lane_planning.summary(sys.modules[__name__], data), indent=2) + "\n")
        if args.setup is not None:
            data["rules"]["setup"] = split_commands(args.setup)
        if args.setup_not_required:
            data["rules"]["setup_not_required"] = True
        if args.env_template_file:
            templates = json.loads(Path(args.env_template_file).read_text())
            if not isinstance(templates, dict):
                raise LaneError("environment templates must be a JSON object")
            data["environment_templates"] = templates
        if args.integration:
            data["integration_required"] = args.integration == "required"
    if args.mode:
        run.note("directive", f"autonomy mode set to {args.mode}")
    print("ok")


# ---------------------------------------------------------------- commands: lanes

def cmd_lane_add(args) -> None:
    run = resolve_run(args.run)
    effort_rank(args.effort)
    if args.token_budget is not None and args.token_budget <= 0:
        raise LaneError("token budget must be positive")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.id):
        raise LaneError("lane id must be 1–64 letters, digits, underscores or hyphens")
    if args.kind == "review" and not args.review_of:
        raise LaneError("review lanes require --review-of <implementation lane>")
    with run.locked() as data:
        if args.id in data["lanes"]:
            raise LaneError(f"lane {args.id} already exists")
        ceiling = data.get("effort_ceiling", DEFAULT_CEILING)
        if effort_rank(args.effort) > effort_rank(ceiling) and not args.override:
            raise LaneError(f"effort {args.effort} exceeds ceiling {ceiling}; pass --override "
                            "'<user instruction>' only when the user explicitly asked for it")
        repo = Path(data["repo"])
        base = args.base or data["base"]
        worktree = args.worktree
        if worktree == "auto":
            worktree = str(run.path / "worktrees" / args.id)
        if worktree and args.create_worktree and args.kind != "review":
            if not args.branch:
                raise LaneError("--create-worktree requires --branch")
            branch_exists = git("rev-parse", "--verify", "--quiet", f"refs/heads/{args.branch}",
                                cwd=repo, check=False)
            if branch_exists:
                git("worktree", "add", worktree, args.branch, cwd=repo)
            else:
                git("worktree", "add", "-b", args.branch, worktree, base, cwd=repo)
        lane = {
            "id": args.id, "items": csv(args.items), "kind": args.kind, "tool": args.tool,
            "model": args.model, "effort": args.effort, "worktree": str(Path(worktree).resolve()) if worktree else None,
            "branch": args.branch, "base": base, "owns": csv(args.owns), "reserves": csv(args.reserves),
            "allow": csv(args.allow), "deny": csv(args.deny), "budget_usd": args.budget_usd,
            "start": csv(args.start), "validate": split_commands(args.validate),
            "extra_tools": csv(args.extra_tools), "mcp_config": args.mcp_config,
            "state": "queued" if args.queued else "planned", "blocked_by": csv(args.blocked_by),
            "attempt": 0, "session": None, "pid": None, "pr": None, "override": args.override,
            "created": now(),
            "no_validation_required": args.no_validation_required,
            "token_budget": args.token_budget,
        }
        if args.kind == "review":
            lane_review.register(sys.modules[__name__], run, data, lane, args.review_of)
        data["lanes"][args.id] = lane
        run.lane_dir(args.id).mkdir(parents=True, exist_ok=True)
        lane["environment"] = lane_runtime.reserve(sys.modules[__name__], run, args.id)
    ok, setup_lines = True, []
    if lane.get("worktree") and args.kind not in ("review", "research"):
        ok, setup_lines = lane_runtime.prepare(sys.modules[__name__], run, args.id, 600)
    if args.override:
        run.note("directive", f"{args.id} effort {args.effort}. User instruction: {args.override}")
    print(f"lane {args.id} added ({args.tool}/{args.model}@{args.effort})")
    if args.no_validation_required:
        run.note("directive", args.no_validation_required)
    if not ok:
        raise LaneError("setup blocked: " + "; ".join(setup_lines))


def cmd_lane_set(args) -> None:
    run = resolve_run(args.run)
    with run.locked() as data:
        lane = get_lane(data, args.id)
        if lane.get("state") == "running" and any(getattr(args, key) is not None
                                                  for key in ("model", "effort", "branch", "owns", "validate", "token_budget")):
            raise LaneError("do not change a running lane's launch settings")
        if lane["kind"] == "review" and (args.owns or args.branch or args.validate):
            raise LaneError("reviewer ownership, branch and validation cannot be overridden")
        if args.state:
            if args.state not in LANE_STATES:
                raise LaneError(f"state must be one of {', '.join(LANE_STATES)}")
            lane["state"] = args.state
        for key in ("pr", "model", "branch"):
            value = getattr(args, key)
            if value:
                lane[key] = value
        if args.effort:
            ceiling = data.get("effort_ceiling", DEFAULT_CEILING)
            if effort_rank(args.effort) > effort_rank(ceiling) and not args.override:
                raise LaneError(f"effort {args.effort} exceeds ceiling {ceiling}; pass --override")
            lane["effort"] = args.effort
            lane["override"] = args.override or lane.get("override")
        if args.owns is not None:
            lane["owns"] = csv(args.owns)
        if args.start is not None:
            lane["start"] = csv(args.start)
        if args.validate is not None:
            lane["validate"] = split_commands(args.validate)
        if args.token_budget is not None:
            if args.token_budget <= 0:
                raise LaneError("token budget must be positive")
            lane["token_budget"] = args.token_budget
    print("ok")


def load_adapters() -> dict:
    adapters = json.loads(json.dumps(DEFAULT_ADAPTERS))
    override = home() / "adapters.json"
    if override.exists():
        for tool, modes in json.loads(override.read_text(encoding="utf-8")).items():
            adapters.setdefault(tool, {}).update(modes)
    return adapters


def run_adapters(data: dict) -> dict:
    if "adapters" not in data:
        data["adapters"] = load_adapters()
    return data["adapters"]


def check_command(run_path: Path | str, lane_id: str) -> str:
    return f"python3 {shlex.quote(str(SCRIPT))} check --run {shlex.quote(str(run_path))} --id {lane_id}"


def lane_gates(data: dict, lane: dict) -> list[str]:
    """The lane's own validation commands, without the always-appended secret scanner."""
    return list(lane.get("validate") or data["rules"]["validate"])


def lane_validation(data: dict, lane: dict) -> list[str]:
    commands = lane_gates(data, lane)
    scanner = data["rules"].get("secret_scan")
    if scanner:
        commands.append(scanner)
    return commands


def render_assignment(data: dict, lane: dict, run_path: Path) -> str:
    template = ASSIGNMENT_TEMPLATE.read_text(encoding="utf-8")
    values = {
        "start": ", ".join(lane.get("start", [])) or "(as named in the brief)",
        "check": check_command(run_path, lane["id"]),
        "lane": lane["id"], "items": ", ".join(lane["items"]) or "(see brief)",
        "worktree": lane["worktree"] or "(none: read-only lane)", "branch": lane["branch"] or "(none)",
        "base": lane["base"], "owns": ", ".join(lane["owns"]) or "(only what the brief names)",
        "reserves": ", ".join(lane["reserves"]) or "none",
        "validate": "; ".join(lane_validation(data, lane)) or "(as stated in the brief)",
        "protected": "; ".join(data["rules"]["protected"]) or "(as stated in the brief)",
        "namespace": lane.get("environment", {}).get("LANE_NAMESPACE", "unassigned"),
        "ports": lane.get("environment", {}).get("LANE_PORT_START", "?") + "-" + lane.get("environment", {}).get("LANE_PORT_END", "?"),
    }
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def build_command(run: Run, data: dict, lane: dict, prompt: str, resume: bool, attempt: int) -> list[str]:
    tool = lane["tool"]
    adapters = run_adapters(data)
    template = adapters[tool]["resume" if resume else "new"]
    review = lane["kind"] == "review"
    # Custom adapters cannot bypass reviewer restrictions.
    if review:
        if tool == "claude":
            template = ["claude", "-p", "{prompt}", "--model", "{model}", "--effort", "{effort}",
                        "--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk",
                        "--append-system-prompt", "{contract}", "--tools", "Read,Glob,Grep",
                        "--strict-mcp-config", "--mcp-config", NO_MCP, "--disable-slash-commands",
                        "--settings", '{"disableAllHooks":true,"attribution":{"commit":"","pr":"","sessionUrl":false}}',
                        # Project settings come from the commit under review; load only the user's own.
                        "--setting-sources", "user",
                        # The packet and patch files live outside the reviewer's checkout.
                        "--add-dir", "{review_dir}",
                        "--disallowedTools", ",".join(["Edit", "Write", "Bash", "Agent", "Read(**/.env*)",
                                                       *lane.get("deny", [])]),
                        "{budget_args}"]
            if resume:
                template += ["--resume", "{session}"]
        else:
            template = ["codex", "exec", "--json", "-o", "{last}", "-m", "{model}",
                        "-c", "model_reasoning_effort={effort}", "--sandbox", "read-only",
                        "-c", 'approval_policy="never"', "-C", "{worktree}"]
            flags, warnings = lane_cache.reviewer_codex_flags(lane["worktree"])
            template += flags
            lane["launch_warnings"] = warnings
            if resume:
                # Parent exec options retain the read-only policy on resume.
                template += ["resume", "{session}"]
            template += ["{prompt}"]
    if resume and not lane.get("session"):
        raise LaneError(f"lane {lane['id']} has no session to resume yet")
    gitdir = ""
    if lane.get("worktree"):
        gitdir = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=lane["worktree"], check=False)
    allow = DEFAULT_ALLOW.get(tool, []) + lane.get("allow", [])
    if tool == "claude" and not review:
        # Lanes may run their validation and the compact check wrapper without prompting.
        for command in [check_command(run.path, lane["id"]), *lane_validation(data, lane)]:
            allow += [f"Bash({command})", f"Bash({command} *)"]
    deny = DEFAULT_DENY.get(tool, []) + lane.get("deny", [])
    lists = {
        "{allow_args}": ["--allowedTools", ",".join(allow)] if allow else [],
        "{deny_args}": ["--disallowedTools", ",".join(deny)] if deny else [],
        "{budget_args}": ["--max-budget-usd", str(lane["budget_usd"])] if lane.get("budget_usd") else [],
    }
    scalars = {
        "{prompt}": prompt, "{model}": lane["model"], "{effort}": lane["effort"],
        "{session}": lane.get("session") or "", "{name}": f"{data['id']}-{lane['id']}",
        "{worktree}": lane.get("worktree") or str(run.path), "{gitdir}": gitdir or str(run.path),
        "{last}": str(run.lane_dir(lane["id"]) / f"attempt-{attempt}.last.md"),
        "{contract}": lane_cache.contract(data, lane, PLUGIN_ROOT),
        "{tools}": ",".join(LANE_TOOLS + sorted(set(lane.get("extra_tools", [])) - set(LANE_TOOLS))),
        "{mcp_config}": lane.get("mcp_config") or NO_MCP,
        "{review_dir}": str(run.lane_dir(lane["id"]) / "reviews"),
    }
    command: list[str] = []
    for token in template:
        if token in lists:
            command.extend(lists[token])
            continue
        # One pass, so text inside a substituted value (a brief that mentions {model}) is never rewritten.
        command.append(re.sub(r"\{[a-z_]+\}", lambda match: scalars.get(match.group(0), match.group(0)), token))
    if tool == "claude" and (review or template == DEFAULT_ADAPTERS["claude"]["resume" if resume else "new"]):
        command += lane_cache.claude_cache_flags(command[0])
    if tool == "codex" and data.get("codex_contract") == "developer":
        combined = data.get("codex_developer_prefix", "") + "\n\n" + scalars["{contract}"]
        command[2:2] = ["-c", "developer_instructions=" + json.dumps(combined)]
    return command


def start(run: Run, lane_id: str, message_file: str | None, resume: bool, dry_run: bool) -> None:
    # A dry run works on a copy, so it never freezes adapters or contracts into the run.
    with (contextlib.nullcontext(run.read()) if dry_run else run.locked()) as data:
        lane = get_lane(data, lane_id)
        refresh(run, data, lane)
        if lane["state"] == "running":
            raise LaneError(f"lane {lane_id} is running; wait for its turn to end or stop it")
        for blocker in lane.get("blocked_by", []):
            if data["lanes"].get(blocker, {}).get("state") != "merged" and not resume:
                raise LaneError(f"lane {lane_id} is blocked by {blocker} (not merged)")
        text = Path(message_file).read_text(encoding="utf-8") if message_file else ""
        if lane["kind"] == "review":
            prompt = lane_review.launch_prompt(sys.modules[__name__], data, lane)
            if text.strip():
                prompt += "\n\n" + text
        elif resume:
            if not text.strip():
                raise LaneError("send needs --message-file")
            if not lane_runtime.setup_current(data, lane):
                raise LaneError("lane setup is missing or stale; run 'lanectl setup' before resuming")
            prompt = text
        else:
            if not text.strip():
                raise LaneError("launch needs --brief-file")
            lane_runtime.ready(sys.modules[__name__], data, lane, text)
            draft = lane.get("brief_draft")
            if draft and (not draft["approved"] or lane_cache.digest(text) != draft["sha256"]):
                raise LaneError("generated brief needs coordinator approval; edited drafts need reapproval")
            prompt = render_assignment(data, lane, run.path) + "\n\n" + text
            template = run_adapters(data)[lane["tool"]]["new"]
            if not any("{contract}" in token for token in template) and data.get("codex_contract") != "developer":
                prompt = lane_cache.contract(data, lane, PLUGIN_ROOT) + "\n\n" + prompt
        if lane["kind"] == "review" and lane["tool"] == "codex" and not resume and data.get("codex_contract") != "developer":
            prompt = lane_cache.contract(data, lane, PLUGIN_ROOT) + "\n\n" + prompt
        attempt = lane.get("attempt", 0) + 1
        if lane.get("token_budget") and sum(u.get("in", 0) + u.get("out", 0) for u in lane.get("usage", {}).values()) >= lane["token_budget"]:
            raise LaneError("reported token budget is exhausted; choose a new budget before continuing")
        command = build_command(run, data, lane, prompt, resume, attempt)
        warnings = lane.get("launch_warnings") or []
        if dry_run:
            contract = lane_cache.contract(data, lane, PLUGIN_ROOT)
            printable = ["<prompt>" if c == prompt else "<contract>" if c == contract else c for c in command]
            print(json.dumps(printable))
            for warning in warnings:
                print(f"warning: {warning}", file=sys.stderr)
            return
        try:
            profile = lane_cache.profile(lane["tool"], lane, command,
                                         lane_cache.contract(data, lane, PLUGIN_ROOT), prompt)
            if lane.get("cache_profile") and profile["mcp_sha256"] != lane["cache_profile"]["mcp_sha256"]:
                raise LaneError("MCP configuration changed; use a fresh lane instead of changing a live session")
            lane["cache_profile"] = profile
        except (ValueError, OSError) as exc:
            raise LaneError(str(exc)) from exc
        ldir = run.lane_dir(lane_id)
        ldir.mkdir(parents=True, exist_ok=True)
        atomic_write(ldir / f"attempt-{attempt}.cmd.json", json.dumps(command) + "\n")
        atomic_write(ldir / f"attempt-{attempt}.prompt.md", prompt)
        cwd = lane.get("worktree") or data["repo"]
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "_supervise", str(ldir), str(attempt), cwd, str(run.path), lane_id],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        lane.update({"attempt": attempt, "pid": proc.pid, "state": "running", "started": now(),
                     "started_ts": time.time(), "exit_code": None, "error": None})
    print(f"lane {lane_id} {'resumed' if resume else 'launched'} (attempt {attempt}, pid {proc.pid})")
    for warning in warnings:
        print(f"warning: {warning}")


def cmd_launch(args) -> None:
    start(resolve_run(args.run), args.id, args.brief_file, resume=False, dry_run=args.dry_run)


def cmd_launch_group(args) -> None:
    run = resolve_run(args.run)
    if args.warm_timeout <= 0:
        raise LaneError("warm timeout must be positive")
    try:
        briefs = json.loads(Path(args.briefs_file).read_text())
    except (ValueError, OSError) as exc:
        raise LaneError("briefs file must map lane IDs to local brief paths") from exc
    if not isinstance(briefs, dict) or not briefs or not all(isinstance(v, str) for v in briefs.values()):
        raise LaneError("briefs file must be a nonempty object of lane IDs and paths")
    data = run.read()
    groups = {}
    for lane_id, brief in briefs.items():
        lane = get_lane(data, lane_id)
        if lane.get("attempt") or lane["state"] not in ("planned", "queued"):
            raise LaneError(f"{lane_id} already started or needs a coordinator decision")
        for blocker in lane.get("blocked_by", []):
            if data["lanes"].get(blocker, {}).get("state") != "merged":
                raise LaneError(f"{lane_id} is blocked by {blocker}")
        text = Path(brief).read_text()
        if not text.strip():
            raise LaneError(f"empty brief for {lane_id}")
        if lane["kind"] == "review":
            lane_review.launch_prompt(sys.modules[__name__], data, lane)
        else:
            lane_runtime.ready(sys.modules[__name__], data, lane, text)
            draft = lane.get("brief_draft")
            if draft and (not draft["approved"] or lane_cache.digest(text) != draft["sha256"]):
                raise LaneError(f"{lane_id} generated brief needs approval/reapproval")
        template = run_adapters(data)[lane["tool"]]["new"]
        shared = lane_cache.contract(data, lane, PLUGIN_ROOT)
        # Group hints are candidates, not a promise of identical hidden CLI context.
        grouping = lane_cache.digest({"tool": lane["tool"], "model": lane["model"], "effort": lane["effort"],
                                      "kind": lane["kind"], "contract": lane_cache.digest(shared),
                                      "tools": sorted(set(lane.get("extra_tools", []))),
                                      "mcp": lane_cache.mcp_digest(lane.get("mcp_config")), "adapter": template})
        groups.setdefault(grouping, []).append(lane_id)
    held = []
    for ids in groups.values():
        leader = ids[0]
        start(run, leader, briefs[leader], resume=False, dry_run=False)
        if args.warm_cache and len(ids) > 1:
            problem = warm_up(run, leader, args.warm_timeout)
            if problem:
                # Followers stay unstarted; other groups are independent and still launch.
                held.append(f"{leader} {problem}; followers not launched: {', '.join(ids[1:])}")
                continue
        for lane_id in ids[1:]:
            start(run, lane_id, briefs[lane_id], resume=False, dry_run=False)
    if held:
        raise LaneError("cache warm-up incomplete: " + " | ".join(held))


def warm_up(run: Run, leader: str, timeout: float) -> str | None:
    """Wait until the leader's first model call has been served, so followers can hit its cache."""
    deadline = time.monotonic() + timeout
    while True:
        with run.locked() as current:
            lane = refresh(run, current, get_lane(current, leader))
            info = parse_stream(run.lane_dir(leader) / f"attempt-{lane.get('attempt', 1)}.jsonl", lane["tool"])
        if lane["state"] in ("failed", "crashed", "stopped") or info["error"]:
            return "failed"
        # Claude reports the first response; Codex reports its first item once the first call returns.
        if info["response_started"] if lane["tool"] == "claude" else (info["last"] or info["done"]):
            return None
        if time.monotonic() >= deadline:
            return "timed out"
        time.sleep(0.2)


def cmd_send(args) -> None:
    run = resolve_run(args.run)
    message_file = args.message_file
    if args.review_from:
        with run.locked() as data:
            reviewer = get_lane(data, args.review_from)
            lane_review.launch_prompt(sys.modules[__name__], data, reviewer)
            if reviewer["review_of"] != args.id:
                raise LaneError("reviewer is assigned to a different target")
            manifest = reviewer["review"]
            if manifest.get("status") != "blocked" or not manifest.get("validated_report"):
                raise LaneError("validate blocking findings before forwarding them")
            report = json.loads(Path(manifest["validated_report"]).read_text())
            if lane_cache.digest(report) != manifest["report_sha256"]:
                raise LaneError("validated review report changed")
            if not any(item["blocking"] for item in report["findings"]):
                raise LaneError("review has missing evidence but no actionable blocking changes")
            target = get_lane(data, args.id)
            if target.get("correction_rounds", 0) >= 1 and not args.correction_override:
                raise LaneError("one correction round already used; ask the user before another")
            message_file = str(Path(manifest["packet"]).parent / "changes.md")
            if lane_cache.digest(Path(message_file).read_text()) != manifest["changes_sha256"]:
                raise LaneError("validated change list changed")
        if args.correction_override and not args.dry_run:
            run.note("directive", args.correction_override)
    start(run, args.id, message_file, resume=True, dry_run=args.dry_run)
    if args.review_from and not args.dry_run:
        with run.locked() as data:
            target = get_lane(data, args.id)
            target["correction_rounds"] = target.get("correction_rounds", 0) + 1


def cmd_supervise(args) -> None:
    ldir = Path(args.lane_dir)
    attempt = args.attempt
    command = json.loads((ldir / f"attempt-{attempt}.cmd.json").read_text(encoding="utf-8"))
    with open(ldir / f"attempt-{attempt}.jsonl", "w") as out, open(ldir / f"attempt-{attempt}.err", "w") as err:
        try:
            data = resolve_run(args.run_path).read() if args.run_path else {}
            lane = get_lane(data, args.lane_id) if args.lane_id else {}
            env = lane_runtime.environment(data, lane) if lane else None
            proc = subprocess.Popen(command, cwd=args.cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    start_new_session=True)
            atomic_write(ldir / f"attempt-{attempt}.child", str(proc.pid))

            def forward(signum, _frame):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signum)

            signal.signal(signal.SIGINT, forward)
            signal.signal(signal.SIGTERM, forward)
            budget = lane.get("token_budget")
            previous = sum(u.get("in", 0) + u.get("out", 0) for key, u in lane.get("usage", {}).items() if key != str(attempt))
            stream, seen = ldir / f"attempt-{attempt}.jsonl", -1
            while proc.poll() is None:
                size = stream.stat().st_size if stream.exists() else 0
                if budget and size != seen:
                    seen = size
                    info = parse_stream(stream, lane["tool"])
                    if previous + info["tokens_in"] + info["tokens_out"] >= budget:
                        atomic_write(ldir / f"attempt-{attempt}.guard", "reported token threshold reached; counts can arrive late")
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(proc.pid, signal.SIGTERM)
                        break
                time.sleep(0.5)
            try:
                code = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                code = proc.wait()
        except (OSError, ValueError, KeyError):
            err.write("lane executable or environment configuration unavailable\n")
            code = 127
    atomic_write(ldir / f"attempt-{attempt}.exit", f"{code}\n")


def cmd_stop(args) -> None:
    run = resolve_run(args.run)
    with run.locked() as data:
        lane = get_lane(data, args.id)
        if lane.get("state") != "running" or not pid_alive(lane.get("pid")):
            print(f"lane {args.id} is not running")
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(lane["pid"], signal.SIGINT)
        lane["state"] = "stopped"
    run.note("event", f"{args.id} stopped: {args.reason}")
    print(f"lane {args.id} stopped")


# ---------------------------------------------------------------- commands: observation

def fmt_cost(value: float) -> str:
    return "<$0.01" if 0 < value < 0.01 else f"${value:.2f}"


def fmt_tokens(value: int) -> str:
    return f"{value / 1000:.0f}k" if value >= 1000 else str(value)


def lane_line(lane: dict) -> str:
    usage = lane.get("usage", {})
    tokens_in = sum(u.get("in", 0) for u in usage.values())
    tokens_out = sum(u.get("out", 0) for u in usage.values())
    costs = [u.get("cost_usd") for u in usage.values() if u.get("cost_usd") is not None]
    cost = f" {fmt_cost(sum(costs))}" if costs else ""
    spend = f" in {fmt_tokens(tokens_in)}/out {fmt_tokens(tokens_out)}{cost}" if usage else ""
    items = ",".join(lane["items"]) or "-"
    extra = []
    if lane.get("report_status"):
        extra.append(f"report={lane['report_status']}")
    if lane.get("state") == "running" and lane.get("last_activity"):
        extra.append(f"last={lane['last_activity']}")
    if lane.get("pr"):
        extra.append(f"pr={lane['pr']}")
    if lane.get("blocked_by") and lane.get("state") in ("queued", "planned"):
        extra.append("after=" + ",".join(lane["blocked_by"]))
    return (f"{lane['id']:<5} {items:<14} {lane['tool']}/{lane['model']}@{lane['effort']:<7} "
            f"{lane['state']:<17}{spend} {' '.join(extra)}").rstrip()


def cmd_status(args) -> None:
    run = resolve_run(args.run)
    with run.locked() as data:
        lanes = [get_lane(data, args.id)] if args.id else list(data["lanes"].values())
        for lane in lanes:
            refresh(run, data, lane)
    if args.json:
        print(json.dumps({"run": data["id"], "mode": data["mode"], "lanes": lanes}, indent=2))
        return
    print(f"run {data['id']}  mode={data['mode']}  base={data['base']}")
    for lane in lanes:
        print(lane_line(lane))


def cmd_wait(args) -> None:
    run = resolve_run(args.run)
    deadline = time.monotonic() + args.timeout
    before = {lane_id: lane["state"] for lane_id, lane in run.read()["lanes"].items()}
    changed: dict[str, str] = {}
    settle_until = None
    while True:
        with run.locked() as data:
            for lane in data["lanes"].values():
                if lane["state"] == "running":
                    refresh(run, data, lane)
                if lane["state"] != before.get(lane["id"]):
                    changed[lane["id"]] = lane_line(lane)
        if changed:
            if settle_until is None:
                settle_until = time.monotonic() + args.settle
            still_running = any(lane["state"] == "running" for lane in data["lanes"].values())
            if time.monotonic() >= min(settle_until, deadline) or not still_running:
                print("\n".join(changed.values()))
                return
        if not any(state == "running" for state in before.values()):
            print("no running lanes")
            return
        if time.monotonic() >= deadline:
            print("timeout: no lane changed state")
            sys.exit(2)
        time.sleep(args.interval)


def cmd_report(args) -> None:
    run = resolve_run(args.run)
    with run.locked() as data:
        refresh(run, data, get_lane(data, args.id))
    report = run.lane_dir(args.id) / "report.md"
    print(report.read_text(encoding="utf-8").strip() if report.exists() else "(no report yet)")


def changed_files(lane: dict) -> set[str]:
    worktree = lane.get("worktree")
    if not worktree or not Path(worktree).exists():
        return set()
    files = set()
    merge_base = git("merge-base", lane["base"], "HEAD", cwd=worktree, check=False)
    if merge_base:
        files.update(filter(None, git("diff", "--name-only", merge_base, "HEAD", cwd=worktree).splitlines()))
    raw = subprocess.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=worktree,
                         text=True, capture_output=True, check=True).stdout
    entries = raw.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        files.add(entry[3:])
        if entry[0] in "RC":
            index += 1  # skip the rename/copy source path
    return files


def scope_problems(data: dict) -> list[str]:
    touched: dict[str, list[str]] = {}
    problems = []
    for lane in data["lanes"].values():
        if lane["state"] in ("merged", "closed") or lane["kind"] in ("review", "research"):
            continue
        files = changed_files(lane)
        for path in files:
            touched.setdefault(path, []).append(lane["id"])
        if lane["owns"]:
            outside = sorted(p for p in files if not any(fnmatch.fnmatch(p, g) for g in lane["owns"]))
            if outside:
                problems.append(f"{lane['id']} outside ownership: {', '.join(outside)}")
    overlaps = sorted(f"{path}: {', '.join(ids)}" for path, ids in touched.items() if len(ids) > 1)
    problems.extend(f"overlap {item}" for item in overlaps)
    return problems


def cmd_scope(args) -> None:
    problems = scope_problems(resolve_run(args.run).read())
    print("\n".join(problems) if problems else "scope ok")
    if problems:
        sys.exit(3)


def cmd_note(args) -> None:
    run = resolve_run(args.run)
    run.note(args.kind, args.text)
    print("ok")


def cmd_packet(args) -> None:
    run = resolve_run(args.run)
    with run.locked() as data:
        for lane in data["lanes"].values():
            refresh(run, data, lane)
    print(f"# Resume packet: {data['name']}\n")
    print(f"run: {run.path}\nrepo: {data['repo']}\nbase: {data['base']}\nmode: {data['mode']}"
          f"\neffort ceiling: {data['effort_ceiling']}")
    if data["rules"]["validate"]:
        print("validate: " + "; ".join(data["rules"]["validate"]))
    if data["rules"]["protected"]:
        print("protected: " + "; ".join(data["rules"]["protected"]))
    print("\n## Lanes")
    for lane in data["lanes"].values():
        print(lane_line(lane) + (f" session={lane['session']}" if lane.get("session") else ""))
    print("\n## Ledger")
    print((run.path / "ledger.md").read_text(encoding="utf-8").split("## Entries", 1)[-1].strip() or "(empty)")


def cmd_cleanup(args) -> None:
    run = resolve_run(args.run)
    with run.locked() as data:
        lane = get_lane(data, args.id)
        refresh(run, data, lane)
        if lane["state"] == "running":
            raise LaneError("stop the lane before cleanup")
        repo = Path(data["repo"])
        done = []
        if args.remove_worktree and lane.get("worktree") and Path(lane["worktree"]).exists():
            git("worktree", "remove", lane["worktree"], cwd=repo)
            done.append("worktree removed")
            lane_runtime.release(sys.modules[__name__], run, args.id)
        if args.delete_branch and lane.get("branch"):
            git("branch", "-d", lane["branch"], cwd=repo)
            done.append(f"branch {lane['branch']} deleted")
    print(", ".join(done) or "nothing to clean")


# ---------------------------------------------------------------- repo profile (AGENTS.md)

def agents_file(root: Path) -> Path:
    path = root / "AGENTS.md"
    if path.is_symlink():
        raise LaneError(f"refusing to use symbolic-link {path}")
    return path


def read_profile(root: Path) -> dict[str, str]:
    path = agents_file(root)
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if MANAGED_START not in text or MANAGED_END not in text:
        return {}
    block = text.split(MANAGED_START, 1)[1].split(MANAGED_END, 1)[0]
    profile = {}
    for match in re.finditer(r"^- ([a-z_]+): (.+)$", block, re.MULTILINE):
        if match.group(1) in PROFILE_KEYS:
            profile[match.group(1)] = match.group(2).strip()
    return profile


def render_profile(profile: dict[str, str]) -> str:
    lines = [f"- {key}: {profile[key]}" for key in PROFILE_KEYS if profile.get(key)]
    return (f"{MANAGED_START}\n## Agent Lanes\n\n"
            "Repository profile for agent-lanes coordinators. Lanes follow the rest of this file.\n\n"
            + "\n".join(lines) + f"\n{MANAGED_END}")


def cmd_profile_show(args) -> None:
    profile = read_profile(repo_root(args.repo))
    if not profile:
        print("no agent-lanes profile in AGENTS.md")
    for key in PROFILE_KEYS:
        if key in profile:
            print(f"{key}: {profile[key]}")
    missing = [key for key in PROFILE_KEYS if key not in profile]
    if profile and missing:
        print("missing: " + ", ".join(missing))


def cmd_profile_set(args) -> None:
    root = repo_root(args.repo)
    path = agents_file(root)
    profile = read_profile(root)
    for pair in args.pairs:
        key, sep, value = pair.partition("=")
        key = key.strip().replace("-", "_")
        if not sep or key not in PROFILE_KEYS:
            raise LaneError(f"expected key=value with key in: {', '.join(PROFILE_KEYS)}")
        value = " ".join(value.split())
        if value:
            profile[key] = value
        else:
            profile.pop(key, None)
    block = render_profile(profile)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if (MANAGED_START in current) != (MANAGED_END in current):
        raise LaneError(f"refusing to edit {path}: agent-lanes markers are incomplete")
    if MANAGED_START in current:
        before, rest = current.split(MANAGED_START, 1)
        after = rest.split(MANAGED_END, 1)[1]
        text = f"{before.rstrip()}\n\n{block}{after.rstrip()}\n".lstrip()
    elif current.strip():
        text = f"{current.rstrip()}\n\n{block}\n"
    else:
        text = f"# Agent Instructions\n\n{block}\n"
    atomic_write(path, text)
    print(f"{path.name} updated (uncommitted)")


# ---------------------------------------------------------------- compact validation

def failure_lines(output: str, tail: int) -> list[str]:
    lines = [line.rstrip()[:300] for line in output.splitlines() if line.strip()]
    picked = [line for line in lines if FAILURE_LINE.search(line)][-tail:]
    for line in lines[-5:]:
        if line not in picked:
            picked.append(line)
    return picked or ["(no output)"]


def run_checks(data: dict, lane: dict, timeout: float, tail: int) -> tuple[bool, list[str]]:
    if lane["kind"] not in ("review", "research") and not lane_runtime.legacy(data):
        try:
            lane_runtime.isolated(sys.modules[__name__], data, lane)
            if not lane_runtime.setup_current(data, lane):
                return False, ["FAIL setup missing/stale; run 'lanectl setup' before validation"]
        except (LaneError, ValueError, OSError, KeyError, subprocess.TimeoutExpired):
            return False, ["FAIL validation needs an isolated prepared worktree"]
    commands = lane_validation(data, lane)
    if not commands:
        return True, ["no validation commands configured"]
    cwd = lane.get("worktree") or data["repo"]
    ok, out = True, []
    for command in commands:
        began = time.monotonic()
        code, output = lane_runtime.run_shell(command, cwd, lane_runtime.environment(data, lane), timeout)
        secs = time.monotonic() - began
        if command == data["rules"].get("secret_scan"):
            ok &= code == 0
            out.append(f"{'PASS' if code == 0 else 'FAIL'} secret scan (exit {code}; output redacted)")
            continue
        if code == 0:
            out.append(f"PASS {command} ({secs:.0f}s)")
        else:
            ok = False
            out.append(f"FAIL {command} exit {code} ({secs:.0f}s)")
            out.extend(f"  {line}" for line in failure_lines(output, tail))
    return ok, out


def cmd_check(args) -> None:
    run = resolve_run(args.run)
    data = run.read()
    ok, lines = run_checks(data, get_lane(data, args.id), args.timeout, args.tail)
    print("\n".join(lines))
    if not ok:
        sys.exit(4)
    if args.baseline_command and not lane_integration.baseline(sys.modules[__name__], run, data, get_lane(data, args.id),
                                                             args.baseline_command, args.timeout, args.tail):
        raise LaneError("baseline evidence is inconclusive or tests also pass on base")


# ---------------------------------------------------------------- repository map

def cmd_map(args) -> None:
    root = Path(resolve_run(args.run).read()["repo"]) if args.run else repo_root(args.repo)
    terms = csv(args.terms)
    if not terms:
        raise LaneError("--terms needs at least one search term")
    pathspec = ["--", *csv(args.paths)] if args.paths else []
    files = git("ls-files", *pathspec, cwd=root).splitlines()
    found: dict[str, dict] = {}
    for term in terms:
        for path in files:
            if term.lower() in path.lower():
                found.setdefault(path, {"terms": set(), "hits": 0, "defs": {}})["terms"].add(term)
        raw = subprocess.run(["git", "grep", "-I", "-i", "-n", "-z", "-F", "-e", term, *pathspec],
                             cwd=root, text=True, capture_output=True).stdout
        for line in raw.splitlines():
            parts = line.split("\0", 2)
            if len(parts) != 3:
                continue
            path, number, text = parts
            entry = found.setdefault(path, {"terms": set(), "hits": 0, "defs": {}})
            entry["terms"].add(term)
            entry["hits"] += 1
            if DEFINITION.match(text) and len(entry["defs"]) < args.defs:
                entry["defs"].setdefault(int(number), text.strip()[:120])
    ranked = sorted(found.items(), key=lambda item: (-len(item[1]["terms"]), -item[1]["hits"], item[0]))
    shown = ranked[:args.limit]
    rows = []
    for path, entry in shown:
        with contextlib.suppress(OSError):
            entry["lines"] = (root / path).read_bytes().count(b"\n")
        rows.append({"path": path, "lines": entry.get("lines"), "terms": sorted(entry["terms"]),
                     "hits": entry["hits"], "defs": [f"{n}: {t}" for n, t in sorted(entry["defs"].items())]})
    if args.json:
        print(json.dumps({"matched": len(found), "files": rows}, indent=2))
        return
    total = sum(row["lines"] or 0 for row in rows)
    print(f"map: {len(terms)} terms, {len(found)} files matched, showing {len(rows)} (~{total} lines)")
    for row in rows:
        print(f"{row['path']}  {row['lines']} lines  terms={','.join(row['terms'])}  hits={row['hits']}")
        for item in row["defs"]:
            print(f"  {item}")


# ---------------------------------------------------------------- usage and questions

def fmt_secs(secs: float | None) -> str:
    if secs is None:
        return "-"
    minutes, seconds = divmod(int(secs), 60)
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


def lane_usage(lane: dict) -> dict:
    total = {"attempts": len(lane.get("usage", {})), "in": 0, "fresh": 0, "cache_write": 0, "cache_read": 0,
             "out": 0, "cost_usd": 0.0, "secs": 0, "first_calls": [], "cache_write_known": True,
             "cost_known": True}
    for usage in lane.get("usage", {}).values():
        for key in ("in", "fresh", "cache_write", "cache_read", "out", "secs"):
            total[key] += usage.get(key) or 0
        total["cost_usd"] += usage.get("cost_usd") or 0.0
        total["cache_write_known"] &= usage.get("cache_write") is not None
        total["cost_known"] &= usage.get("cost_usd") is not None
        if usage.get("first_call"):
            total["first_calls"].append({**usage["first_call"], "resumed": usage.get("resumed", False),
                                         "cache_profile": usage.get("cache_profile")})
    if not total["cache_write_known"]:
        total["cache_write"] = None
    if not total["cost_known"]:
        total["cost_usd"] = None
    return total


def cmd_usage(args) -> None:
    run = resolve_run(args.run)
    with run.locked() as data:
        for lane in data["lanes"].values():
            refresh(run, data, lane)
    rows = {lane_id: lane_usage(lane) for lane_id, lane in data["lanes"].items()}
    keys = ("in", "fresh", "cache_write", "cache_read", "out", "cost_usd", "secs", "attempts")
    totals = {key: sum(row[key] or 0 for row in rows.values()) for key in keys}
    totals["cache_write_known"] = all(row["cache_write_known"] for row in rows.values())
    totals["cost_known"] = all(row["cost_known"] for row in rows.values())
    if not totals["cache_write_known"]:
        totals["cache_write"] = None
    if not totals["cost_known"]:
        totals["cost_usd"] = None
    if args.json:
        print(json.dumps({"lanes": rows, "total": totals}, indent=2))
        return
    print(f"{'lane':<6}{'turns':>6}{'fresh':>9}{'cache-w':>9}{'cache-r':>9}{'out':>8}{'cost':>9}{'time':>9}")
    for lane_id, row in [*rows.items(), ("total", totals)]:
        writes = fmt_tokens(row["cache_write"]) if row["cache_write_known"] else "unknown"
        print(f"{lane_id:<6}{row['attempts']:>6}{fmt_tokens(row['fresh']):>9}{writes:>9}"
              f"{fmt_tokens(row['cache_read']):>9}{fmt_tokens(row['out']):>8}"
              f"{(fmt_cost(row['cost_usd']) if row.get('cost_known', all(r['cost_known'] for r in rows.values())) else 'unknown'):>9}"
              f"{fmt_secs(row['secs']):>9}")


def cmd_questions(args) -> None:
    run = resolve_run(args.run)
    with run.locked() as data:
        for lane in data["lanes"].values():
            refresh(run, data, lane)
    pending = []
    for lane in data["lanes"].values():
        if lane.get("state") == "running" or lane.get("report_status") not in ("question", "blocked"):
            continue
        report = run.lane_dir(lane["id"]) / "report.md"
        text = report.read_text(encoding="utf-8") if report.exists() else ""
        match = re.search(r"^\s*Question/Blocker:\s*(.+)$", text, re.MULTILINE)
        detail = match.group(1).strip() if match else "(see lanectl report)"
        pending.append(f"{len(pending) + 1}. {lane['id']} ({','.join(lane['items']) or '-'}) "
                       f"{lane['report_status']}: {detail}")
    print("\n".join(pending) if pending else "no open questions")


# ---------------------------------------------------------------- merge queue

def reserve_hold(data: dict, lane: dict) -> str | None:
    """A lane holding resource key=v waits while an unmerged lane holds the same key with a lower value."""
    def order(value: str):
        return (0, int(value), value) if value.isdigit() else (1, 0, value)

    for reserve in lane.get("reserves", []):
        key, _, value = reserve.partition("=")
        for other in data["lanes"].values():
            if other is lane or other.get("state") in ("merged", "closed"):
                continue
            for theirs in other.get("reserves", []):
                other_key, _, other_value = theirs.partition("=")
                if other_key == key and value and other_value and order(other_value) < order(value):
                    return f"waits for {other['id']} ({theirs})"
    return None


def queue_lines(data: dict) -> list[str]:
    queued = sorted((lane for lane in data["lanes"].values() if lane.get("state") == "merge-queued"),
                    key=lambda lane: lane.get("queued_at") or "")
    lines, head = [], None
    for position, lane in enumerate(queued, 1):
        hold = reserve_hold(data, lane)
        unknown = [dep for dep in lane["blocked_by"] if dep not in data["lanes"]]
        if unknown:
            hold = "unknown dependency " + ", ".join(unknown)
        elif any(data["lanes"][dep]["state"] != "merged" for dep in lane["blocked_by"]):
            hold = "waits for dependencies"
        if lane.get("needs_rebase"):
            hold = "needs rebase"
        approval = lane.get("review_approval")
        if not hold and approval and lane_head(lane) != approval["head_sha"]:
            hold = "needs re-review (commit changed)"
        if not hold and lane_review.dirty(sys.modules[__name__], lane):
            hold = "uncommitted changes"
        if not hold and data.get("integration_required") and not lane_integration.current(sys.modules[__name__], data):
            hold = "needs integration proof"
        if not hold and head is None:
            head = lane["id"]
        label = "next" if head == lane["id"] else (hold or "waiting")
        lines.append(f"{position}. {lane['id']} {','.join(lane['items']) or '-'} {lane.get('branch') or '-'}  {label}")
    return lines


def cmd_queue(args) -> None:
    run = resolve_run(args.run)
    with run.locked() as data:
        for lane_id in csv(args.add):
            lane = get_lane(data, lane_id)
            approval = lane.get("review_approval")
            if approval and lane_review.clean_head(sys.modules[__name__], lane) != approval["head_sha"]:
                raise LaneError(f"{lane_id} changed after review; prepare a correction review")
            if approval:
                if (approval["validation_sha256"] != lane_cache.digest(lane_validation(data, lane))
                        or approval["protected_sha256"] != lane_cache.digest(data["rules"]["protected"])):
                    raise LaneError(f"{lane_id} gates changed after review; prepare a new review")
                approved_report = json.loads(Path(approval["report_file"]).read_text())
                if lane_cache.digest(approved_report) != approval["report_sha256"]:
                    raise LaneError(f"{lane_id} approved report changed")
            if (lane.get("review_requested") or lane_review.risk(sys.modules[__name__], data, lane, [])["review_required"]) and not approval:
                raise LaneError(f"{lane_id} requires a validated review before joining the merge queue")
            lane.update({"state": "merge-queued", "queued_at": now()})
        for lane_id in csv(args.remove):
            get_lane(data, lane_id).update({"state": "in-review", "queued_at": None})
        lines = queue_lines(data)
    print("\n".join(lines) if lines else "merge queue empty")


def lane_head(lane: dict) -> str:
    worktree = lane.get("worktree")
    if not worktree or not Path(worktree).is_dir():
        return ""
    return git("rev-parse", "HEAD", cwd=worktree, check=False)


def patch_id(worktree: str, base: str, head: str) -> str:
    """Identity of a lane's own changes, independent of where they sit on the base."""
    diff = subprocess.run(["git", "diff", "--no-ext-diff", base, head], cwd=worktree, capture_output=True).stdout
    out = subprocess.run(["git", "patch-id", "--stable"], cwd=worktree, input=diff, capture_output=True).stdout
    return out.split()[0].decode() if out.split() else ""


def sync_lanes(run: Run, data: dict, fetch: bool, check: bool, timeout: float, tail: int) -> list[str]:
    """Rebase idle lanes under the run lock. Checks run afterwards, without the lock (check_synced)."""
    repo = Path(data["repo"])
    base = data["base"]
    target = base
    if git("remote", cwd=repo, check=False).split().count("origin"):
        if fetch:
            git("fetch", "--quiet", "origin", base, cwd=repo, check=False)
        if git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base}", cwd=repo, check=False):
            target = f"origin/{base}"
    out = []
    for lane in data["lanes"].values():
        worktree = lane.get("worktree")
        if (lane.get("state") in ("merged", "closed") or lane.get("kind") in ("review", "research")
                or not worktree or not Path(worktree).exists()):
            continue
        refresh(run, data, lane)
        lane_id = lane["id"]
        if lane["state"] == "running":
            lane["needs_rebase"] = True
            out.append(f"{lane_id} running: sync again after its turn ends")
            continue
        if git("status", "--porcelain", cwd=worktree, check=False):
            lane["needs_rebase"] = True
            out.append(f"{lane_id} has uncommitted changes: send it the REBASE instruction")
            continue
        if subprocess.run(["git", "merge-base", "--is-ancestor", target, "HEAD"], cwd=worktree,
                          capture_output=True).returncode == 0:
            lane.update({"base": target, "needs_rebase": False})
            line = f"{lane_id} up to date"
        else:
            old_head = lane_head(lane)
            before = patch_id(worktree, git("merge-base", target, old_head, cwd=worktree, check=False) or target, old_head)
            proc = subprocess.run(["git", "rebase", "--quiet", target], cwd=worktree, text=True, capture_output=True)
            if proc.returncode != 0:
                conflicts = git("diff", "--name-only", "--diff-filter=U", cwd=worktree, check=False).split()
                git("rebase", "--abort", cwd=worktree, check=False)
                lane["needs_rebase"] = True
                out.append(f"{lane_id} conflict in {', '.join(conflicts) or 'unknown files'}: send it the REBASE instruction")
                continue
            lane.update({"base": target, "needs_rebase": False})
            line = f"{lane_id} rebased cleanly onto {target}"
            new_head = lane_head(lane)
            approval = lane.get("review_approval")
            if approval and before and before == patch_id(worktree, target, new_head):
                # Same changes on a newer base: the review still applies to them.
                approval.update({"head_sha": new_head, "carried_from": old_head})
                line += "; review approval carried forward (same changes)"
            elif approval:
                lane.pop("review_approval", None)
                line += "; review approval cleared (changes differ after rebase)"
                if lane.get("state") == "merge-queued":
                    lane.update({"state": "in-review", "queued_at": None})
                    line += "; removed from queue: prepare a correction review, then queue again"
            if lane.get("pr") or lane.get("state") == "merge-queued":
                line += "; push with --force-with-lease"
        if check:
            lane["_check"] = True
        out.append(line)
    return out


def check_synced(run: Run, lines: list[str], timeout: float, tail: int) -> list[str]:
    """Run checks for lanes marked by sync_lanes, outside the run lock. Stale setup is redone first."""
    c = sys.modules[__name__]
    with run.locked() as data:
        pending = {lane["id"]: lane_head(lane) for lane in data["lanes"].values() if lane.pop("_check", False)}
        snapshot = copy.deepcopy(data)
    results = {}
    for lane_id, head in pending.items():
        lane = get_lane(snapshot, lane_id)
        setup_lines: list[str] = []
        if not lane_runtime.setup_current(snapshot, lane):
            ok, setup_lines = lane_runtime.setup(c, snapshot, lane, timeout)
            if not ok:
                results[lane_id] = (False, ["FAIL setup after rebase: " + "; ".join(setup_lines)], lane.get("setup"))
                continue
        ok, check_lines = run_checks(snapshot, lane, timeout, tail)
        results[lane_id] = (ok, check_lines, lane.get("setup"))
    with run.locked() as data:
        for lane_id, (ok, check_lines, setup_state) in results.items():
            lane = get_lane(data, lane_id)
            if lane_head(lane) != pending[lane_id]:
                lines.append(f"{lane_id} moved during checks; run sync --check again")
                continue
            if setup_state:
                lane["setup"] = setup_state
            index = next((i for i, line in enumerate(lines) if line.startswith(lane_id + " ")), None)
            note = "; check pass" if ok else "; check FAILED:\n" + "\n".join(check_lines)
            if index is None:
                lines.append(lane_id + note)
            else:
                lines[index] += note
            if not ok:
                if lane["state"] != "blocked":
                    lane["unblock_to"] = lane["state"]
                lane["state"] = "blocked"
                lane.pop("review_approval", None)
    return lines


def cmd_sync(args) -> None:
    run = resolve_run(args.run)
    with run.locked() as data:
        lines = sync_lanes(run, data, not args.no_fetch, args.check, args.timeout, args.tail)
    if args.check:
        lines = check_synced(run, lines, args.timeout, args.tail)
    print("\n".join(lines) if lines else "no lanes to sync")
    if args.check and any("check FAILED" in line for line in lines):
        sys.exit(4)


def cmd_merged(args) -> None:
    run = resolve_run(args.run)
    with run.locked() as data:
        lane = get_lane(data, args.id)
        lane.update({"state": "merged", "queued_at": None, "needs_rebase": False})
        if args.pr:
            lane["pr"] = args.pr
        lines = [] if args.no_sync else sync_lanes(run, data, True, args.check, args.timeout, args.tail)
    if args.check and not args.no_sync:
        lines = check_synced(run, lines, args.timeout, args.tail)
    queue = queue_lines(run.read())
    run.note("merge", f"{args.id} merged" + (f" ({args.pr})" if args.pr else "") + (f" as {args.commit}" if args.commit else ""))
    print(f"lane {args.id} merged")
    if lines:
        print("\n".join(lines))
    print("queue: " + ("; ".join(queue) if queue else "empty"))
    if args.check and any("check FAILED" in line for line in lines):
        sys.exit(4)


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lanectl", description="agent-lanes state and process control")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Check CLIs and state location.").set_defaults(func=cmd_doctor)

    run = sub.add_parser("run", help="Create, list, or update runs.")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    new = run_sub.add_parser("new")
    new.add_argument("--repo", default=".")
    new.add_argument("--name", required=True)
    new.add_argument("--base", help="default: the AGENTS.md profile's base")
    new.add_argument("--mode", choices=("review", "merge-on-green"), help="default: profile mode, else review")
    new.add_argument("--codex-contract", choices=("prompt", "developer"), default="prompt",
                     help="developer is an opt-in cache-boundary experiment")
    new.add_argument("--codex-developer-prefix-file", help="preserve effective existing developer instructions for experiment")
    new.add_argument("--setup"); new.add_argument("--setup-not-required", action="store_true")
    new.add_argument("--env-template-file"); new.add_argument("--integration", choices=("required", "optional"))
    new.set_defaults(func=cmd_run_new)
    lst = run_sub.add_parser("list")
    lst.add_argument("--repo", default=".")
    lst.add_argument("--all", action="store_true")
    lst.set_defaults(func=cmd_run_list)
    rset = run_sub.add_parser("set")
    rset.add_argument("--run", required=True)
    rset.add_argument("--mode", choices=("review", "merge-on-green"))
    rset.add_argument("--base")
    rset.add_argument("--validate", help="validation commands, ';'-separated")
    rset.add_argument("--protected", help="comma-separated protected environments/rules")
    rset.add_argument("--risk-patterns", help="additional comma-separated sensitive path globs")
    rset.add_argument("--state", choices=("open", "closed"))
    rset.add_argument("--setup"); rset.add_argument("--setup-not-required", action="store_true")
    rset.add_argument("--env-template-file"); rset.add_argument("--integration", choices=("required", "optional"))
    rset.set_defaults(func=cmd_run_set)

    lane = sub.add_parser("lane", help="Add or update lanes.")
    lane_sub = lane.add_subparsers(dest="lane_command", required=True)
    add = lane_sub.add_parser("add")
    add.add_argument("--run", required=True)
    add.add_argument("--id", required=True)
    add.add_argument("--items", default="")
    add.add_argument("--kind", choices=KINDS, default="implement")
    add.add_argument("--review-of", help="required for review lanes; exact-commit snapshot of this lane")
    add.add_argument("--tool", choices=TOOLS, required=True)
    add.add_argument("--model", required=True)
    add.add_argument("--effort", required=True)
    add.add_argument("--override", help="the user's explicit instruction allowing effort above the ceiling")
    add.add_argument("--worktree", help="path, or 'auto' for <run>/worktrees/<id>")
    add.add_argument("--create-worktree", action="store_true")
    add.add_argument("--branch")
    add.add_argument("--base")
    add.add_argument("--owns", default="", help="comma-separated globs this lane may change")
    add.add_argument("--reserves", default="", help="comma-separated reserved resources")
    add.add_argument("--allow", default="", help="extra allowed tool rules (claude)")
    add.add_argument("--deny", default="", help="extra denied tool rules (claude)")
    add.add_argument("--budget-usd", type=float)
    add.add_argument("--token-budget", type=int, help="soft reported-token limit; may overshoot within a CLI turn")
    add.add_argument("--no-validation-required", help="user directive waiving validation for this lane")
    add.add_argument("--start", default="", help="comma-separated files the lane should open first (from map)")
    add.add_argument("--validate", help="this lane's validation commands, ';'-separated (default: the run's)")
    add.add_argument("--extra-tools", default="", help="extra Claude tools, e.g. WebFetch,WebSearch")
    add.add_argument("--mcp-config", help="MCP config JSON for this Claude lane (default: no MCP servers)")
    add.add_argument("--blocked-by", default="")
    add.add_argument("--queued", action="store_true")
    add.set_defaults(func=cmd_lane_add)
    lset = lane_sub.add_parser("set")
    lset.add_argument("--run", required=True)
    lset.add_argument("--id", required=True)
    lset.add_argument("--state")
    lset.add_argument("--pr")
    lset.add_argument("--model")
    lset.add_argument("--branch")
    lset.add_argument("--effort")
    lset.add_argument("--override")
    lset.add_argument("--owns")
    lset.add_argument("--start")
    lset.add_argument("--validate")
    lset.add_argument("--token-budget", type=int)
    lset.set_defaults(func=cmd_lane_set)

    launch = sub.add_parser("launch", help="Start a lane's first turn in the background.")
    launch.add_argument("--run", required=True)
    launch.add_argument("--id", required=True)
    launch.add_argument("--brief-file", required=True)
    launch.add_argument("--dry-run", action="store_true")
    launch.set_defaults(func=cmd_launch)

    group = sub.add_parser("launch-group", help="Launch ready lanes, optionally warm each candidate cache group.")
    group.add_argument("--run", required=True)
    group.add_argument("--briefs-file", required=True, help="JSON object mapping lane IDs to brief paths")
    group.add_argument("--warm-cache", action="store_true")
    group.add_argument("--warm-timeout", type=float, default=30)
    group.set_defaults(func=cmd_launch_group)

    send = sub.add_parser("send", help="Resume a lane's session with a new message.")
    send.add_argument("--run", required=True)
    send.add_argument("--id", required=True)
    # Reviewer lanes can be resumed on a freshly prepared packet without a message.
    source = send.add_mutually_exclusive_group()
    source.add_argument("--message-file", help="required for non-review lanes")
    source.add_argument("--review-from", help="forward only a validated, current blocking change list")
    send.add_argument("--correction-override", help="user's exact authorization for another correction round")
    send.add_argument("--dry-run", action="store_true")
    send.set_defaults(func=cmd_send)

    stop = sub.add_parser("stop", help="Interrupt a running lane.")
    stop.add_argument("--run", required=True)
    stop.add_argument("--id", required=True)
    stop.add_argument("--reason", required=True)
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status", help="Compact lane status with tokens and cost.")
    status.add_argument("--run", required=True)
    status.add_argument("--id")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    wait = sub.add_parser("wait", help="Block until a running lane changes state.")
    wait.add_argument("--run", required=True)
    wait.add_argument("--timeout", type=float, default=600)
    wait.add_argument("--interval", type=float, default=5)
    wait.add_argument("--settle", type=float, default=0,
                      help="after the first change, keep collecting changes for this many seconds")
    wait.set_defaults(func=cmd_wait)

    report = sub.add_parser("report", help="Print a lane's final report only.")
    report.add_argument("--run", required=True)
    report.add_argument("--id", required=True)
    report.set_defaults(func=cmd_report)

    scope = sub.add_parser("scope", help="Check lanes for out-of-ownership changes and overlaps.")
    scope.add_argument("--run", required=True)
    scope.set_defaults(func=cmd_scope)

    note = sub.add_parser("note", help="Append a ledger entry.")
    note.add_argument("--run", required=True)
    note.add_argument("--kind", choices=("directive", "decision", "question", "next", "event", "merge"),
                      required=True)
    note.add_argument("--text", required=True)
    note.set_defaults(func=cmd_note)

    packet = sub.add_parser("packet", help="Print the compact resume packet.")
    packet.add_argument("--run", required=True)
    packet.set_defaults(func=cmd_packet)

    cleanup = sub.add_parser("cleanup", help="Remove a lane worktree and/or merged branch (safe git only).")
    cleanup.add_argument("--run", required=True)
    cleanup.add_argument("--id", required=True)
    cleanup.add_argument("--remove-worktree", action="store_true")
    cleanup.add_argument("--delete-branch", action="store_true")
    cleanup.set_defaults(func=cmd_cleanup)

    profile = sub.add_parser("profile", help="Show or set the repo profile in AGENTS.md.")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)
    pshow = profile_sub.add_parser("show")
    pshow.add_argument("--repo", default=".")
    pshow.set_defaults(func=cmd_profile_show)
    pset = profile_sub.add_parser("set")
    pset.add_argument("--repo", default=".")
    pset.add_argument("pairs", nargs="+", metavar="key=value", help="empty value removes the key")
    pset.set_defaults(func=cmd_profile_set)

    check = sub.add_parser("check", help="Run a lane's validation; print only pass/fail and failing lines.")
    check.add_argument("--run", required=True)
    check.add_argument("--id", required=True)
    check.add_argument("--timeout", type=float, default=1800)
    check.add_argument("--tail", type=int, default=30)
    check.add_argument("--baseline-command", help="optional test-only regression command to run on base")
    check.set_defaults(func=cmd_check)

    mapper = sub.add_parser("map", help="Rank files and definitions matching ticket terms.")
    mapper.add_argument("--repo", default=".")
    mapper.add_argument("--run")
    mapper.add_argument("--terms", required=True, help="comma-separated search terms")
    mapper.add_argument("--paths", help="comma-separated pathspecs to limit the search")
    mapper.add_argument("--limit", type=int, default=12)
    mapper.add_argument("--defs", type=int, default=3, help="definition lines shown per file")
    mapper.add_argument("--json", action="store_true")
    mapper.set_defaults(func=cmd_map)

    usage = sub.add_parser("usage", help="Token, cost and time breakdown per lane.")
    usage.add_argument("--run", required=True)
    usage.add_argument("--json", action="store_true")
    usage.set_defaults(func=cmd_usage)

    questions = sub.add_parser("questions", help="List every open lane question as one batch.")
    questions.add_argument("--run", required=True)
    questions.set_defaults(func=cmd_questions)

    queue = sub.add_parser("queue", help="Show or change the merge queue.")
    queue.add_argument("--run", required=True)
    queue.add_argument("--add", default="", help="lane ids to enqueue")
    queue.add_argument("--remove", default="", help="lane ids to take out of the queue")
    queue.set_defaults(func=cmd_queue)

    sync = sub.add_parser("sync", help="Rebase idle lanes onto the moved base; report conflicts.")
    sync.add_argument("--run", required=True)
    sync.add_argument("--no-fetch", action="store_true")
    sync.add_argument("--check", action="store_true", help="run each rebased lane's validation")
    sync.add_argument("--timeout", type=float, default=1800)
    sync.add_argument("--tail", type=int, default=30)
    sync.set_defaults(func=cmd_sync)

    merged = sub.add_parser("merged", help="Record a merge, then sync the remaining lanes.")
    merged.add_argument("--run", required=True)
    merged.add_argument("--id", required=True)
    merged.add_argument("--pr")
    merged.add_argument("--commit")
    merged.add_argument("--no-sync", action="store_true")
    merged.add_argument("--check", action="store_true")
    merged.add_argument("--timeout", type=float, default=1800)
    merged.add_argument("--tail", type=int, default=30)
    merged.set_defaults(func=cmd_merged)

    sup = sub.add_parser("_supervise")
    sup.add_argument("lane_dir")
    sup.add_argument("attempt", type=int)
    sup.add_argument("cwd")
    sup.add_argument("run_path", nargs="?"); sup.add_argument("lane_id", nargs="?")
    sup.set_defaults(func=cmd_supervise)
    lane_review.add_parsers(sys.modules[__name__], sub)
    lane_runtime.add_parsers(sys.modules[__name__], sub)
    lane_planning.add_parsers(sys.modules[__name__], sub)
    lane_integration.add_parsers(sys.modules[__name__], sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except subprocess.TimeoutExpired as exc:
        print(f"error: command timed out after {exc.timeout:.0f}s: {exc.cmd if isinstance(exc.cmd, str) else ' '.join(map(str, exc.cmd))}",
              file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"error: command failed (exit {exc.returncode}): {' '.join(map(str, exc.cmd)) if isinstance(exc.cmd, list) else exc.cmd}",
              file=sys.stderr)
        return 1
    except (LaneError, ValueError, OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
