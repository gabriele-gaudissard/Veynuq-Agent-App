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


class TaskState:
    """Evidence and explicit execution phases, separate from model prose."""
    PHASES = {"ANALYZING", "ACTION_REQUIRED", "WAITING_FOR_TOOL_CALL", "EXECUTING",
              "VERIFYING", "COMPLETED", "PARTIAL", "FAILED"}

    def __init__(self, store, session, secrets=()):
        self.store, self.session, self.secrets = store, session, secrets
        self.iteration = 0
        self.phase = ""
        self.seen_text = set()
        self.text_repetitions = 0
        self.unproductive_text = 0
        self.last_error = ""
        self.transitions = []
        self.receipts = {}
        self.prior_verified = []
        for message in session.get("history", [])[-100:]:
            if message.get("role") != "tool":
                continue
            try:
                result = json.loads(message["content"])
                value = result.get("result", {})
                if result.get("verification",{}).get("state")=="completed" and isinstance(value,dict) and value.get("path"):
                    self.prior_verified.append({"tool":message.get("name"),"path":value["path"],"sha256":value.get("sha256")})
            except (ValueError, TypeError, KeyError):
                continue
        self.transition("ANALYZING")

    def transition(self, phase):
        if phase not in self.PHASES:
            raise ValueError("Unknown execution phase.")
        with self.store.lock:
            if self.phase != phase:
                self.transitions.append({"phase":phase,"iteration":self.iteration})
                self.transitions = self.transitions[-48:]
            self.phase = phase
            import logging
            logging.getLogger("veyq.engine").info("Execution state run=%s phase=%s iteration=%d",
                self.session.get("task",{}).get("run_id","-"),phase,self.iteration)
            self.session.setdefault("task", {})["execution"] = {
                "phase":phase,"iterations":self.iteration,"text_repetitions":self.text_repetitions,
                "unproductive_text":self.unproductive_text,"last_error":self.last_error,
                "pending_targets":self.pending_targets(), "verified_files":list(self.receipts.values()),
                "transitions":list(self.transitions),
            }

    @staticmethod
    def promises_action(text):
        import re
        fence = re.escape(chr(96)*3)
        text = re.sub(fence+r".*?"+fence, "", text, flags=re.S).lower()
        if re.search(r"\b(?:cannot|can't|unable|non posso|non riesco|impossibile)\b", text):
            return False
        action = r"(?:cre(?:o|erò|are)|scriv(?:o|erò|ere)|modific(?:o|herò|are)|corregg(?:o|erò|ere)|salv(?:o|erò|are)|esegu(?:o|irò|ire)|create|write|edit|modify|fix|save|run|implement|apply)"
        return bool(re.search(r"\b(?:ora|adesso|procedo|provvedo|vado a|now|next|then|i will|i'll|i am going to)\b.{0,100}\b"+action+r"\b", text)
                    or re.search(r"\b(?:creerò|scriverò|modificherò|correggerò|eseguirò)\b", text))

    def text(self, text, pending):
        import re
        normalized = " ".join(re.findall(r"[^\W\d_]+", text.lower())[:512])
        words = frozenset(normalized.split())
        intent = "promised_action" if self.promises_action(text) else "analysis" if re.search(
            r"\b(?:anali\w*|analys\w*|analiz\w*|individuat\w*|identif\w*|problem\w*)\b", normalized) else normalized
        self.text_kind = intent if intent in {"analysis", "promised_action"} else "prose"
        marker = hashlib.sha256(str(intent).encode()).digest()
        similar = any(len(words & previous)/max(1,len(words | previous)) >= .7 for previous in getattr(self,"text_words", []))
        if marker in self.seen_text or similar:
            self.text_repetitions += 1
        self.seen_text.add(marker)
        self.text_words = [*getattr(self,"text_words", [])[-3:], words]
        if pending:
            self.unproductive_text += 1
            self.transition("ACTION_REQUIRED")
        return self.promises_action(text)

    def result(self, result, entry=None):
        from .storage import redact
        if entry and result.get("ok") and entry["tool"] in {"delete_file","move_file"}:
            self.receipts.pop(entry.get("target"), None)
        value = result.get("result")
        if result.get("verification",{}).get("state") in {"completed","unverified"} and isinstance(value,dict) and value.get("sha256"):
            self.receipts[value["path"]] = {key:value[key] for key in ("path","bytes","sha256")}
        if not result.get("ok"):
            self.last_error = redact(str(result.get("error", "Tool failed."))[:600], self.secrets)
        elif not any(r["state"]=="failed" and not r.get("recovered") for r in self.session.get("task",{}).get("operations",[])):
            self.last_error = ""
            if result.get("verification", {}).get("state") == "completed":
                self.unproductive_text = 0
        self.transition("VERIFYING")

    def requirements(self, request, runner):
        """Track unambiguous file targets explicitly requested by the user."""
        import re
        if request == getattr(self, "requirements_request", None):
            return
        self.requirements_request = request
        targets = set()
        verbs = re.compile(r"(?i)\b(?:crea\w*|scrivi\w*|salva\w*|modifica\w*|correggi\w*|create|write|save|edit|modify|update|fix)\b")
        files = re.compile(r"(?:[a-zA-Z]:[\\/]|(?:[.\w-]+[\\/])*)[\w.-]+\.(?:txt|py|js|ts|json|md|html|css|cpp|c|h|ini|yaml|yml|csv)\b")
        for match in verbs.finditer(request):
            rest = re.split(r"[;\n]|\.(?=\s|$)|\b(?:using|with|from|read|leggi|usando|usa|leggendo)\b",
                            request[match.end():], maxsplit=1, flags=re.I)[0]
            found = list(files.finditer(rest))
            if not found or found[0].start() > 80:
                continue
            quoted = []
            for quote in (chr(34), chr(39)):
                quoted.extend((q.start(),q.end()) for q in re.finditer(re.escape(quote)+r"[^"+re.escape(quote)+r"\n]+?"+re.escape(quote),rest)
                              if any(c.isspace() for c in q[0]))
            for target in found:
                if any(a <= target.start() < b for a,b in quoted):
                    continue
                try:
                    targets.add(str(runner.path(target[0])))
                except (ValueError, OSError, PermissionError):
                    continue
        self.targets = targets

    def pending_targets(self):
        from .outcomes import MUTATIONS
        rows = self.session.get("task", {}).get("operations", [])
        changed = any(row["state"]=="completed" and row["tool"] in MUTATIONS for row in rows)
        observed = {row.get("target") for row in rows if row["state"]=="completed"
                    and (row["tool"] in {"write_file","edit_file","download_file","move_file"} or changed and row["tool"]=="read_file")}
        return sorted(getattr(self,"targets",set()) - observed)

    def summary(self):
        task = self.session.get("task", {})
        rows = task.get("operations", [])
        return {
            "goal":task.get("goal","")[:600],"phase":self.phase,
            "pending_targets":self.pending_targets()[:8],
            "previous_verified_observations_recheck_before_reuse":self.prior_verified[-5:],
            "pending_steps":[r.get("step","")[:180] for r in self.session.get("plan",[]) if r.get("status")!="completed"][:8],
            "confirmed":[{"tool":r["tool"],"target":r.get("target","")[-180:]} for r in rows if r["state"]=="completed"][-5:],
            "failed":[{"tool":r["tool"],"target":r.get("target","")[-180:]} for r in rows if r["state"]=="failed" and not r.get("recovered")][-3:],
            "last_error":self.last_error,"unproductive_text":self.unproductive_text,
        }

    def finish(self, state, has_work=False):
        self.transition("COMPLETED" if state=="completed" else
                        "PARTIAL" if state in {"partial","unverified","incomplete"} or has_work else "FAILED")
