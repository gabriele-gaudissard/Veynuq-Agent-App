import copy
import base64
import io
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from veyq.desktop import DesktopAPI
from veyq.engine import Agent, ModelClient, context_window, validate_endpoint
from veyq.network import public_target
from veyq.storage import Store, redact
from veyq.tools import ToolRunner
from veyq.updater import validate_bundle, program_path, Updater
from veyq.update_worker import apply
from veyq.signing import verify_manifest, sign_manifest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.store = Store(self.root / "data")
        self.settings = {**self.store.data["settings"], "workspace": str(self.workspace)}
        self.events, self.approvals = [], []
        self.cancel = threading.Event()
        self.allowed = True
        self.runner = ToolRunner(self.store, self.settings, "test", self.cancel,
                                 lambda *a: self.events.append(a), self.approve, self.root / "app", {})

    def tearDown(self):
        self.temp.cleanup()

    def approve(self, request):
        self.approvals.append(request)
        return self.allowed

    def test_auto_write_read_edit_backup(self):
        self.assertTrue(self.runner.execute("write_file", {"path": "nested/a.py", "content": "value = 1\n"})["ok"])
        self.assertFalse(self.approvals)
        self.assertTrue(self.runner.execute("edit_file", {"path": "nested/a.py", "old": "1", "new": "2"})["ok"])
        result = self.runner.execute("read_file", {"path": "nested/a.py"})
        self.assertIn("value = 2", result["result"]["content"])
        backups = list((self.store.root / "backups").glob("*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].with_suffix("").read_text(), "value = 1\n")

    def test_read_file_handles_bom_text_and_rejects_executable_without_changes(self):
        for encoding in ('utf-8-sig','utf-16'):
            with self.subTest(encoding=encoding):
                path=self.workspace/'text.txt'
                path.write_bytes('caffè\nsecond line'.encode(encoding))
                result=self.runner.execute('read_file',{'path':'text.txt'})
                self.assertTrue(result['ok']);self.assertEqual(result['result']['total_lines'],2)
                self.assertEqual(result['result']['content'],'1: caffè\n2: second line')
        for name,data in (('program.exe',b'MZ\x90\x00'),('binary.txt',b'plain\x00binary'),('unknown.txt',b'\x90')):
            with self.subTest(name=name):
                path=self.workspace/name;path.write_bytes(data)
                result=self.runner.execute('read_file',{'path':name})
                self.assertFalse(result['ok']);self.assertIn('Binary',result['error'])
                self.assertNotIn('codec',result['error']);self.assertEqual(path.read_bytes(),data)

    def test_ambiguous_edit_does_not_mutate(self):
        (self.workspace / "a.txt").write_text("same same")
        self.assertFalse(self.runner.execute("edit_file", {"path": "a.txt", "old": "same", "new": "x"})["ok"])
        self.assertEqual((self.workspace / "a.txt").read_text(), "same same")

    def test_always_denied_read(self):
        self.settings["permission"] = "always"
        self.allowed = False
        (self.workspace / "a.txt").write_text("private")
        result = self.runner.execute("read_file", {"path": "a.txt"})
        self.assertFalse(result["ok"])
        self.assertEqual(self.approvals[0]["tool"], "read_file")
        self.assertNotIn("private", json.dumps(self.events))

    def test_outside_workspace_requires_approval(self):
        self.allowed = False
        target = self.root / "outside.txt"
        self.assertFalse(self.runner.execute("write_file", {"path": str(target), "content": "x"})["ok"])
        self.assertFalse(target.exists())
        self.assertIn("fuori", self.approvals[0]["reason"])

    def test_credential_files_never_read(self):
        self.settings["permission"] = "full"
        for path in [".env", ".env.production", "server.pem", str(self.store.path)]:
            self.assertFalse(self.runner.execute("read_file", {"path": path})["ok"])

    def test_schema_rejects_unknown_and_wrong_types(self):
        for args in [{"path": "x", "content": "a", "extra": "escape"}, {"path": 12, "content": "a"}, {"path": "x"}]:
            self.assertFalse(self.runner.execute("write_file", args)["ok"])
        self.assertFalse((self.workspace / "x").exists())

    def test_offline_blocks_shell_even_full(self):
        self.settings["permission"] = "full"
        self.assertFalse(self.runner.execute("exec_cmd", {"command": "Write-Output unsafe"})["ok"])
        self.assertFalse(self.runner.execute("read_url", {"url": "https://example.com"})["ok"])

    def test_auto_shell_denial_does_not_execute(self):
        self.settings["network"] = True
        self.allowed = False
        command = "Set-Content denied.txt 'bad'" if os.name == "nt" else "echo bad > denied.txt"
        self.assertFalse(self.runner.execute("exec_cmd", {"command": command})["ok"])
        self.assertFalse((self.workspace / "denied.txt").exists())

    def test_shell_exit_code_and_timeout(self):
        self.settings.update(network=True, permission="full", command_timeout=120)
        command = "Write-Output 'verified'; exit 7" if os.name == "nt" else "echo verified; exit 7"
        r = self.runner.execute("exec_cmd", {"command": command})["result"]
        self.assertEqual(r["exit_code"], 7)
        self.assertIn("verified", r["output"])
        command = "Start-Sleep -Seconds 10" if os.name == "nt" else "sleep 10"
        r = self.runner.execute("exec_cmd", {"command": command, "timeout": 1})["result"]
        self.assertTrue(r["timed_out"])

    def test_cancellation_kills_process(self):
        self.settings.update(network=True, permission="full")
        command = "Start-Sleep -Seconds 20" if os.name == "nt" else "sleep 20"
        results = []
        worker = threading.Thread(target=lambda: results.append(self.runner.execute("exec_cmd", {"command": command})))
        worker.start()
        time.sleep(.5)
        self.cancel.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0]["result"]["cancelled"])

    def test_changed_file_invalidates_approval(self):
        target = self.workspace / "a.txt"
        target.write_text("one")
        self.settings["permission"] = "always"
        def changed(_):
            target.write_text("changed by someone else")
            return True
        self.runner.approve = changed
        self.assertFalse(self.runner.execute("write_file", {"path": "a.txt", "content": "two"})["ok"])
        self.assertEqual(target.read_text(), "changed by someone else")

    def test_move_and_delete_backup(self):
        (self.workspace / "a.txt").write_text("original")
        self.assertTrue(self.runner.execute("move_file", {"path": "a.txt", "destination": "b.txt"})["ok"])
        self.assertTrue(self.runner.execute("delete_file", {"path": "b.txt"})["ok"])
        self.assertFalse((self.workspace / "b.txt").exists())
        self.assertEqual(len(self.approvals), 1)

    def test_search_excludes_secrets(self):
        (self.workspace / "safe.txt").write_text("find me")
        (self.workspace / ".env").write_text("find me secret")
        r = self.runner.execute("search_files", {"query": "find"})["result"]["matches"]
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["path"], "safe.txt")

    def test_link_escape(self):
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (self.workspace / "link").symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            # Directory junctions exercise the same escape without requiring
            # Windows Developer Mode / symbolic-link privileges.
            subprocess.run(["cmd", "/c", "mklink", "/J", str(self.workspace / "link"), str(outside)],
                           check=True, capture_output=True)
        self.allowed = False
        self.assertFalse(self.runner.execute("write_file", {"path": "link/x", "content": "x"})["ok"])
        self.assertFalse((outside / "x").exists())

    def test_private_network_blocked(self):
        for url in ["http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/", "http://224.0.0.1/", "file:///etc/passwd", "https://user:pass@example.com/", "http://example.com:22/"]:
            with self.assertRaises(ValueError):
                public_target(url)

    def test_search_fallback_returns_source_urls(self):
        with patch("veyq.tools.public_request", side_effect=[{"text": "no results"}, {"text": '<li class="b_algo"><h2><a href="https://docs.python.org/3/">Python docs</a></h2><div class="b_caption"><p>Official documentation</p></div></li>'}]):
            results = self.runner.tool_web_search("Python documentation")
        self.assertEqual(results[0]["url"], "https://docs.python.org/3/")

    def test_github_put_requires_approval_in_auto(self):
        self.settings["network"] = True
        self.allowed = False
        result = self.runner.execute("github", {"method": "PUT", "endpoint": "contents/test.txt", "body": {"content": "aGVsbG8=", "message": "update"}})
        self.assertFalse(result["ok"])
        self.assertEqual(self.approvals[0]["tool"], "github")

    def test_remote_provider_requires_online_and_tls(self):
        with self.assertRaises(PermissionError):
            validate_endpoint("https://example.com/v1", False)
        with self.assertRaises(ValueError):
            validate_endpoint("http://example.com/v1", True)
        self.assertEqual(validate_endpoint("http://localhost:11434/", False), "http://localhost:11434")

    def test_vault_does_not_persist_plaintext(self):
        self.store.vault.set("provider", "test-secret-token-1234")
        self.assertEqual(self.store.vault.get("provider"), "test-secret-token-1234")
        if os.name == "nt":
            self.assertNotIn(b"test-secret", self.store.vault.path.read_bytes())
        self.assertNotIn("test-secret", self.store.path.read_text())
        self.store.vault.set("provider", "")

    def test_legacy_migration_preserves_chats_scrubs_token(self):
        legacy = self.root / "legacy.json"
        legacy.write_text(json.dumps({"settings": {"token": "legacy-secret"}, "sessions": [{"id": "old", "title": "Keep", "history": []}]}))
        migrated = Store(self.root / "migrated", legacy)
        self.assertEqual(migrated.data["sessions"][0]["title"], "Keep")
        self.assertEqual(migrated.vault.get("provider"), "legacy-secret")
        self.assertNotIn("legacy-secret", migrated.path.read_text())
        self.assertNotIn("legacy-secret", legacy.read_text())

    def test_bridge_full_access_needs_confirmation(self):
        api = DesktopAPI(self.store)
        with self.assertRaises(ValueError):
            api.save_settings({"permission": "full"})
        self.assertNotEqual(api.get_settings()["permission"], "full")
        api.save_settings({"permission": "full", "confirm_full": True})
        self.assertEqual(api.get_settings()["permission"], "full")

    def test_confirmed_full_access_survives_reload_and_can_be_reduced(self):
        DesktopAPI(self.store).save_settings({'permission':'full','confirm_full':True})
        reopened = Store(self.store.root)
        api = DesktopAPI(reopened)
        self.assertEqual(api.get_settings()['permission'], 'full')
        approvals = []
        runner = ToolRunner(reopened, {**api.get_settings(), 'workspace':str(self.workspace)},
                            'consent-reload', self.cancel, lambda *args: None,
                            lambda request: approvals.append(request) or False, self.root/'app', {})
        target = self.root/'controlled-outside-project.txt'
        result = runner.execute('write_file', {'path':str(target),'content':'verified after reload'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(target.read_text(encoding='utf-8'), 'verified after reload')
        self.assertEqual(approvals, [])
        api.save_settings({'permission':'always'})
        reduced = DesktopAPI(Store(self.store.root))
        self.assertEqual(reduced.get_settings()['permission'], 'always')
        with self.assertRaises(ValueError):
            reduced.save_settings({'permission':'full','confirm_full':False})
        self.assertEqual(Store(self.store.root).data['settings']['permission'], 'always')

    def test_bridge_does_not_expose_credentials(self):
        self.store.vault.set("github", "test-hidden-token")
        api = DesktopAPI(self.store)
        self.assertNotIn("test-hidden-token", json.dumps(api.get_settings()))
        self.assertFalse(hasattr(api, "execute_terminal"))
        self.assertFalse(hasattr(api, "create_or_update_file"))

    def test_manual_explorer_uses_tool_gate(self):
        api = DesktopAPI(self.store)
        sid = api.create_session()
        api.set_workspace(sid, str(self.workspace))
        (self.workspace / "safe.py").write_text("print('hello')")
        result = api.browse_project(sid)
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"][0]["name"], "safe.py")
        preview = api.preview_project_file(sid, "safe.py")
        self.assertIn("hello", preview["result"]["content"])
        api.save_settings({"permission": "always"})
        responses = []
        worker = threading.Thread(target=lambda: responses.append(api.preview_project_file(sid, "safe.py")))
        worker.start()
        for _ in range(30):
            if api._agent.pending:
                break
            time.sleep(.02)
        self.assertTrue(api._agent.busy)
        approval = api.get_events()["approval"]
        self.assertEqual(approval["tool"], "read_file")
        api.resolve_approval(approval["id"], False)
        worker.join(3)
        self.assertFalse(responses[0]["ok"])
        self.assertFalse(api._agent.busy)

    def test_approval_expiry_and_cancellation(self):
        agent = Agent(self.store, self.root / "app")
        results = []
        worker = threading.Thread(target=lambda: results.append(agent.approve({"tool": "write_file", "arguments": {}})))
        worker.start()
        for _ in range(20):
            if agent.pending:
                break
            time.sleep(.05)
        self.assertFalse(agent.resolve("wrong-id", True)["ok"])
        agent.stop()
        worker.join(3)
        self.assertEqual(results, [False])

    def test_agent_loop_executes_tools_backend(self):
        api = DesktopAPI(self.store)
        sid = api.create_session()
        api.set_workspace(sid, str(self.workspace))
        responses = [
            {"role": "assistant", "content": "Creo il file.", "tool_calls": [{"id": "a", "type": "function", "function": {"name": "write_file", "arguments": {"path": "proof.txt", "content": "verified"}}}]},
            {"role": "assistant", "content": "", "tool_calls": [{"id":"b", "function":{"name":"read_file", "arguments":{"path":"proof.txt"}}}]},
            {"role": "assistant", "content": "Completato."},
        ]
        with patch.object(ModelClient, "chat", side_effect=responses):
            api.start_run(sid, "Scrivi un file")
            self.wait_idle(api)
        self.assertEqual((self.workspace / "proof.txt").read_text(), "verified")
        history = api.get_session(sid)["history"]
        self.assertEqual([m["role"] for m in history], ["user", "assistant", "tool", "assistant", "tool", "assistant"])
        self.assertTrue(json.loads(history[2]["content"])["ok"])

    def test_markdown_is_never_executed(self):
        api = DesktopAPI(self.store)
        sid = api.create_session()
        api.set_workspace(sid, str(self.workspace))
        with patch.object(ModelClient, "chat", return_value={"role": "assistant", "content": "```exec_cmd\nSet-Content hacked.txt yes\n```"}):
            api.start_run(sid, "test")
            self.wait_idle(api)
        self.assertFalse((self.workspace / "hacked.txt").exists())

    def wait_idle(self, api):
        for _ in range(100):
            if not api._agent.busy:
                return
            time.sleep(.02)
        self.fail("Agent did not finish")

    def test_context_preserves_tool_pairing(self):
        old = [{"role": "user", "content": "old" * 3000}, {"role": "assistant", "content": "done"}]
        latest = [{"role": "user", "content": "now"}, {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]}, {"role": "tool", "content": "result", "tool_call_id": "x"}]
        self.assertEqual(context_window(old + latest, 1000), latest)

    def test_audit_contains_no_file_contents(self):
        self.runner.execute("write_file", {"path": "a", "content": "private-content-unique"})
        audit = (self.store.root / "audit.jsonl").read_text()
        self.assertNotIn("private-content-unique", audit)
        self.assertIn("completed", audit)

    def test_native_stream_preserves_multiple_tool_indices(self):
        class Response:
            status_code = 200
            def iter_lines(self, **kwargs):
                chunks = [
                    {"message": {"tool_calls": [{"function": {"index": 0, "name": "list_dir", "arguments": {"path": "."}}}]}},
                    {"message": {"tool_calls": [{"function": {"index": 1, "name": "read_file", "arguments": {"path": "a"}}}]}},
                    {"message": {}, "done": True},
                ]
                return (json.dumps(c).encode() for c in chunks)
            def close(self):
                pass
        client = ModelClient(self.settings, "", self.cancel, lambda *a: None)
        client.metadata = {}
        with patch.object(client.session, "post", return_value=Response()):
            result = client.chat([])
        self.assertEqual([c["function"]["name"] for c in result["tool_calls"]], ["list_dir", "read_file"])

    def test_compatible_stream_accumulates_json_arguments(self):
        class Response:
            status_code = 200
            def iter_lines(self, **kwargs):
                chunks = [
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "tool1", "function": {"name": "read_file", "arguments": '{"path":'}}]}}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a.txt"}'}}]}}]},
                    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                ]
                return (b"data: " + json.dumps(c).encode() for c in chunks)
            def close(self):
                pass
        client = ModelClient({**self.settings, "provider": "compatible"}, "", self.cancel, lambda *a: None)
        with patch.object(client.session, "post", return_value=Response()):
            result = client.chat([])
        self.assertEqual(json.loads(result["tool_calls"][0]["function"]["arguments"]), {"path": "a.txt"})


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.signer = Ed25519PrivateKey.generate()
        self.signing_key = base64.b64encode(self.signer.private_bytes_raw()).decode()
        public = base64.b64encode(self.signer.public_key().public_bytes_raw()).decode()
        trust = patch("veyq.signing.TRUSTED_PUBLIC_KEY", public)
        trust.start()
        self.addCleanup(trust.stop)

    def bundle(self, bad=None):
        files = {"app.py": b"pass\n", "index.html": b"new", "build.json": b'{"commit":"new"}',
                 "requirements.txt": b"same", "veyq/desktop.py": b"pass\n", "veyq/update_worker.py": b"pass\n"}
        if bad:
            files[bad] = b"bad"
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for name, content in files.items():
                z.writestr(name, content)
        blob = out.getvalue()
        return blob, sign_manifest({"version": "4.0.0", "sequence": 1, "commit": "a" * 40, "archive_sha256": hashlib.sha256(blob).hexdigest(),
                      "files": {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}}, self.signing_key)

    def test_public_api_quota_fallback_keeps_signed_update_checks(self):
        blob, manifest = self.bundle()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'app';root.mkdir()
            store = Store(Path(directory)/'profile')
            updater = Updater(root, store)
            with patch.object(updater, '_installation', return_value={'commit':'b'*40,'sequence':0}), \
                 patch('veyq.updater.public_request', side_effect=[RuntimeError('HTTP 403: API rate limit'), {'text':json.dumps(manifest)}, {'data':blob}]) as request:
                self.assertTrue(updater.check_and_stage()['ready'])
            self.assertEqual(request.call_args_list[1].args[0], 'https://github.com/gabriele-gaudissard/Veynuq-Agent-App/releases/latest/download/veyq-update.json')
            self.assertIn('/releases/download/build-'+manifest['commit']+'/veyq-update.zip',request.call_args_list[2].args[0])
            updater.pending.unlink()
            damaged={**manifest,'sequence':99}
            with patch.object(updater, '_installation', return_value={'commit':'b'*40,'sequence':0}), \
                 patch('veyq.updater.public_request', side_effect=[RuntimeError('HTTP 429: API rate limit'), {'text':json.dumps(damaged)}]) as request:
                with self.assertRaises(Exception):updater.check_and_stage()
            self.assertEqual(request.call_count,2)
            self.assertFalse(updater.pending.exists())
            with patch.object(updater, '_installation', return_value={'commit':'b'*40,'sequence':0}), \
                 patch('veyq.updater.public_request', side_effect=RuntimeError('HTTP 500: temporary failure')) as request:
                with self.assertRaises(RuntimeError):updater.check_and_stage()
            self.assertEqual(request.call_count,1)

    def test_signatures_reject_unsigned_tampered_and_wrong_key(self):
        blob, manifest = self.bundle()
        verify_manifest(manifest)
        unsigned = {k: v for k, v in manifest.items() if k != "signature"}
        with self.assertRaises(ValueError):
            verify_manifest(unsigned)
        manifest["commit"] = "b" * 40
        with self.assertRaises(ValueError):
            verify_manifest(manifest)
        _, manifest = self.bundle()
        other = Ed25519PrivateKey.generate()
        manifest["signature"]["value"] = base64.b64encode(other.sign(b"malicious")).decode()
        with self.assertRaises(ValueError):
            verify_manifest(manifest)

    def test_bundle_hash_and_zip_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            blob, manifest = self.bundle()
            validate_bundle(blob, manifest, Path(temp) / "stage")
            with self.assertRaises(ValueError):
                validate_bundle(blob + b"tamper", manifest, Path(temp) / "bad")
            blob, manifest = self.bundle("../escape.py")
            with self.assertRaises(ValueError):
                validate_bundle(blob, manifest, Path(temp) / "bad")
            self.assertFalse((Path(temp).parent / "escape.py").exists())

    def test_update_preserves_data_and_backups_old_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "app"
            data = Path(temp) / "data"
            stage = data / "updates" / "stage"
            root.mkdir(); data.mkdir(); stage.mkdir(parents=True)
            blob, manifest = self.bundle()
            validate_bundle(blob, manifest, stage)
            (root / "app.py").write_bytes(b"# old\n")
            (root / "requirements.txt").write_bytes(b"same")
            (data / "state.json").write_text('"private chat"')
            previous = {"root": str(root), "commit": "old", "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}}
            plan = data / "pending-update.json"
            plan.write_text(json.dumps({"root": str(root), "stage": str(stage), "data_root": str(data), "manifest": manifest, "previous": previous}))
            apply(plan, restart=False)
            self.assertEqual((root / "app.py").read_bytes(), b"pass\n")
            self.assertEqual((data / "state.json").read_text(), '"private chat"')
            self.assertFalse(plan.exists())
            self.assertEqual(next((data / "app-backups").glob("*/app.py")).read_bytes(), b"# old\n")

    def test_failed_update_rolls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "app"; data = Path(temp) / "data"; stage = data / "updates" / "stage"
            root.mkdir(); data.mkdir(); stage.mkdir(parents=True)
            blob, manifest = self.bundle()
            validate_bundle(blob, manifest, stage)
            (stage / "app.py").write_text("def broken syntax")
            manifest["files"]["app.py"] = hashlib.sha256((stage / "app.py").read_bytes()).hexdigest()
            manifest = sign_manifest(manifest, self.signing_key)
            (root / "app.py").write_text("# old")
            (root / "requirements.txt").write_bytes(b"same")
            previous = {"root": str(root), "commit": "old", "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}}
            plan = data / "pending-update.json"
            plan.write_text(json.dumps({"root": str(root), "stage": str(stage), "data_root": str(data), "manifest": manifest, "previous": previous}))
            with self.assertRaises(Exception):
                apply(plan, restart=False)
            self.assertEqual((root / "app.py").read_text(), "# old")
            self.assertFalse((root / "index.html").exists())

    def test_worker_rejects_replayed_version_before_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "app"; data = Path(temp) / "data"; stage = data / "updates" / "stage"
            root.mkdir(); stage.mkdir(parents=True)
            blob, manifest = self.bundle()
            validate_bundle(blob, manifest, stage)
            original = b"# keep this file"
            (root / "app.py").write_bytes(original)
            previous = {"sequence": manifest["sequence"], "commit": "old", "files": {"app.py": hashlib.sha256(original).hexdigest()}}
            plan = data / "pending-update.json"
            plan.write_text(json.dumps({"root": str(root), "stage": str(stage), "data_root": str(data), "manifest": manifest, "previous": previous}))
            with self.assertRaises(ValueError):
                apply(plan, restart=False)
            self.assertEqual((root / "app.py").read_bytes(), original)

    def test_early_update_rejection_reopens_only_intact_installation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = (Path(temp) / "app").resolve(); data = (Path(temp) / "data").resolve()
            root.mkdir(); data.mkdir()
            (root / "app.py").write_bytes(b"pass\n")
            (root / "Launcher.ps1").write_bytes(b"# launcher\n")
            previous = {"root": str(root), "commit": "old", "sequence": 0,
                        "files": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in ("app.py", "Launcher.ps1")}}
            (data / "installation.json").write_text(json.dumps(previous))
            _, manifest = self.bundle()
            plan = data / "pending-update.json"
            plan.write_text(json.dumps({"root": str(root), "stage": str(Path(temp) / "outside"), "data_root": str(data), "manifest": manifest, "previous": previous}))
            with patch("veyq.update_worker.subprocess.Popen") as launch:
                with self.assertRaises(ValueError): apply(plan, restart=True)
                launch.assert_called_once()
                if os.name == "nt":
                    command = launch.call_args.args[0]
                    self.assertEqual(command[command.index("-ExecutionPolicy") + 1], "Bypass")
            self.assertEqual((root / "app.py").read_bytes(), b"pass\n")
            (root / "app.py").write_bytes(b"tampered")
            with patch("veyq.update_worker.subprocess.Popen") as launch:
                with self.assertRaises(ValueError): apply(plan, restart=True)
                launch.assert_not_called()

    def test_developer_checkout_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "app"; root.mkdir(); (root / ".git").mkdir()
            store = Store(Path(temp) / "data")
            self.assertFalse(Updater(root, store).check_and_stage()["ready"])


if __name__ == "__main__":
    unittest.main()
