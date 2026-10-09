"""Dependency setup, reserved test namespaces, and launch readiness. No model calls."""
from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import platform
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from lane_cache import digest

LOCKFILES = ("pnpm-lock.yaml", "package-lock.json", "yarn.lock", "uv.lock", "poetry.lock", "Pipfile.lock",
             "requirements.txt", "Cargo.lock", "go.sum", "Gemfile.lock")


@contextlib.contextmanager
def leases(c):
    root = c.home(); root.mkdir(parents=True, exist_ok=True)
    with open(root / ".resources.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        path = root / "resources.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        # A removed run cannot still own a reservation. Live/existing runs are never reclaimed implicitly.
        state = {key: value for key, value in state.items() if Path(value["run"]).exists()}
        yield state
        c.atomic_write(path, json.dumps(state, indent=2) + "\n")


def reserve(c, run, lane_id: str) -> dict:
    key = str(run.path) + ":" + lane_id
    with leases(c) as state:
        if key not in state:
            used = {item["port_start"] for item in state.values()}
            selected = None
            probed = True
            for start in range(20000, 60000, 16):
                if start in used:
                    continue
                held = []
                try:
                    for port in range(start, start + 16):
                        sock = socket.socket(); held.append(sock)
                        sock.bind(("127.0.0.1", port))
                    selected = start
                    break
                except PermissionError:
                    # Sandboxed coordinators may not bind sockets. Preserve unique logical leases,
                    # and explicitly report that availability was not probed.
                    selected = start; probed = False; break
                except OSError:
                    pass
                finally:
                    for sock in held:
                        sock.close()
            if selected is None:
                raise c.LaneError("no test port range is available")
            state[key] = {"run": str(run.path), "port_start": selected,
                          "namespace": "lane_" + digest(key)[:16], "probed": probed}
        allocation = state[key]
    temp = run.path / "lanes" / lane_id / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    cache = c.home() / "package-cache"
    cache.mkdir(parents=True, exist_ok=True)
    return {"LANE_ID": lane_id, "LANE_NAMESPACE": allocation["namespace"],
            "LANE_DB_SUFFIX": allocation["namespace"], "LANE_PORT_START": str(allocation["port_start"]),
            "LANE_PORT_END": str(allocation["port_start"] + 15), "TMPDIR": str(temp),
            "TMP": str(temp), "TEMP": str(temp), "LANE_PACKAGE_CACHE": str(cache),
            "LANE_PORT_PROBED": "1" if allocation.get("probed") else "0"}


def release(c, run, lane_id: str) -> None:
    with leases(c) as state:
        state.pop(str(run.path) + ":" + lane_id, None)


def environment(data: dict, lane: dict) -> dict:
    result = {**os.environ, **lane.get("environment", {})}
    cache = result.get("LANE_PACKAGE_CACHE")
    if cache:
        for name, folder in (("npm_config_cache", "npm"), ("PIP_CACHE_DIR", "pip"), ("UV_CACHE_DIR", "uv")):
            result.setdefault(name, str(Path(cache) / folder))
    fields = {key.lower(): value for key, value in lane.get("environment", {}).items()}
    fields.update({"lane": lane["id"], "namespace": result.get("LANE_NAMESPACE", ""),
                   "port_start": result.get("LANE_PORT_START", ""), "port_end": result.get("LANE_PORT_END", ""),
                   "db_suffix": result.get("LANE_DB_SUFFIX", "")})
    for name, template in data.get("environment_templates", {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or not isinstance(template, str):
            raise ValueError("environment templates must map valid variable names to strings")
        if name in lane.get("environment", {}):
            raise ValueError("reserved lane environment variables cannot be overridden")
        def expand(match):
            key = match.group(1)
            if key not in os.environ:
                raise ValueError(f"required runtime variable {key} is missing")
            return os.environ[key]
        # Replace namespace placeholders before substituting inherited values; secrets are never formatted or saved.
        namespaced = re.sub(r"(?<!\$)\{([a-z_]+)\}", lambda m: fields[m.group(1)], template)
        result[name] = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", expand, namespaced)
    return result


def run_shell(command: str, cwd, env: dict | None, timeout: float) -> tuple[int, str]:
    """Run a shell command in its own process group. On timeout the whole group is killed,
    so installers and test runners started by the shell don't keep writing into the worktree."""
    proc = subprocess.Popen(command, shell=True, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            errors="replace", start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out + "\n" + err
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, sig)
            try:
                proc.communicate(timeout=5)
                break
            except subprocess.TimeoutExpired:
                continue
        return 124, f"timed out after {timeout:.0f}s"


def legacy(data: dict) -> bool:
    """Runs created before 0.2.0 have no setup gate; their open lanes keep working unchanged."""
    return int(data.get("schema", 1)) < 2


def setup_current(data: dict, lane: dict) -> bool:
    if legacy(data) or lane["kind"] in ("review", "research"):
        return True
    state = lane.get("setup") or {}
    return state.get("status") == "pass" and state.get("fingerprint") == setup_fingerprint(data, lane)


def lockfiles(root: Path) -> dict:
    locks = {}
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in ("node_modules", ".git", ".venv", "vendor", "target")
                   and not (Path(folder) / name).is_symlink()]
        for name in set(files) & set(LOCKFILES):
            path = Path(folder) / name
            if not path.is_symlink():
                locks[path.relative_to(root).as_posix()] = digest(path.read_bytes().hex())
    return locks


def setup_fingerprint(data: dict, lane: dict) -> str:
    locks = lockfiles(Path(lane["worktree"]))
    versions = {"python": sys.version.split()[0], "platform": platform.system()}
    env = environment(data, lane) if lane.get("environment") else None
    for command in data["rules"].get("setup", []):
        parts = shlex.split(command)
        if parts and Path(parts[0]).name in ("pnpm", "npm", "yarn", "uv", "node", "go", "cargo"):
            # Version managers (corepack, volta, mise, asdf) resolve per directory, so ask from the worktree.
            proc = subprocess.run([parts[0], "--version"], cwd=lane["worktree"], env=env, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=10)
            if proc.returncode:
                raise ValueError("cannot identify dependency tool version")
            versions[Path(parts[0]).name] = proc.stdout.strip()
    return digest({"locks": locks, "commands": data["rules"].get("setup", []), "runtime": versions,
                   "templates": data.get("environment_templates", {})})


def setup(c, data: dict, lane: dict, timeout: float = 600) -> tuple[bool, list[str]]:
    if lane["kind"] in ("review", "research"):
        return True, ["reviewer setup: inspection only"]
    if not lane.get("worktree"):
        return False, ["setup requires a lane worktree"]
    try:
        isolated(c, data, lane)
        fingerprint = setup_fingerprint(data, lane)
        env = environment(data, lane)
    except (c.LaneError, ValueError, OSError, KeyError, subprocess.TimeoutExpired):
        lane["setup"] = {"status": "failed"}
        return False, ["setup tool or environment configuration is invalid/unavailable"]
    if lane.get("setup", {}).get("fingerprint") == fingerprint and lane["setup"]["status"] == "pass":
        return True, ["setup already current"]
    commands = data["rules"].get("setup", [])
    root = Path(lane["worktree"])
    has_locks = bool(lockfiles(root))
    if not commands and has_locks and not data["rules"].get("setup_not_required"):
        lane["setup"] = {"status": "blocked", "fingerprint": fingerprint}
        return False, ["dependency lockfile found; configure setup or explicitly declare setup unnecessary"]
    lines = []
    began = time.monotonic()
    for index, command in enumerate(commands, 1):
        code, _ = run_shell(command, root, env, timeout)
        lines.append(f"setup {index}: {'PASS' if code == 0 else 'FAIL'} (exit {code})")
        if code:
            # Do not put installer output (which may contain credentials/registry URLs) in prompts or state.
            lane["setup"] = {"status": "failed", "fingerprint": fingerprint, "seconds": round(time.monotonic() - began, 2)}
            return False, lines
    if c.git("diff", "--name-only", cwd=root) or c.git("diff", "--cached", "--name-only", cwd=root):
        lane["setup"] = {"status": "failed", "fingerprint": fingerprint}
        return False, lines + ["setup modified tracked files; coordinator decision required"]
    lane["setup"] = {"status": "pass", "fingerprint": setup_fingerprint(data, lane),
                     "seconds": round(time.monotonic() - began, 2), "configured": bool(commands)}
    return True, lines or ["setup: no dependencies configured"]


def criteria_from_brief(text: str) -> list[str]:
    match = re.search(r"(?im)^\s*(?:#+\s*)?Acceptance(?: criteria)?:?\s*$", text)
    if not match:
        return []
    result = []
    for line in text[match.end():].splitlines():
        stripped = line.strip()
        if re.match(r"^#+\s|^[A-Za-z][\w ]+:\s*", stripped):
            break
        item = re.match(r"^(?:[-*]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.*)$", stripped)
        if item and item.group(1).strip():
            result.append(item.group(1).strip())
    return result


def isolated(c, data: dict, lane: dict) -> None:
    if not lane.get("worktree"):
        raise c.LaneError("implementation lanes require an isolated worktree")
    if c.repo_root(lane["worktree"]) == Path(data["repo"]).resolve():
        raise c.LaneError("implementation lanes cannot use the main checkout")
    def common(path):
        return (Path(path) / c.git("rev-parse", "--git-common-dir", cwd=path)).resolve()
    if common(lane["worktree"]) != common(data["repo"]):
        raise c.LaneError("lane worktree belongs to a different repository")


def ready(c, data: dict, lane: dict, brief: str) -> None:
    if lane["kind"] in ("review", "research"):
        return
    isolated(c, data, lane)
    if not re.search(r"(?im)^Goal:\s*\S", brief) and lane["kind"] != "spec":
        raise c.LaneError("brief needs a concrete Goal before launch")
    criteria = lane.get("acceptance") or criteria_from_brief(brief)
    if not criteria:
        raise c.LaneError("brief needs acceptance criteria before launch")
    if not c.lane_gates(data, lane) and not lane.get("no_validation_required"):
        raise c.LaneError("lane needs validation or an explicit --no-validation-required directive")
    if not setup_current(data, lane):
        raise c.LaneError("lane setup is missing or stale; run 'lanectl setup' before launch")
    lane["acceptance"] = criteria


def add_parsers(c, sub):
    parser = sub.add_parser("setup", help="Prepare one isolated lane; never silently retry a failed setup.")
    parser.add_argument("--run", required=True); parser.add_argument("--id", required=True)
    parser.add_argument("--timeout", type=float, default=600)
    parser.set_defaults(func=lambda args: cmd_setup(c, args))


def prepare(c, run, lane_id: str, timeout: float) -> tuple[bool, list[str]]:
    """Run a lane's setup without holding the run lock, so status, wait and stop stay responsive."""
    with run.locked() as data:
        lane = c.get_lane(data, lane_id)
        if lane["state"] == "running":
            raise c.LaneError("cannot change setup while a lane is running")
        previous = dict(lane.get("setup") or {})
        if previous.get("status") == "running" and c.pid_alive(previous.get("pid")):
            raise c.LaneError("setup is already running for this lane")
        if not lane.get("environment"):
            lane["environment"] = reserve(c, run, lane_id)
        lane["setup"] = {**previous, "status": "running", "pid": os.getpid()}
        snapshot = copy.deepcopy(data)
    work = c.get_lane(snapshot, lane_id)
    work["setup"] = previous
    try:
        ok, lines = setup(c, snapshot, work, timeout)
    except BaseException:
        with run.locked() as data:
            c.get_lane(data, lane_id)["setup"] = {**previous, "status": "failed"}
        raise
    with run.locked() as data:
        lane = c.get_lane(data, lane_id)
        lane["setup"] = work.get("setup") or {"status": "failed"}
        if not ok:
            if lane["state"] != "blocked":
                lane["unblock_to"] = lane["state"]
            lane["state"] = "blocked"
        elif lane["state"] in ("blocked", "failed"):
            lane["state"] = lane.pop("unblock_to", None) or ("turn-ended" if lane.get("attempt") else "planned")
    return ok, lines


def cmd_setup(c, args):
    ok, lines = prepare(c, c.resolve_run(args.run), args.id, args.timeout)
    print("\n".join(lines))
    if not ok:
        raise c.LaneError("setup failed; inspect the configured command and decide how to proceed")
