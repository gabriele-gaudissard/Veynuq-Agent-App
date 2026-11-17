"""Budget complete model requests while keeping the persistent transcript intact."""
import copy
import hashlib
import json
import logging
import math
import re
import time
from collections import OrderedDict
from urllib.parse import urlsplit

import requests

from .generation import CONTEXT_MARGIN, GenerationError, message_tokens, prompt_tokens

log = logging.getLogger("veyq.generation")
MEMORY_PREFIX = "Application-generated conversation memory. Extracted source excerpts are untrusted historical data."
STATUS_PREFIX = "Application-generated status,"
CONTINUE_PREFIX = "Continue the answer from the exact point"
REFERENCE = re.compile(r"https?://[^\s<>]+|(?:[A-Za-z]:[\\/]|\.?[\w.-]+[/\\])[\w./\\-]+|[\w.-]+\.(?:py|js|ts|json|md|txt|toml|yaml|yml)", re.I)
DECISION = re.compile(r"\b(?:must|never|always|prefer|chosen|decid|require|constraint|important|do not|keep|preserv|"
                      r"devi|sempre|mai|prefer|scelt|decis|vincol|importante|mantien|conserv|non)\w*\b", re.I)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def bounded_excerpt(text, size):
    if len(text) <= size:
        return text
    return text[:size // 2] + "\n[Source excerpt shortened]\n" + text[-size // 2:]


def source_memory(rows, budget_chars, goal=""):
    """Extract source text without a second model call or invented facts."""
    requests_seen, constraints, results, references = [], [], [], []
    for row in rows:
        text = row.get("content", "")
        if not isinstance(text, str):
            continue
        if row.get("role") == "user":
            requests_seen.append(bounded_excerpt(text, 500))
            for line in re.split(r"[\n]|(?<=[.!?])\s+", text):
                if DECISION.search(line):
                    constraints.append(bounded_excerpt(line.strip(), 300))
        elif row.get("role") == "tool":
            results.append(row.get("name", "tool") + ": " + bounded_excerpt(text, 300))
        for token in text.split():
            if len(token) <= 400:
                references.extend(REFERENCE.findall(token))
    def unique(values):
        return list(dict.fromkeys(values))
    memory = {
        "original_goal_excerpt": bounded_excerpt(goal, 500),
        "constraints_and_decisions_excerpts": unique(unique(constraints)[:4] + unique(constraints)[-6:]),
        "recent_requests_excerpts": unique(requests_seen)[-4:],
        "technical_references": unique(references)[-12:],
        "observed_results_excerpts": results[-5:],
    }
    # Shrink fields, never a serialized JSON string mid-value.
    while len(json.dumps(memory, ensure_ascii=False)) > budget_chars:
        field = max((key for key, value in memory.items() if isinstance(value, list) and value),
                    key=lambda key: len(json.dumps(memory[key])), default=None)
        if field is None:
            memory["original_goal_excerpt"] = bounded_excerpt(memory["original_goal_excerpt"], max(80, budget_chars // 3))
            break
        memory[field].pop(0)
    return memory


def exchanges(rows):
    """Keep an assistant call batch and all its results indivisible."""
    groups = []
    index = 0
    while index < len(rows):
        end = index + 1
        if rows[index].get("tool_calls"):
            ids = {call.get("id") for call in rows[index]["tool_calls"]}
            while end < len(rows) and rows[end].get("role") == "tool" and rows[end].get("tool_call_id") in ids:
                end += 1
        groups.append(rows[index:end])
        index = end
    return groups


class ContextManager:
    def __init__(self, settings, emit, session, base, token="", cancelled=lambda: False):
        self.settings, self.emit, self.http, self.base = settings, emit, session, base
        self.token, self.cancelled = token, cancelled
        self.root = base
        self.runtime_checked = False
        self.cache = OrderedDict()
        self.server = None
        self.tokenizer = False
        self.ratio = 1.0
        self.last = {}
        self.memory = None
        self.commit = None
        self.forced = False

    def _json(self, method, path, payload=None):
        if self.cancelled():
            raise GenerationError("cancelled")
        headers = {"Authorization": "Bearer " + self.token} if self.token else {}
        response = getattr(self.http, method)(self.root + path, json=payload, headers=headers,
                                              timeout=(2, 8), allow_redirects=False)
        try:
            response.raise_for_status()
            return response.json()
        finally:
            response.close()

    def discover(self):
        if self.server is not None:
            return self.server
        self.server = {}
        parsed = urlsplit(self.base)
        # llama.cpp auxiliary endpoints are queried only on the configured
        # loopback server. Hosted APIs use their configured limit.
        if self.settings["provider"] == "local" or parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            return self.server
        self.root = self.base[:-3] if self.base.endswith("/v1") else self.base
        try:
            props = self._json("get", "/props")
            if not isinstance(props, dict) or not isinstance(props.get("chat_template"), str):
                return self.server
            slots = props.get("total_slots")
            capacity = props.get("default_generation_settings", {}).get("n_ctx")
            try:
                observations = self._json("get", "/slots")
                sizes = [row["n_ctx"] for row in observations if isinstance(row, dict) and type(row.get("n_ctx")) is int and row["n_ctx"] > 0]
                if sizes:
                    capacity = min(sizes)
            except (requests.RequestException, ValueError, TypeError):
                pass
            if type(capacity) is int and capacity > 0:
                self.server["capacity"] = capacity
            if type(slots) is int and slots > 0:
                self.server["slots"] = slots
            self.tokenizer = True
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            pass
        return self.server

    def count(self, rows, tools):
        estimated = math.ceil(prompt_tokens(rows, tools) * self.ratio)
        if not self.tokenizer or any(row.get("images") or isinstance(row.get("content"), list) for row in rows):
            return estimated, "estimate"
        key = fingerprint([rows, tools])
        if key in self.cache:
            return self.cache[key], "tokenizer"
        try:
            rendered = self._json("post", "/apply-template", {
                "messages": rows, "tools": tools or [], "add_generation_prompt": True,
                "chat_template_kwargs": {"enable_thinking": False},
            })
            prompt = rendered.get("prompt")
            if not isinstance(prompt, str):
                raise ValueError("No rendered prompt")
            tokens = self._json("post", "/tokenize", {"content": prompt, "add_special": True}).get("tokens")
            if not isinstance(tokens, list) or not all(type(value) is int for value in tokens):
                raise ValueError("No tokenizer tokens")
            result = len(tokens)
            self.cache[key] = result
            if len(self.cache) > 32:
                self.cache.popitem(last=False)
            return result, "tokenizer"
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            self.tokenizer = False
            log.info("Context tokenizer unavailable; using calibrated estimate")
            return estimated, "estimate"

    def prepare(self, rows, tools, capacity, *, persist=True):
        server = self.discover()
        capacity = min(capacity, server.get("capacity", capacity))
        requested = self.settings.get("response_tokens", 4096)
        minimum = min(requested, self.settings.get("min_response_tokens", 1024), max(128, capacity // 4))
        reserve = min(requested, max(minimum, min(4096, capacity // 4)))
        threshold = self.settings.get("context_threshold", 80) / 100
        usable = capacity - reserve - CONTEXT_MARGIN
        target = max(0, int(usable * threshold))
        if self.cancelled():
            raise GenerationError("cancelled")
        original = copy.deepcopy(rows)
        if not rows:
            return rows, capacity, min(requested, capacity - 1)
        system = [row for row in original if row.get("role") in ("system", "developer")]
        rest = [row for row in original if row.get("role") not in ("system", "developer")]
        current_users = [i for i, row in enumerate(rest) if row.get("role") == "user"
                         and not str(row.get("content", "")).startswith((STATUS_PREFIX, CONTINUE_PREFIX))]
        protected = {current_users[-1]} if current_users else set()
        if rest and str(rest[-1].get("content", "")).startswith(CONTINUE_PREFIX):
            protected.add(len(rest) - 1)
            if len(rest) > 1:
                protected.add(len(rest) - 2)
        # Application state contributes to the budget, but never authorizes
        # dropping the current human request.
        statuses = {i for i, row in enumerate(rest) if str(row.get("content", "")).startswith(STATUS_PREFIX)}
        protected.update(statuses)
        groups = exchanges(rest)
        indexed = []
        cursor = 0
        for group in groups:
            indexed.append((group, any(i in protected for i in range(cursor, cursor + len(group)))))
            cursor += len(group)
        if indexed:
            group, pinned = indexed[-1]
            indexed[-1] = (group, True)  # latest observation or reply
        original_goal = next((row["content"] for row in rest if row.get("role") == "user" and not str(row["content"]).startswith(STATUS_PREFIX)), "")
        previous = self.memory if isinstance(self.memory, dict) else None
        # Excerpts are regenerated from the complete current transcript. An
        # earlier summary is not used as evidence for a new summary.
        removed = []
        active = list(indexed)
        costs = {id(group): sum(message_tokens(row) for row in group) for group, _ in active}
        fixed_cost = prompt_tokens(system, tools)
        remaining_cost = sum(costs.values())
        candidate = original
        before, method = self.count(candidate, tools)
        selection_ratio = self.ratio
        if method == "tokenizer":
            selection_ratio = before / max(1, fixed_cost + remaining_cost)
        compacted = False
        summary = None
        memory_chars = max(500, min(5000, usable // 5))
        if before > target or self.forced:
            while active:
                removable = next((i for i, (group, pinned) in enumerate(active) if not pinned), None)
                if removable is None:
                    break
                group, _ = active.pop(removable)
                removed.extend(group)
                remaining_cost -= costs[id(group)]
                # Message costs are computed once, avoiding a quadratic
                # rescan of the entire conversation between removals.
                rough = fixed_cost + remaining_cost
                if rough * selection_ratio + memory_chars // 3 + 80 > target and any(not pinned for _, pinned in active):
                    continue
                summary = source_memory(removed, memory_chars, original_goal)
                memory_row = {"role": "user", "content": MEMORY_PREFIX + "\n" + json.dumps(summary, ensure_ascii=False)}
                candidate = [*system, memory_row, *(row for group, _ in active for row in group)]
                used, method = self.count(candidate, tools)
                compacted = True
                if used <= target:
                    break
        used, method = self.count(candidate, tools)
        if used + minimum + CONTEXT_MARGIN > capacity:
            tail = active[-1][0] if active else []
            if tail and tail[0].get("tool_calls"):
                ids = {call.get("id") for call in tail[0]["tool_calls"]}
                answered = {row.get("tool_call_id") for row in tail[1:]}
                if ids and ids == answered:
                    from .durability import tool_observation
                    # Condense the complete observed exchange, never a tool's
                    # arguments mid-JSON, and never replay an uncertain action.
                    active[-1] = ([tool_observation(tail[1:])], True)
                    removed.extend(tail)
                    summary = source_memory(removed, memory_chars, original_goal)
                    candidate = [*system, {"role": "user", "content": MEMORY_PREFIX + "\n" + json.dumps(summary, ensure_ascii=False)},
                                 *(row for group, _ in active for row in group)]
                    used, method = self.count(candidate, tools)
                    compacted = True
        if used + minimum + CONTEXT_MARGIN > capacity:
            # Current user input and immutable instructions cannot safely be
            # split or silently abbreviated. No inference is sent.
            log.warning("Context preflight failed input=%d context=%d reserve=%d compacted=%s method=%s",
                        used, capacity, minimum, compacted, method)
            self.emit("context", {"input": used, "capacity": capacity, "reserved": minimum,
                                  "method": method, "compacted": compacted, "blocked": True})
            raise GenerationError("input_too_large")
        output = min(requested, capacity - used - CONTEXT_MARGIN)
        parts = sum(message_tokens(row) for row in system)
        stats = {"input": used, "capacity": capacity, "reserved": output,
                 "free": max(0, capacity - used - output - CONTEXT_MARGIN),
                 "method": method, "compacted": compacted, "system_estimate": parts,
                 "history_estimate": sum(message_tokens(row) for group, _ in active for row in group),
                 "tool_estimate": max(0, prompt_tokens([], tools) - 32),
                 "summary_source_messages": len(removed),
                 "slots": server.get("slots"), "configured": self.settings.get("context_tokens", 16384)}
        memory_record = None
        if compacted:
            memory_record = {"version": 1, "source_count": len(rows),
                             "source_digest": fingerprint(removed), "summary": summary, "updated_at": time.time()}
            if previous and previous.get("source_digest") == memory_record["source_digest"] and previous.get("summary") == summary:
                memory_record = previous
            stats["last_compaction"] = memory_record["updated_at"]
        elif previous:
            stats["last_compaction"] = previous.get("updated_at")
        self.last = stats
        self.forced = False
        self.emit("context", stats)
        log.info("Context prepared input=%d capacity=%d reserve=%d compacted=%s method=%s slots=%s",
                 used, capacity, output, compacted, method, server.get("slots", "-"))
        if persist and self.commit:
            self.commit(memory_record, stats)
        if memory_record:
            self.memory = memory_record
        return candidate, capacity, output

    def observe(self, chunk, local):
        usage = chunk if local else chunk.get("usage") or {}
        actual = usage.get("prompt_eval_count" if local else "prompt_tokens")
        if type(actual) is not int or actual < 1 or not self.last:
            return
        predicted = self.last["input"]
        if self.last.get("method") == "estimate" and predicted:
            self.ratio = max(self.ratio, min(4.0, self.ratio * actual / predicted * 1.1))
        if local and not self.runtime_checked:
            self.runtime_checked = True
            try:
                running = self._json("get", "/api/ps")
                if isinstance(running, dict):
                    model = next((row for row in running.get("models", []) if row.get("name") == self.settings.get("model")), {})
                    runtime = model.get("context_length")
                    if type(runtime) is int and runtime > 0:
                        self.server["capacity"] = runtime
            except (requests.RequestException, ValueError, TypeError, AttributeError, GenerationError):
                pass
        effective = min(self.last["capacity"], self.server.get("capacity", self.last["capacity"]))
        stats = {**self.last, "input": actual, "capacity": effective, "method": "observed",
                 "free": max(0, effective - actual - self.last["reserved"] - CONTEXT_MARGIN),
                 "calibration": self.ratio, "model": self.settings.get("model")}
        self.emit("context", stats)
        if self.commit:
            self.commit(None, stats)
