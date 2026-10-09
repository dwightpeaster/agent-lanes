"""Disposable integration trees, optional baseline regressions, and compact GitHub CI."""
from __future__ import annotations

import copy
import json
import subprocess
import time
from pathlib import Path

from lane_cache import digest
import lane_review
import lane_runtime


def base_ref(c, data):
    remote = "refs/remotes/origin/" + data["base"]
    return remote if c.git("rev-parse", "--verify", "--quiet", remote, cwd=data["repo"], check=False) else data["base"]


def ordering(data):
    return digest({key: {"blocked_by": lane["blocked_by"], "reserves": lane["reserves"], "queued_at": lane.get("queued_at")}
                   for key, lane in data["lanes"].items() if lane["state"] not in ("merged", "closed")})


def current(c, data: dict) -> bool:
    proof = data.get("integration")
    if not proof or proof["status"] != "pass":
        return False
    if c.git("rev-parse", base_ref(c, data), cwd=data["repo"]) != proof["base_sha"]:
        return False
    if digest(c.lane_validation(data, {"validate": []})) != proof["validation_sha256"] or digest(data["rules"].get("setup", [])) != proof["setup_sha256"]:
        return False
    if digest(data.get("environment_templates", {})) != proof["environment_sha256"]:
        return False
    if ordering(data) != proof["ordering_sha256"]:
        return False
    queued = {lane_id for lane_id, lane in data["lanes"].items() if lane["state"] == "merge-queued"}
    if queued != set(proof["lane_heads"]):
        return False
    for lane_id, head in proof["lane_heads"].items():
        lane = data["lanes"].get(lane_id)
        if not lane or lane["state"] in ("merged", "closed"):
            return False
        try:
            if lane_review.clean_head(c, lane) != head:
                return False
        except c.LaneError:
            # A dirty or missing worktree means the proof no longer describes it; the queue says so.
            return False
    return True


def remove_checkout(c, data: dict, checkout: Path, keep: bool) -> None:
    """Disposable checkouts are force-removed (generated files are expected); failed ones are kept."""
    if keep:
        print(f"checkout retained for inspection: {checkout} (remove with: git worktree remove --force {checkout})")
        return
    subprocess.run(["git", "worktree", "remove", "--force", str(checkout)], cwd=data["repo"], capture_output=True)


# Disposable merges must not depend on the user's identity or run repository hooks.
MERGE = ["git", "-c", "user.name=agent-lanes", "-c", "user.email=agent-lanes@localhost",
         "merge", "--no-verify", "--no-edit", "--no-ff"]


