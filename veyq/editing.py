"""Precise text edits, destination conflicts and activity-local recovery."""
import copy
import hashlib
import json
import os
from .file_write import file_version, validate_write


class EditError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(code + ": " + message)
        self.details = {"code": code, **details}


def edited_text(before, old, new, sha256, start_line=None, end_line=None, expected_sha256=None):
    if expected_sha256 is not None and (
            len(expected_sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in expected_sha256)):
        raise EditError("INVALID_EDIT_RANGE", "Fornisci un SHA-256 valido.")
    if expected_sha256 is not None and expected_sha256.lower() != sha256:
        raise EditError("FILE_CHANGED", "Il file è cambiato: verifica il contenuto attuale prima di modificare.",
                        file_sha256=sha256)
    offset, stop = 0, len(before)
    if start_line is not None or end_line is not None:
        lines = before.splitlines(keepends=True)
        if (type(start_line) is not int or type(end_line) is not int or start_line < 1
                or end_line < start_line or end_line > len(lines) or expected_sha256 is None):
            raise EditError("INVALID_EDIT_RANGE", "Usa start_line/end_line validi e expected_sha256 dalla lettura o diagnostica corrente.",
                            file_sha256=sha256)
        offset = sum(map(len, lines[:start_line - 1]))
        stop = offset + sum(map(len, lines[start_line - 1:end_line]))
    scope = before[offset:stop]
    # Preserve existing LF→CRLF compatibility; never normalize unrelated text.
    if old and scope.count(old) != 1 and "\r\n" in scope and "\r" not in old + new:
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    count, position, first, candidates = 0, 0, -1, []
    if old:
        while True:
            found = scope.find(old, position)
            if found < 0:
                break
            if first < 0:
                first = found
            count += 1
            if len(candidates) < 12:
                begin = offset + found
                last = begin + len(old) - 1
                candidates.append({"start_line": before.count("\n", 0, begin) + 1,
                                   "end_line": before.count("\n", 0, last) + 1})
            position = found + 1  # Overlapping matches are ambiguous too.
    if count != 1:
        code = "TEXT_NOT_FOUND" if not count else "AMBIGUOUS_MATCH"
        message = ("Il testo old non compare nell'intervallo selezionato; usa il testo letterale già letto."
                   if not count else "Il testo old compare più volte; aggiungi contesto univoco o un intervallo verificato.")
        raise EditError(code, message, match_count=count, matches=candidates,
                        candidates_truncated=count > len(candidates), file_sha256=sha256)
    begin = offset + first
    return before[:begin] + new + before[begin + len(old):]


