"""Deterministic ticket briefs and comparison of completed runs."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
from pathlib import Path

from lane_cache import digest
from lane_runtime import criteria_from_brief


def cmd_brief(c, args):
    run = c.resolve_run(args.run)
    data = run.read(); lane = c.get_lane(data, args.id)
    if lane.get("attempt"):
        raise c.LaneError("generate a brief before the lane starts")
    if args.ticket_file:
        ticket = json.loads(Path(args.ticket_file).read_text())
    else:
        command = data.get("profile", {}).get("ticket_command")
        if not command:
            raise c.LaneError("supply --ticket-file or a configured JSON argv ticket_command")
        argv = json.loads(command)
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv):
            raise c.LaneError("ticket_command must be a JSON argument array, not a shell command")
        if not args.ticket_id:
            raise c.LaneError("--ticket-id is required for a ticket command")
        proc = subprocess.run([part.replace("{id}", args.ticket_id) for part in argv], cwd=data["repo"],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode:
            raise c.LaneError("ticket adapter failed; no raw tracker output recorded")
        ticket = json.loads(proc.stdout)
    if not isinstance(ticket, dict) or not isinstance(ticket.get("title"), str) or not ticket["title"].strip():
        raise c.LaneError("ticket needs a title")
    acceptance = ticket.get("acceptance") or criteria_from_brief(ticket.get("body", ""))
    validation = ticket.get("validation") or c.lane_validation(data, lane)
    if not isinstance(acceptance, list) or not acceptance or not all(isinstance(v, str) and v.strip() for v in acceptance):
        raise c.LaneError("ticket has no explicit acceptance criteria; ask for them rather than inventing them")
    if not isinstance(validation, list) or not validation or not all(isinstance(v, str) and v.strip() for v in validation):
        raise c.LaneError("ticket has no validation; configure it before generating a brief")
    dependencies = ticket.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(isinstance(v, str) for v in dependencies):
        raise c.LaneError("ticket dependencies must be item IDs")
    mapped = []; unresolved = []
    for item in dependencies:
        matches = [other["id"] for other in data["lanes"].values() if item in other["items"]]
        if len(matches) != 1:
            unresolved.append(item)
        else:
            mapped += matches
    if unresolved:
        raise c.LaneError("unresolved ticket dependencies: " + ", ".join(unresolved))
    terms = args.terms or ",".join(re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", ticket["title"])[:6])
    if not terms:
        raise c.LaneError("supply --terms for the repository map")
    import contextlib, io
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        c.cmd_map(argparse.Namespace(run=str(run.path), repo=".", terms=terms, paths=args.paths,
                                    defs=2, limit=8, json=True))
    mapped_files = json.loads(output.getvalue())["files"]
    start = [row["path"] for row in mapped_files[:4]]
    text = "Goal: " + ticket["title"].strip() + "\nAcceptance:\n" + "\n".join("- " + v.strip() for v in acceptance)
    text += "\nTicket: " + str(ticket.get("id", args.ticket_id or "provided ticket"))
    text += "\nValidation: " + "; ".join(validation)
    text += "\nStart files (suggested; coordinator must verify ownership): " + (", ".join(start) or "no map matches")
    text += "\nDependencies: " + (", ".join(dependencies) or "none declared")
    text += "\nConstraints: Follow assigned ownership and protected resources.\n"
    folder = run.lane_dir(args.id)
    path = folder / "brief-draft.md"; c.atomic_write(path, text)
    with run.locked() as current:
        lane = c.get_lane(current, args.id)
        if lane.get("attempt"):
            raise c.LaneError("lane started while drafting; draft not adopted")
        lane.update({"acceptance": acceptance, "validate": validation, "start": start,
                     "blocked_by": sorted(set(lane["blocked_by"] + mapped)),
                     "brief_draft": {"path": str(path), "sha256": digest(text), "approved": False},
                     "ticket_source_sha256": digest(ticket)})
    print(f"{path}\nDraft needs coordinator approval and ownership verification; {len(start)} suggested start files.")


def cmd_approve(c, args):
    run = c.resolve_run(args.run)
    with run.locked() as data:
        lane = c.get_lane(data, args.id)
        draft = lane.get("brief_draft")
        if not draft or lane.get("attempt"):
            raise c.LaneError("no unstarted draft to approve")
        if not lane["owns"]:
            raise c.LaneError("assign ownership before approving a generated brief")
        text = Path(draft["path"]).read_text()
        criteria = criteria_from_brief(text)
        if not criteria or not re.search(r"(?im)^Goal:\s*\S", text):
            raise c.LaneError("approved brief must retain a goal and acceptance criteria")
        lane["acceptance"] = criteria
        draft.update({"approved": True, "sha256": digest(text)})
    print("brief approved")


def summary(c, data):
    rows = {lane_id: c.lane_usage(lane) for lane_id, lane in data["lanes"].items()}
    start = dt.datetime.fromisoformat(data["created"])
    end = dt.datetime.fromisoformat(data.get("closed_at", c.now()))
    return {"run": data["id"], "state": data["state"], "base_sha": data.get("initial_base_sha"),
            "items": sorted({item for lane in data["lanes"].values() if lane["kind"] != "review" for item in lane["items"]}),
            "merged": [lane_id for lane_id, lane in data["lanes"].items() if lane["state"] == "merged"],
            "unfinished": [lane_id for lane_id, lane in data["lanes"].items() if lane["state"] not in ("merged", "closed")],
            "fresh": sum(row["fresh"] for row in rows.values()), "cache_read": sum(row["cache_read"] for row in rows.values()),
            "input": sum(row["in"] for row in rows.values()),
            "output": sum(row["out"] for row in rows.values()),
            "reported_cost_usd": sum(row["cost_usd"] or 0 for row in rows.values()),
            "cost_complete": all(row["cost_known"] for row in rows.values()),
            "wall_seconds": max(0, (end - start).total_seconds()),
            "agent_seconds": sum(row["secs"] for row in rows.values()),
            "setup_seconds": sum(lane.get("setup", {}).get("seconds", 0) for lane in data["lanes"].values()),
            "correction_rounds": sum(lane.get("correction_rounds", 0) for lane in data["lanes"].values()),
            "lane_usage": rows, "savings": "unknown without a comparable completed run"}


def cmd_summary(c, args):
    run = c.resolve_run(args.run)
    with run.locked() as data:
        for lane in data["lanes"].values():
            c.refresh(run, data, lane)
        result = summary(c, data)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        cost = c.fmt_cost(result["reported_cost_usd"]) if result["cost_complete"] else "unknown (partial reported cost available)"
        print(f"merged={len(result['merged'])} unfinished={len(result['unfinished'])} "
              f"fresh={result['fresh']} cached={result['cache_read']} out={result['output']} "
              f"cost={cost} elapsed={c.fmt_secs(result['wall_seconds'])} agent-time={c.fmt_secs(result['agent_seconds'])}")


def cmd_compare(c, args):
    parallel = c.resolve_run(args.lanes_run).read(); serial = c.resolve_run(args.serial_run).read()
    left = summary(c, parallel); right = summary(c, serial)
    if parallel["repo"] != serial["repo"] or not left["base_sha"] or left["base_sha"] != right["base_sha"] or left["items"] != right["items"]:
        raise c.LaneError("comparison requires the same repository, starting commit and item set")
    if left["state"] != "closed" or right["state"] != "closed" or left["unfinished"] or right["unfinished"]:
        raise c.LaneError("compare completed, closed runs only")
    implementations = [lane for lane in serial["lanes"].values() if lane["kind"] != "review"]
    if len(implementations) != 1:
        raise c.LaneError("serial baseline must use one implementation session")
    evidence = json.loads(Path(args.evidence_file).read_text())
    if (evidence.get("same_acceptance") is not True or evidence.get("same_fixtures") is not True
            or evidence.get("both_passed") is not True):
        raise c.LaneError("supply explicit same-acceptance/fixtures and passing-result evidence")
    a = left["input"] + left["output"]
    b = right["input"] + right["output"]
    result = {"lanes": left, "serial": right, "evidence": evidence,
              "reported_token_reduction_percent": round(100 * (b - a) / b, 2) if b else None,
              "elapsed_reduction_percent": round(100 * (right["wall_seconds"] - left["wall_seconds"]) / right["wall_seconds"], 2)
                                            if right["wall_seconds"] else None,
              "limits": ["run counters exclude coordinator calls made outside lanectl; supply those separately",
                         "token reductions are not dollar reductions; input includes reported cache writes",
                         "cache temperature/order and task outcomes must be controlled across repeated trials"]}
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(args.output)


def add_parsers(c, sub):
    parser = sub.add_parser("brief", help="Generate a source-grounded draft from a normalized ticket.")
    parser.add_argument("--run", required=True); parser.add_argument("--id", required=True)
    sources = parser.add_mutually_exclusive_group(); sources.add_argument("--ticket-file")
    parser.add_argument("--ticket-id"); parser.add_argument("--terms"); parser.add_argument("--paths")
    parser.set_defaults(func=lambda args: cmd_brief(c, args))
    approve = sub.add_parser("approve-brief", help="Record coordinator review of a generated brief.")
    approve.add_argument("--run", required=True); approve.add_argument("--id", required=True)
    approve.set_defaults(func=lambda args: cmd_approve(c, args))
    s = sub.add_parser("summary", help="Actual run totals; never invent a serial savings estimate.")
    s.add_argument("--run", required=True); s.add_argument("--json", action="store_true")
    s.set_defaults(func=lambda args: cmd_summary(c, args))
    compare = sub.add_parser("compare", help="Compare paired completed runs; does not launch agents or mutate applications.")
    compare.add_argument("--lanes-run", required=True); compare.add_argument("--serial-run", required=True)
    compare.add_argument("--evidence-file", required=True); compare.add_argument("--output", required=True)
    compare.set_defaults(func=lambda args: cmd_compare(c, args))