def cmd_integration(c, args):
    run = c.resolve_run(args.run)
    data = run.read()
    selected = c.csv(args.ids) or [lane["id"] for lane in data["lanes"].values() if lane["state"] == "merge-queued"]
    if not selected or len(set(selected)) != len(selected):
        raise c.LaneError("integration needs distinct lane IDs (default: queued lanes)")
    if not data["rules"]["validate"]:
        raise c.LaneError("integration requires the full run validation gate")
    if c.scope_problems(data):
        raise c.LaneError("scope must be clean before integration")
    simulated = copy.deepcopy(data); ordered = []; pending = selected[:]
    while pending:
        progressed = False
        for lane_id in pending[:]:
            lane = c.get_lane(simulated, lane_id)
            if lane["kind"] in ("review", "research") or lane["state"] == "running":
                raise c.LaneError("integration accepts idle implementation lanes only")
            if c.reserve_hold(simulated, lane) or any(c.get_lane(simulated, dep)["state"] != "merged" for dep in lane["blocked_by"]):
                continue
            ordered.append(lane_id); lane["state"] = "merged"; pending.remove(lane_id); progressed = True
        if not progressed:
            raise c.LaneError("integration selection has unresolved dependency/resource order")
    heads = {lane_id: lane_review.clean_head(c, c.get_lane(data, lane_id)) for lane_id in ordered}
    base = c.git("rev-parse", base_ref(c, data), cwd=data["repo"])
    folder = run.path / "integration" / str(time.time_ns()); folder.mkdir(parents=True)
    checkout = folder / "checkout"
    c.git("worktree", "add", "--detach", str(checkout), base, cwd=data["repo"])
    synthetic = {"id": "integration-" + folder.name, "kind": "implement", "worktree": str(checkout), "validate": []}
    passed = False
    try:
        synthetic["environment"] = lane_runtime.reserve(c, run, synthetic["id"])
        for lane_id in ordered:
            proc = subprocess.run([*MERGE, heads[lane_id]], cwd=checkout, capture_output=True, text=True, timeout=300)
            if proc.returncode:
                c.git("merge", "--abort", cwd=checkout, check=False)
                raise c.LaneError(f"integration conflict for {lane_id}; source worktrees were not changed")
        ok, setup_lines = lane_runtime.setup(c, data, synthetic, args.timeout)
        if not ok:
            raise c.LaneError("integration setup failed: " + "; ".join(setup_lines))
        ok, lines = c.run_checks(data, synthetic, args.timeout, args.tail)
        result = {"status": "pass" if ok else "failed", "base_sha": base, "lane_heads": heads, "order": ordered,
                  "head_sha": c.git("rev-parse", "HEAD", cwd=checkout), "tree_sha": c.git("rev-parse", "HEAD^{tree}", cwd=checkout),
                  "validation_sha256": digest(c.lane_validation(data, {"validate": []})), "setup_sha256": digest(data["rules"].get("setup", [])),
                  "environment_sha256": digest(data.get("environment_templates", {})),
                  "ordering_sha256": ordering(data),
                  "checks": lines, "at": c.now()}
        # Bind the proof only if neither source commits nor gate commands moved during execution.
        with run.locked() as latest:
            if (c.git("rev-parse", base_ref(c, latest), cwd=latest["repo"]) != base
                    or any(lane_review.clean_head(c, c.get_lane(latest, lane_id)) != head for lane_id, head in heads.items())
                    or digest(c.lane_validation(latest, {"validate": []})) != result["validation_sha256"]
                    or digest(latest["rules"].get("setup", [])) != result["setup_sha256"]
                    or digest(latest.get("environment_templates", {})) != result["environment_sha256"]
                    or ordering(latest) != result["ordering_sha256"]):
                raise c.LaneError("integration inputs changed during checks; proof not adopted")
            latest["integration"] = result
        c.atomic_write(folder / "result.json", json.dumps(result, indent=2) + "\n")
        print("\n".join(lines))
        if not ok:
            raise c.LaneError("combined validation failed; merge queue remains gated")
        print(f"integration PASS for {','.join(ordered)} at {result['head_sha']}")
        passed = True
    finally:
        remove_checkout(c, data, checkout, keep=not passed)
        lane_runtime.release(c, run, synthetic["id"])