class RecoveryGuard:
    """Cache authorized reads, bound changed edit attempts, reject empty checkpoints."""
    MATCH_ERRORS = {"TEXT_NOT_FOUND", "AMBIGUOUS_MATCH"}

    def __init__(self, runner):
        self.runner = runner
        self.reads = {}
        self.failures = {}
        self.write_failures = {}
        self.writes = {}
        self.read_stamps = {}
        self.epoch = 0
        self.last_checkpoint = None
        self.checkpoint_stalls = 0
        self.evidence = set()
        self.guidance = ""
        self.stopped = ""
        self.stalls = 0

    def reset(self):
        self.failures.clear()
        self.write_failures.clear()
        self.writes.clear()
        self.guidance = self.stopped = ""
        self.stalls = self.checkpoint_stalls = 0
        self.epoch += 1  # New human instruction can justify a new checkpoint.

    @staticmethod
    def stamp(path):
        try:
            info = path.stat()
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
        except OSError:
            return None

    @staticmethod
    def version(path):
        try:
            return file_version(path)
        except (OSError, ValueError):
            return None

    @staticmethod
    def signature(args):
        return tuple(args.get(key) for key in ("old", "new", "start_line", "end_line"))

    def write_signature(self, args):
        content = args.get("content", "").encode("utf-8")
        newline = args.get("newline")
        if newline and not content.endswith((b"\r",b"\n")):
            content += b"\r\n" if newline=="crlf" else b"\n"
        part = args.get("part",1 if args.get("final") is False else None)
        return (hashlib.sha256(content).hexdigest(), bool(args.get("overwrite")),
                part, bool(args.get("final")) if part is not None else True)

    def before(self, name, args):
        from .tools import SPECS, validate
        validate(args, SPECS[name], unbounded_text="content" if name == "write_file" else None)
        if name == "write_file":
            validate_write(args)
        if name == "checkpoint_task" and self.last_checkpoint == self.epoch:
            self.checkpoint_stalls += 1
            self.guidance = "A checkpoint is already saved for these observations. Changing progress wording is not task progress. Perform the next necessary authorized action or report the actual result."
            if self.checkpoint_stalls >= 2:
                self.stopped = "checkpoint_no_progress"
            return {"ok": False, "code": "CHECKPOINT_NO_PROGRESS", "not_executed": True,
                    "error": "Checkpoint già salvato: nessun nuovo risultato concreto. Prosegui il lavoro senza ripetere checkpoint_task."}
        if name == "write_file" and name not in self.runner.settings.get("disabled_tools", []):
            path = self.runner.path(args["path"])
            key, signature = os.path.normcase(str(path)), self.write_signature(args)
            previous = self.writes.get((key,signature))
            if previous:
                value = previous["result"]
                version = self.version(self.runner.path(value["path"]))
                if version and version[-1].hex()==value["sha256"]:
                    return {**copy.deepcopy(previous),"reused_result":True}
            case = self.write_failures.get(key)
            if case:
                version = self.version(path)
                if not version or version[-1].hex()!=case["hash"]:
                    self.write_failures.pop(key,None)
                elif signature in case["failed"]:
                    self.stalls += 1
                    if self.stalls >= 3:
                        self.stopped = "write_conflict_no_progress"
                    return {**copy.deepcopy(case["failed"][signature]),"not_executed":True,"repeated_write":True,
                            "error":"Scrittura identica già fallita: scegli una destinazione disponibile o una sostituzione autorizzata."}
                elif case["attempts"] >= 2:
                    self.stopped = "write_conflict_no_progress"
                    return {"ok":False,"code":"WRITE_RECOVERY_EXHAUSTED","not_executed":True,
                            "error":"Esauriti due tentativi di recupero del conflitto di destinazione."}
                else:
                    case["attempts"] += 1
            return None
        if name not in {"read_file", "edit_file"} or name in self.runner.settings.get("disabled_tools", []):
            return None
        path = self.runner.path(args["path"])
        key = os.path.normcase(str(path))
        if name == "read_file":
            start, end = args.get("start_line", 1), args.get("end_line", 300)
            if type(start) is not int or type(end) is not int or start < 1 or end < start or end - start > 1000:
                return None
            self.read_stamps[key] = self.stamp(path)
            if key not in self.reads:
                return None
            version = self.version(path)
            digest = version[-1].hex() if version else None
            for entry in reversed(self.reads[key]):
                if entry["hash"] != digest:
                    continue
                value = entry["result"]["result"]
                if entry["range"] == (start, end):
                    return {**copy.deepcopy(entry["result"]), "reused_result": True}
                actual_end = min(end, value["total_lines"])
                a, b = entry["range"]
                if len(value["content"]) < 30000 and a <= start <= actual_end <= min(b, value["total_lines"]):
                    result = copy.deepcopy(entry["result"])
                    rows = value["content"].split("\n")
                    result["result"]["content"] = "\n".join(rows[start-a:actual_end-a+1])
                    result["reused_result"] = True
                    return result
            return None
        case = self.failures.get(key)
        if not case:
            return None
        current = self.version(path)
        if not current or current[-1].hex() != case["hash"]:
            self.failures.pop(key, None)
            self.guidance = ""
            return None
        signature = self.signature(args)
        if signature in case["failed"]:
            self.stalls += 1
            if self.stalls >= 3:
                self.stopped = "edit_no_progress"
            return {**copy.deepcopy(case["failed"][signature]), "repeated_edit": True, "not_executed": True,
                    "error": "Tentativo edit_file identico già fallito sullo stesso contenuto. Usa contesto o righe più precise; azione non ripetuta."}
        if case["attempts"] >= 2:
            self.stopped = "edit_recovery_exhausted"
            return {"ok": False, "code": "EDIT_RECOVERY_EXHAUSTED", "not_executed": True,
                    "error": "Esauriti due tentativi di recupero con modifiche diverse."}
        case["attempts"] += 1
        return None

    def after(self, name, args, result):
        from .tools import SPECS, validate
        try:
            validate(args, SPECS[name], unbounded_text="content" if name == "write_file" else None)
            if name == "write_file":
                validate_write(args)
        except (ValueError, KeyError, TypeError):
            return
        if name in {"read_file","edit_file","write_file"}:
            try:
                self.runner.path(args["path"])
            except (ValueError, OSError, PermissionError):
                return
        if name == "checkpoint_task":
            if result.get("ok") and not result.get("reused_result"):
                self.last_checkpoint = self.epoch
            return
        if name == "read_file" and result.get("ok"):
            path = self.runner.path(args["path"])
            key = os.path.normcase(str(path))
            value = result.get("result")
            if not isinstance(value, dict) or not isinstance(value.get("content"), str):
                return
            version = self.version(path)
            if version and self.read_stamps.get(key) == self.stamp(path):
                digest = version[-1].hex()
                if not result.get("reused_result"):
                    entries = [e for e in self.reads.get(key, []) if e["hash"] == digest]
                    entries.append({"hash": digest, "range": (args.get("start_line",1), args.get("end_line",300)),
                                    "result": copy.deepcopy(result)})
                    self.reads[key] = entries
                if result.get("reused_result") and (key in self.failures or key in self.write_failures):
                    self.stalls += 1
                    if self.stalls >= 3:
                        self.stopped = "edit_no_progress" if key in self.failures else "write_conflict_no_progress"
            else:
                self.reads.pop(key, None)
        if name == "read_file" and not result.get("ok") and (os.path.normcase(str(self.runner.path(args["path"]))) in self.failures or os.path.normcase(str(self.runner.path(args["path"]))) in self.write_failures):
            self.stalls += 1
            if self.stalls >= 3:
                self.stopped = ("edit_no_progress" if os.path.normcase(str(self.runner.path(args["path"]))) in self.failures
                                else "write_conflict_no_progress")
        if name == "edit_file":
            key = os.path.normcase(str(self.runner.path(args["path"])))
            if result.get("ok"):
                self.failures.pop(key, None)
                self.guidance = ""
                self.stalls = 0
            elif result.get("code") in self.MATCH_ERRORS or key in self.failures:
                if result.get("repeated_edit"):
                    return
                case = self.failures.setdefault(key, {"hash": result.get("file_sha256"),
                                                       "failed": {}, "attempts": 0})
                signature = self.signature(args)
                if signature in case["failed"]:
                    self.stalls += 1
                    if self.stalls >= 3:
                        self.stopped = "edit_no_progress"
                    return
                case["failed"][signature] = copy.deepcopy(result)
                self.stalls = 0
                self.guidance = ("Continue the existing edit, not a new analysis. Last failure: " + str(result.get("code")) +
                    ". Reuse the already-read literal content if unchanged. Use longer unique surrounding text, or start_line/end_line with expected_sha256=" +
                    str(result.get("file_sha256", case["hash"])) + ". Do not repeat a failed patch. At most two changed recovery attempts are allowed.")
                if case["attempts"] >= 2:
                    self.stopped = "edit_recovery_exhausted"
        if name == "write_file":
            key, signature = os.path.normcase(str(self.runner.path(args["path"]))), self.write_signature(args)
            value = result.get("result")
            if result.get("ok") and isinstance(value,dict) and value.get("published") is True:
                self.writes[(key,signature)] = copy.deepcopy(result)
                self.write_failures.pop(key,None)
                self.stalls = 0
                self.guidance = ""
            elif result.get("code")=="DESTINATION_EXISTS" or key in self.write_failures:
                if result.get("repeated_write"):
                    return
                version = self.version(self.runner.path(args["path"]))
                if version:
                    case = self.write_failures.setdefault(key,{"hash":version[-1].hex(),"failed":{},"attempts":0})
                    if signature in case["failed"]:
                        self.stalls += 1
                        if self.stalls >= 3:
                            self.stopped = "write_conflict_no_progress"
                        return
                    case["failed"][signature] = copy.deepcopy(result)
                    self.stalls = 0
                    self.guidance = ("Continue the current write without repeating analysis or unchanged reads. The destination exists and remains intact. "
                        "final=true does not authorize overwrite. If the user authorized replacement, use overwrite=true; ambiguous replacement must use the normal confirmation. "
                        "For a requested new version use an available alternative, such as " + str(result.get("suggested_path","")) +
                        ". Do not repeat the failed call. Only two changed recovery attempts are allowed.")
                    if case["attempts"] >= 2:
                        self.stopped = "write_conflict_no_progress"
        # Only new tool evidence, not checkpoint wording or plan bookkeeping,
        # permits another useful checkpoint.
        if result.get("ok") and name != "update_plan" and not result.get("reused_result"):
            value = result.get("result")
            identity = [name, args, value]
            if name == "read_file" and isinstance(value,dict):
                identity = [name, value.get("path"), value.get("content"), value.get("total_lines")]
            signature = hashlib.sha256(json.dumps(identity,sort_keys=True,default=str).encode()).digest()
            if signature not in self.evidence:
                self.evidence.add(signature)
                self.epoch += 1
                self.checkpoint_stalls = 0


