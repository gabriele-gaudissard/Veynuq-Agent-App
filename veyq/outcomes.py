"""Metadata-only outcomes for one agent run."""
import hashlib
import json
import logging

from .tools import WRITE

MUTATIONS = WRITE | {"exec_cmd", "clone_repository", "restore_backup", "git_worktree", "sandbox_changes",
                     "proposal_changes", "computer_action", "computer_pointer", "computer_sandbox_type", "windows_sandbox", "github"}

log = logging.getLogger("veyq.engine")


class Outcomes:
    def __init__(self, runner):
        self.runner = runner
        self.rows = []
        self.last_phase = "request"

    def phase(self, phase, **details):
        self.last_phase = phase
        log.info("Agent phase=%s run=%s %s", phase, self.runner.run_id,
                    " ".join(f"{key}={hashlib.sha256(str(value).encode()).hexdigest()[:12] if key == 'call' else value}"
                             for key, value in details.items()))

    def observe(self, call, args, result, pending):
        name = call.get("function", {}).get("name", "")
        raw_target = args.get("path") or args.get("destination") or args.get("repository") or ""
        if raw_target and ("path" in args or "destination" in args):
            try:
                raw_target = str(self.runner.path(raw_target))
            except (OSError, ValueError, PermissionError):
                raw_target = ""
        identity_name = name
        if name in {"read_file", "read_document", "inspect_binary", "view_image"}:
            identity_name = "file_inspection"
        elif name in {"write_file", "edit_file"}:
            identity_name = "file_change"
        identity = [identity_name, raw_target or args]
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        value = result.get("result")
        unpublished = isinstance(value, dict) and value.get("published") is False
        unknown = result.get("outcome_unknown", False)
        pending_target = any(row.get("target") == raw_target or row.get("kind") in {"project", "github", "desktop", "window"}
                             for row in pending)
        if unknown:
            state = "unverified"
        elif not result.get("ok"):
            state = "failed"
        elif unpublished:
            state = "staged"
        elif pending_target or name in MUTATIONS and not raw_target and pending:
            state = "unverified"
        else:
            state = "completed"
        entry = {"call_id": call["id"], "tool": name, "target": raw_target, "state": state,
                 "key": key, "outcome_unknown": unknown, "evidence": result.get("verification", {}).get("evidence")}
        if result.get("ok"):
            for previous in self.rows:
                if previous["key"] == key and previous["state"] in {"failed", "unverified"}:
                    previous["recovered"] = True
        self.rows.append(entry)
        self.refresh(pending)
        self.phase("tool_result", tool=name, outcome=state, call=call["id"])
        return entry

    def refresh(self, pending):
        targets = {entry.get("target") for entry in pending}
        for row in self.rows:
            if row["state"] == "unverified" and not row.get("outcome_unknown") and row["target"] and row["target"] not in targets:
                # A failed command with unknown side effects requires a
                # separate, relevant observation before its status can change.
                if row["tool"] in {"write_file", "edit_file", "download_file", "restore_backup",
                                    "make_dir", "move_file", "delete_file"}:
                    row["state"] = "completed"
            if row["state"] == "unverified" and not row.get("outcome_unknown") and not pending and row["tool"] in MUTATIONS:
                row["state"] = "completed"
        self.runner.session.setdefault("task", {})["operations"] = self.rows

    def failure_state(self):
        failed = [row for row in self.rows if row["state"] == "failed" and not row.get("recovered")]
        if any(row["state"] == "unverified" and row.get("outcome_unknown") and not row.get("recovered") for row in self.rows):
            return "unverified"
        if not failed:
            return None
        successful = any(row["state"] == "completed" for row in self.rows)
        return "partial" if successful else "failed"

    def diagnostics(self, category):
        return {"category": category,
                "completed": sum(row["state"] == "completed" for row in self.rows),
                "failed": sum(row["state"] == "failed" and not row.get("recovered") for row in self.rows),
                "unverified": sum(row["state"] in {"unverified", "staged"} and not row.get("recovered") for row in self.rows)}
