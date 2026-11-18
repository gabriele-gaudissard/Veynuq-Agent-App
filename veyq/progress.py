"""Detect repeated identical observations without treating long work as a loop."""
import hashlib
import json

OBSERVATIONS = {"read_file", "list_dir", "search_files", "read_document", "view_image",
                "inspect_binary", "code_intelligence", "git_status", "read_project_context",
                "browser_state", "plugin_catalog", "plugin_tools", "tool_setup",
                "update_plan", "checkpoint_task"}


class ProgressGuard:
    def __init__(self):
        self.reset()

    def reset(self):
        self.seen = set()
        self.redundant = 0
        self.warnings = 0
        self.stopped = False
        self.guidance = ""

    def observe(self, name, args, result):
        if self.stopped:
            return False
        # File changes, commands and other successful actions open a new
        # observation phase. Distinct ranges/results count as real progress.
        value = result.get("result")
        if (result.get("ok") and name in {"write_file", "edit_file"}
                and isinstance(value, dict) and value.get("published") is True and value.get("sha256")):
            # Rewriting the same known file state, even with a different backup
            # ID or call arguments, does not advance the activity.
            identity = ["file_state", value.get("path") or args.get("path"), value["sha256"]]
        elif result.get("ok") and name not in OBSERVATIONS:
            self.seen.clear()
            self.redundant = 0
            self.guidance = ""
            return False
        else:
            identity = [name, args, result]
        signature = hashlib.sha256(json.dumps(identity, sort_keys=True,
                                               ensure_ascii=False).encode()).digest()
        if signature not in self.seen:
            self.seen.add(signature)
            self.redundant = 0
            return False
        self.redundant += 1
        if self.redundant < 4:
            return False
        self.redundant = 0
        self.warnings += 1
        self.stopped = self.warnings >= 2
        self.guidance = ("The last observations repeated unchanged results without progress. "
                         "Do not reread the same inputs again. Use the recorded results for the next "
                         "necessary action or a final evidence-based answer. If essential information "
                         "is missing, ask one specific question. Never invent actions or bypass permissions.")
        return True