class FileConflict(FileExistsError):
    def __init__(self, target, digest=None, intent=None):
        super().__init__("Destination exists. Choose a new version, edit the existing file, or explicitly confirm overwrite=true.")
        self.details = {"code":"DESTINATION_EXISTS", "file_sha256":digest,
                        "suggested_path":str(available_version(target)),
                        "overwrite_authorized":intent in {"overwrite","programmatic"},
                        "overwrite_requires_confirmation":intent=="ambiguous"}


def available_version(target):
    import re
    match = re.fullmatch(r"(.*)_v(\d+)", target.stem)
    stem, number = (match[1], int(match[2])+1) if match else (target.stem, 2)
    while True:
        candidate = target.with_name(stem + "_v" + str(number) + target.suffix)
        if not candidate.exists():
            return candidate
        number += 1


def write_intent(session, target):
    """Only trusted human instructions can grant replacement of an existing file."""
    import re
    requests = [m.get("content","") for m in session.get("history",[]) if m.get("role")=="user"]
    if not requests and session.get("task",{}).get("goal"):
        requests = [session["task"]["goal"]]
    if not requests:
        return "programmatic"  # Legacy direct API: explicit overwrite=True is the instruction.
    for request in reversed(requests):
        text = request.casefold()
        if re.search(r"\b(?:nuova versione|nuova copia|new version|new copy|versione alternativa)\b",text):
            return "new_version"
        if re.search(r"\b(?:non sovrascriv\w*|non sostitu\w*|non rimpiazz\w*|do not (?:overwrite|replace)|don't (?:overwrite|replace)|never (?:overwrite|replace))\b",text):
            return "forbidden"
        if re.search(r"\b(?:explain|how to|spiega\w*|come si|come posso|come fare)\b",text):
            return "ambiguous"
        if target.name.casefold() in text:
            # Only a clear directive naming this target skips an extra conflict
            # question. Descriptions, quoted examples and uncertain phrasing
            # retain the normal approval flow even in full-access mode.
            names = "|".join(re.escape(value.casefold()) for value in (str(target), target.name))
            directive = (r"(?:^|[.!?;\n]|\b(?:and|e|then|poi)\s+)\s*"
                         r"(?:(?:please|per favore|puoi|can you|potresti)\s+)?"
                         r"(?:sovrascrivi|sostituisci|rimpiazza|overwrite|replace)\s+"
                         r"(?:(?:il file|the file)\s+)?[\x60\x22\x27]?(?:" + names +
                         r")(?=$|[\s\x60\x22\x27.,;!?])")
            if chr(96)*3 not in text and re.search(directive,text):
                return "overwrite"
            return "ambiguous"
        # A concrete new instruction cannot inherit unrelated old file consent.
        if not re.fullmatch(r"(?:continue|continua|prosegui|riprendi|go on)[.! ]*",text.strip()):
            break
    return "ambiguous"
