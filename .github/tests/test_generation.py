import json
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from veyq.desktop import DesktopAPI
from veyq.durability import compact
from veyq.engine import Cancelled, ModelClient
from veyq.generation import message_tokens, new_text, prose_request
from veyq.storage import DEFAULTS, Store


def native(text="", reason="stop", *, count=None):
    result = Mock(status_code=200)
    chunks = ({"message": {"content": text}},
              {"done": True, "done_reason": reason, "eval_count": count})
    result.iter_lines.return_value = (json.dumps(chunk).encode() for chunk in chunks)
    return result


def compatible(text, reason):
    result = Mock(status_code=200)
    result.iter_lines.return_value = iter([
        b"data: " + json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": None}]}).encode(),
        b"data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": reason}]}).encode(),
        b"data: [DONE]",
    ])
    return result


MESSAGES = [{"role": "user", "content": "Explain the history of port cities."}]


class GenerationTests(unittest.TestCase):
    def client(self, **settings):
        self.cancel = threading.Event()
        self.events = []
        client = ModelClient({**DEFAULTS, **settings}, "", self.cancel, lambda *event: self.events.append(event))
        client.metadata = {"capabilities": ["tools"], "model_info": {"family.context_length": 65536}}
        client.tools = []
        client.allow_text_continuation = True
        self.addCleanup(client.close)
        return client

    def test_short_answer_uses_configured_local_budget(self):
        client = self.client()
        with patch.object(client.session, "post", return_value=native("Rome.", count=2)) as post, \
                self.assertLogs("veyq.generation", level=logging.INFO) as metrics:
            message = client.chat(MESSAGES)
        self.assertEqual(message, {"role": "assistant", "content": "Rome."})
        self.assertIn("output=2 context=16384 output_budget=4096", metrics.output[0])
        self.assertEqual(post.call_args.kwargs["json"]["options"],
                         {"num_ctx": 16384, "num_predict": 4096, "temperature": .15})

    def test_2000_and_8000_token_streams_fit_despite_wire_overhead(self):
        frame = json.dumps({"message": {"content": " a"}, "model": "local/" + "m"*190,
                            "created_at": "2026-11-17T00:00:00Z", "done": False}).encode()
        for cap in (2000, 8000):
            with self.subTest(cap=cap):
                client = self.client(response_tokens=cap)
                result = Mock(status_code=200)
                def frames():
                    for _ in range(cap):
                        yield frame
                    yield json.dumps({"done": True, "done_reason": "stop", "eval_count": cap}).encode()
                result.iter_lines.return_value = frames()
                with patch.object(client.session, "post", return_value=result) as post:
                    self.assertEqual(len(client.chat(MESSAGES)["content"]), cap * 2)
                self.assertEqual(post.call_args.kwargs["json"]["options"]["num_predict"], cap)

    def test_context_reserves_room_and_refuses_oversized_input(self):
        client = self.client(context_tokens=24576, response_tokens=16384)
        client.metadata["model_info"]["family.context_length"] = 8192
        prompt = [{"role": "user", "content": "example " * 800}]
        with patch.object(client.session, "post", return_value=native("Done.")) as post:
            client.chat(prompt)
        options = post.call_args.kwargs["json"]["options"]
        self.assertEqual(options["num_ctx"], 8192)
        self.assertLess(options["num_predict"], 8192)
        with patch.object(client.session, "post") as post, self.assertLogs("veyq.engine"):
            with self.assertRaisesRegex(RuntimeError, "little room"):
                client.chat([{"role": "user", "content": "example " * 5000}])
            post.assert_not_called()

    def test_compatible_output_field_is_opt_in(self):
        for mode in ("server", "max_tokens", "max_completion_tokens"):
            client = self.client(provider="compatible", compatible_limit_field=mode, response_tokens=8000)
            with patch.object(client.session, "post", return_value=compatible("Done.", "stop")) as post:
                self.assertEqual(client.chat(MESSAGES)["content"], "Done.")
            payload = post.call_args.kwargs["json"]
            fields = set(payload) & {"max_tokens", "max_completion_tokens"}
            self.assertEqual(fields, set() if mode == "server" else {mode})
            if fields:
                self.assertEqual(payload[mode], 8000)
            self.assertNotIn("options", payload)

    def test_free_text_continues_without_advertising_tools_or_repeating(self):
        client = self.client(text_continuations=2, provider="compatible")
        start = "We began on the shore. In the next centuries"
        following = "In the next centuries trade expanded."
        with patch.object(client.session, "post", side_effect=[compatible(start, "length"),
                                                             compatible(following, "stop")]) as post:
            message = client.chat(MESSAGES)
        self.assertEqual(message, {"role": "assistant", "content": start + " trade expanded."})
        request = post.call_args.kwargs["json"]
        self.assertNotIn("tools", request)
        self.assertNotIn("tool_choice", request)
        self.assertEqual(request["messages"][1]["content"], start)
        self.assertEqual("".join(data["text"] for kind, data in self.events if kind == "text"), message["content"])
        self.assertEqual(new_text(start, start), "")

    def test_bounded_continuations_save_incomplete_response(self):
        client = self.client(text_continuations=1)
        with patch.object(client.session, "post", side_effect=[
            native("The side streets were busy.", "length"), native("A market opened.", "length")]) as post:
            message = client.chat(MESSAGES)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(message["incomplete"], "length")
        self.assertEqual(message["content"], "The side streets were busy. A market opened.")

    def test_repeated_or_structured_continuation_stops(self):
        for second, reason in (("Port cities flourished.", "no_progress"), ('{"result": 1}', "structured")):
            with self.subTest(reason=reason):
                client = self.client()
                with patch.object(client.session, "post", side_effect=[
                    native("Port cities flourished.", "length"), native(second)]) as post:
                    message = client.chat(MESSAGES)
                self.assertEqual(post.call_count, 2)
                self.assertEqual(message["content"], "Port cities flourished.")
                self.assertEqual(message["incomplete"], reason)

    def test_truncated_tool_calls_and_structured_responses_never_continue(self):
        self.assertFalse(prose_request("Produce JSON or CSV from this report."))
        client = self.client(tool_retries=0)
        data = [native('{"unfinished":', "length"), native(chr(96)*3 + "python\nx = 3", "length")]
        tool_reply = Mock(status_code=200)
        tool_reply.iter_lines.return_value = iter([
            b'{"message":{"content":"Starting now.","tool_calls":[{"function":{"name":"read_file","arguments":{"path":"x"}}}]}}',
            b'{"done":true,"done_reason":"length"}',
        ])
        for rejected in [*data, tool_reply]:
            with patch.object(client.session, "post", return_value=rejected) as post, self.assertLogs("veyq.engine"):
                with self.assertRaisesRegex(RuntimeError, "output limit"):
                    client.chat(MESSAGES)
                post.assert_called_once()
        self.assertFalse(any(kind == "notice" for kind, _ in self.events))

    def test_stop_while_receiving_continuation_preserves_only_safe_prefix(self):
        client = self.client()
        second = Mock(status_code=200)
        def stops():
            yield b'{"message":{"content":"This was not finished."}}'
            self.cancel.set()
            yield b'{"done":true,"done_reason":"stop"}'
        second.iter_lines.return_value = stops()
        with patch.object(client.session, "post", side_effect=[native("First paragraph.", "length"), second]) as post:
            with self.assertRaises(Cancelled) as error:
                client.chat(MESSAGES)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(error.exception.partial, "First paragraph.")
        second.close.assert_called_once()

    def test_continuation_timeout_preserves_partial_and_reports_reason(self):
        client = self.client()
        with patch.object(client.session, "post", side_effect=[
            native("First paragraph.", "length"), requests.Timeout("timed out")]), self.assertLogs("veyq.engine"):
            message = client.chat(MESSAGES)
        self.assertEqual(message["content"], "First paragraph.")
        self.assertEqual(message["incomplete"], "timeout")

    def test_backend_context_and_oom_are_distinct_and_not_retried(self):
        client = self.client()
        for detail, user_message in [("prompt exceeds context window", "little room"),
                                     ("CUDA error: out of memory", "out of memory")]:
            with self.subTest(detail=detail), patch.object(client.session, "post", return_value=Mock(
                    status_code=500, text=json.dumps({"error": detail}))) as post, self.assertLogs("veyq.engine"):
                with self.assertRaisesRegex(RuntimeError, user_message):
                    client.chat(MESSAGES)
                post.assert_called_once()

    def test_long_history_is_compacted_without_mutating_saved_messages(self):
        messages = [{"role": "user", "content": "Earlier requirements " + "a" * 3000},
                    {"role": "assistant", "content": "Initial answer."},
                    {"role": "user", "content": "Latest facts."},
                    {"role": "assistant", "content": "Start of answer." + " aspect " * 3000 + "End of answer."},
                    {"role": "user", "content": "Please continue from the latest facts."}]
        baseline = json.dumps(messages)
        session = {"history": messages}
        result = compact(session, 1500, measure=message_tokens)
        self.assertEqual(json.dumps(messages), baseline)
        self.assertEqual(result[-1], messages[-1])
        self.assertIn("Earlier requirements", session["context_summary"])
        self.assertIn("End of answer.", result[-2]["content"])
        self.assertLess(sum(map(message_tokens, result)), 1500)

    def test_numeric_identifiers_are_budgeted_as_separate_tokens(self):
        sample = " ".join(f"log-123-{i:03d}" for i in range(100))
        cost = message_tokens({"role": "user", "content": sample})
        self.assertGreaterEqual(cost, 900)

    def test_partial_answer_is_saved_and_can_be_continued_in_same_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "workspace"
            project.mkdir()
            store = Store(root / "profile")
            store.data["settings"]["text_continuations"] = 0
            api = DesktopAPI(store)
            sid = api.create_session()
            api.set_workspace(sid, str(project))
            with patch.object(ModelClient, "local_metadata", return_value={"capabilities": ["tools"]}), \
                    patch("requests.Session.post", side_effect=[native("First part.", "length"), native("Next part.")]) as post:
                api.start_run(sid, "Explain the history of port cities.")
                api._agent.worker.join(10)
                self.assertEqual(api._agent.state, "incomplete")
                self.assertEqual(api.get_session(sid)["history"][-1]["incomplete"], "length")
                api.start_run(sid, "Continue, please.")
                api._agent.worker.join(10)
                self.assertEqual(api._agent.state, "completed")
            self.assertEqual(api.get_session(sid)["history"][-1]["content"], "Next part.")
            forwarded = post.call_args.kwargs["json"]["messages"]
            partial = next(row for row in forwarded if row["role"] == "assistant")
            self.assertIn("First part.", partial["content"])
            self.assertNotIn("incomplete", partial)
