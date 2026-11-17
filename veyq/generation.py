"""Budget prompts and continue prose without dispatching actions."""
import json
import logging
import re

log = logging.getLogger(__name__)

MIN_OUTPUT = 128
CONTEXT_MARGIN = 256
MAX_OUTPUT = 16384
MAX_CONTINUATIONS = 8
COMPATIBLE_LIMIT_FIELDS = {"server", "max_tokens", "max_completion_tokens"}


class GenerationError(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def context_capacity(settings, metadata=None):
    limits = [settings.get("context_tokens", 16384)]
    info = (metadata or {}).get("model_info") or {}
    if isinstance(info, dict):
        limits.extend(value for key, value in info.items()
                      if key.endswith(".context_length") and type(value) is int and value > 0)
    return min(limits)


def estimate_tokens(value, bytes_per_token=3):
    # Providers do not share a tokenizer or a chat template. Use UTF-8 bytes
    # as a practical estimate and keep a reserve; report real usage separately.
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return (len(encoded) + bytes_per_token - 1) // bytes_per_token


def message_tokens(message):
    content = message.get("content", "")
    images = len(message.get("images") or ())
    if isinstance(content, list):
        images += sum(isinstance(part, dict) and part.get("type") == "image_url" for part in content)
        content = [part for part in content if not isinstance(part, dict) or part.get("type") != "image_url"]
    return 8 + 1024 * images + estimate_tokens({
        **{key: value for key, value in message.items() if key not in {"images", "incomplete"}},
        "content": content,
    })


def prompt_tokens(messages, tools):
    return 32 + sum(map(message_tokens, messages)) + (estimate_tokens(tools, bytes_per_token=4) if tools else 0)


def output_budget(settings, capacity, estimated_prompt):
    requested = settings.get("response_tokens", 4096)
    available = capacity - estimated_prompt - CONTEXT_MARGIN
    if available < min(requested, MIN_OUTPUT):
        raise GenerationError("context")
    return min(requested, available)


def continuation_limit(settings):
    limit = settings.get("text_continuations", 3)
    return limit if type(limit) is int and 0 <= limit <= MAX_CONTINUATIONS else 3


def prose_request(text):
    return bool(text.strip()) and not re.search(
        r"\b(jsonl?|ndjson|yaml|toml|csv|tsv|xml|schemas?|structured output|formato strutturato|"
        r"source code|codice sorgente|markdown table|tabella markdown|execute|esegu\w*|powershell|"
        r"shell script|tool calls?|function calls?)\b", text, re.I)


def prose_output(text):
    if not text.strip() or re.match(r"\s*(?:[\[{<]|[\x60~]{3}|(?:function|class|import|def)\s)", text):
        return False
    return not re.search(r"<\s*/?\s*(?:tool_call|function|invoke|tool_use)\b|\[TOOL_CALLS\]", text, re.I) and not re.search(
        r"(?m)^\s*(?:\x60{3,}|~{3,})", text)


def continuation_prompt(messages, answer):
    return [*messages,
            {"role": "assistant", "content": answer[-8000:]},
            {"role": "user", "content": (
                "Continue the answer from the exact point where it was cut off. "
                "The preceding assistant message is the tail of the answer already shown. "
                "Preserve its language and formatting. If needed, begin with the exact last sentence fragment before continuing. "
                "Finish the original request without restating earlier passages or invoking tools."
            )}]


def new_text(answer, continuation):
    if not continuation.strip() or continuation.strip() in answer.strip():
        return ""
    if continuation.startswith(answer):
        return continuation[len(answer):]
    # The model saw the tail only. Remove exact overlaps from that tail, and
    # keep the new characters after it, including intentional whitespace.
    tail = answer[-8000:]
    candidate = continuation.lstrip()
    for position in range(len(tail)):
        if tail[position] != candidate[0]:
            continue
        overlap = len(tail) - position
        if overlap >= 12 and candidate.startswith(tail[position:]):
            return candidate[overlap:]
    if answer[-1:].isspace() or continuation[:1].isspace() or continuation.startswith((".", ",", "!", "?", ":", ";", ")", "]")):
        return continuation
    return " " + continuation


def backend_failure(detail):
    if re.search(r"out of memory|not enough (?:system )?memory|insufficient memory|"
                 r"failed to allocate|unable to allocate|cuda error.*memory|memory required.*available", detail, re.I):
        return "memory"
    if re.search(r"context_length_exceeded|context (?:length|window|size)|"
                 r"(?:prompt|input).{0,30}(?:too long|too large|exceeds.*tokens)", detail, re.I):
        return "context"
    if re.search(r"timed?\s*out|read timeout|deadline exceeded", detail, re.I):
        return "timeout"
    return None


def log_usage(chunk, local, estimated, capacity, limit):
    usage = chunk if local else chunk.get("usage") or {}
    if not isinstance(usage, dict):
        return
    prompt = usage.get("prompt_eval_count" if local else "prompt_tokens")
    generated = usage.get("eval_count" if local else "completion_tokens")
    duration = usage.get("eval_duration")
    speed = round(generated * 1e9 / duration, 1) if type(generated) is int and type(duration) is int and duration > 0 else None
    if type(prompt) is int or type(generated) is int:
        log.info("Model usage input_estimate=%d input=%s output=%s context=%d output_budget=%s tokens_per_second=%s",
                 estimated, prompt if type(prompt) is int else "-", generated if type(generated) is int else "-",
                 capacity, limit, speed)