def baseline(c, run, data, lane, command, timeout, tail):
    head = lane_review.clean_head(c, lane); base = lane_review.base_sha(c, lane, head)
    code, _ = lane_runtime.run_shell(command, lane["worktree"], lane_runtime.environment(data, lane), timeout)
    if code:
        raise c.LaneError("baseline command must first pass on the candidate commit")
    changed = lane_review.paths(c, lane["worktree"], base, head)
    tests = [path for path in changed if re_test(path)]
    if not tests:
        raise c.LaneError("baseline regression check needs changed test files")
    folder = run.path / "baselines" / str(time.time_ns()); folder.mkdir(parents=True)
    checkout = folder / "checkout"
    c.git("worktree", "add", "--detach", str(checkout), base, cwd=data["repo"])
    synthetic = {**lane, "id": "baseline-" + folder.name, "worktree": str(checkout)}
    synthetic.pop("setup", None)
    try:
        synthetic["environment"] = lane_runtime.reserve(c, run, synthetic["id"])
        ok, setup_lines = lane_runtime.setup(c, data, synthetic, timeout)
        if not ok:
            raise c.LaneError("baseline dependency setup is inconclusive: " + "; ".join(setup_lines))
        patch = subprocess.run(["git", "diff", "--binary", base, head, "--", *tests], cwd=lane["worktree"],
                               capture_output=True, check=True).stdout
        applied = subprocess.run(["git", "apply", "--binary"], input=patch, cwd=checkout, capture_output=True)
        if applied.returncode:
            raise c.LaneError("changed tests cannot be applied to base; baseline evidence is inconclusive")
        # Install base dependencies before the test-only overlay; setup must not modify tracked base files.
        code, diagnostics = lane_runtime.run_shell(command, checkout, lane_runtime.environment(data, synthetic), timeout)
        inconclusive = code == 124 or any(word in diagnostics.lower() for word in
                                          ("modulenotfounderror", "importerror", "command not found", "no tests ran", "syntaxerror"))
        outcome = "base-fails" if code and not inconclusive else "inconclusive" if inconclusive else "base-passes"
        result = {"head_sha": head, "base_sha": base, "test_files": tests, "outcome": outcome,
                  "exit_code": code, "note": "base failure is evidence, not proof of complete coverage"}
        c.atomic_write(folder / "result.json", json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        return outcome == "base-fails"
    finally:
        # Discard only the test overlay created above, then remove the disposable checkout.
        tracked = [p for p in tests if c.git("ls-tree", base, "--", p, cwd=checkout, check=False)]
        if tracked:
            c.git("restore", "--source=HEAD", "--staged", "--worktree", "--", *tracked, cwd=checkout, check=False)
        for path in tests:
            if not c.git("ls-tree", base, "--", path, cwd=checkout, check=False):
                candidate = checkout / path
                if candidate.is_file() and not candidate.is_symlink():
                    candidate.unlink()
        remove_checkout(c, data, checkout, keep=False)
        lane_runtime.release(c, run, synthetic["id"])


def re_test(path):
    import re
    return bool(re.search(r"(^|/)(tests?|specs?)(/|[_.])|[_.](test|spec)[_.]", path))


def cmd_ci(c, args):
    run = c.resolve_run(args.run); data = run.read(); lane = c.get_lane(data, args.id)
    head = lane_review.clean_head(c, lane)
    if not lane.get("pr"):
        raise c.LaneError("lane has no PR URL")
    result = subprocess.run(["gh", "pr", "view", lane["pr"], "--json", "headRefOid,statusCheckRollup"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise c.LaneError("CI unavailable; authentication or GitHub request failed")
    info = json.loads(result.stdout)
    if info["headRefOid"] != head:
        raise c.LaneError("PR commit differs from the local lane commit")
    checks = info.get("statusCheckRollup") or []
    state = "unavailable"
    if checks:
        failed = any(check.get("conclusion") in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED") or check.get("state") in ("FAILURE", "ERROR") for check in checks)
        pending = any(check.get("status") not in (None, "COMPLETED") or check.get("state") == "PENDING"
                      or not (check.get("conclusion") in ("SUCCESS", "NEUTRAL", "SKIPPED") or check.get("state") == "SUCCESS")
                      for check in checks)
        state = "failed" if failed else "pending" if pending else "green"
    print(f"CI {state}: {len(checks)} checks at {head}")
    if state != "green":
        raise c.LaneError("CI is not green")


def add_parsers(c, sub):
    parser = sub.add_parser("integration", help="Test the ordered combination without changing source lanes.")
    parser.add_argument("--run", required=True); parser.add_argument("--ids")
    parser.add_argument("--timeout", type=float, default=1800); parser.add_argument("--tail", type=int, default=30)
    parser.set_defaults(func=lambda args: cmd_integration(c, args))
    ci = sub.add_parser("ci", help="Compact CI disposition bound to the lane's commit.")
    ci.add_argument("--run", required=True); ci.add_argument("--id", required=True)
    ci.set_defaults(func=lambda args: cmd_ci(c, args))
