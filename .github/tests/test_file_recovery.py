"""Focused file-conflict recovery checks; all files live in TemporaryDirectory."""
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from veyq.desktop import DesktopAPI
from veyq.editing import RecoveryGuard
from veyq.engine import ModelClient
from veyq.storage import Store
from veyq.tools import ToolRunner


def call(name, **args):
    return {"role":"assistant","content":"","tool_calls":[
        {"id":name+str(id(args)),"function":{"name":name,"arguments":args}}]}


DONE = {"role":"assistant","content":"Done. Verified the result."}


class FileRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root/"project"
        self.project.mkdir()
        self.store = Store(self.root/"profile")
        self.settings = self.store.data["settings"]
        self.settings.update(workspace=str(self.project), permission="full", auto_update=False, max_steps=0)
        self.approvals = []
        self.allowed = True
        self.session = {"history":[]}
        self.runner = ToolRunner(self.store,self.settings,"test",threading.Event(),lambda *a:None,
                                 self.approve,self.root/"app",self.session)
        self.addCleanup(self.runner.close)
        self.guard = RecoveryGuard(self.runner)

    def approve(self, preview):
        self.approvals.append(preview)
        return self.allowed

    def create(self, value="one\nsame\ntwo\nsame\nthree\nsame\n", name="proof.txt"):
        path = self.project/name
        path.write_bytes(value.encode("utf-8"))
        return path

    def execute(self, name, **args):
        result = self.guard.before(name,args)
        if result is None:
            result = self.runner.execute(name,args)
        self.guard.after(name,args,result)
        return result

    def edit(self, **args):
        return self.execute("edit_file",path="proof.txt",**args)

    def human(self, text):
        self.session["history"].append({"role":"user","content":text})

    def test_unique_match_preserves_legacy_interface_and_backup(self):
        path = self.create()
        result = self.edit(old="two\nsame",new="two\nfixed")
        self.assertTrue(result["ok"])
        self.assertEqual(path.read_text(),"one\nsame\ntwo\nfixed\nthree\nsame\n")
        backup = self.store.root/"backups"/result["result"]["backup"]
        self.assertEqual(backup.read_text(),"one\nsame\ntwo\nsame\nthree\nsame\n")

    def test_zero_and_three_matches_are_distinct_and_do_not_write(self):
        path = self.create()
        for old,code,count in [("missing","TEXT_NOT_FOUND",0),("same","AMBIGUOUS_MATCH",3)]:
            result = self.runner.execute("edit_file",{"path":"proof.txt","old":old,"new":"fixed"})
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"],code)
            self.assertEqual(result["match_count"],count)
            self.assertEqual(result["file_sha256"],hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(path.read_text().count("same"),3)
        self.assertFalse((self.store.root/"backups").exists())

    def test_second_occurrence_scoped_to_verified_lines(self):
        path = self.create()
        failed = self.edit(old="same",new="fixed")
        self.assertEqual([r["start_line"] for r in failed["matches"]],[2,4,6])
        result = self.edit(old="same",new="fixed",start_line=4,end_line=4,
                           expected_sha256=failed["file_sha256"])
        self.assertTrue(result["ok"])
        self.assertEqual(path.read_text(),"one\nsame\ntwo\nfixed\nthree\nsame\n")

    def test_range_requires_hash_and_rejects_external_change(self):
        path = self.create()
        result = self.runner.execute("edit_file",{"path":"proof.txt","old":"same","new":"fixed",
                                                  "start_line":4,"end_line":4})
        self.assertEqual(result["code"],"INVALID_EDIT_RANGE")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.write_text("external change")
        result = self.runner.execute("edit_file",{"path":"proof.txt","old":"same","new":"fixed",
                                                  "start_line":4,"end_line":4,"expected_sha256":digest})
        self.assertEqual(result["code"],"FILE_CHANGED")
        self.assertEqual(path.read_text(),"external change")

    def test_crlf_and_overlapping_matches(self):
        path = self.create("start\r\nsame\r\nend\r\n")
        self.assertTrue(self.edit(old="start\nsame",new="start\nnew")["ok"])
        self.assertEqual(path.read_bytes(),b"start\r\nnew\r\nend\r\n")
        path.write_text("aaa")
        result = self.runner.execute("edit_file",{"path":"proof.txt","old":"aa","new":"b"})
        self.assertEqual(result["code"],"AMBIGUOUS_MATCH")
        self.assertEqual(path.read_text(),"aaa")

    def test_unchanged_reads_reused_and_new_ranges_allowed(self):
        path = self.create()
        original = ToolRunner.tool_read_file
        with patch.object(ToolRunner,"tool_read_file",autospec=True,side_effect=original) as read:
            self.execute("read_file",path="proof.txt",start_line=1,end_line=3)
            self.assertTrue(self.execute("read_file",path="./proof.txt",start_line=2,end_line=3)["reused_result"])
            self.execute("read_file",path="proof.txt",start_line=4,end_line=6)
            self.assertEqual(read.call_count,2)
            path.write_text("changed\n")
            self.assertNotIn("reused_result",self.execute("read_file",path="proof.txt"))
            self.assertEqual(read.call_count,3)

    def test_truncated_line_cache_is_not_used_for_different_range(self):
        self.create("x"*31000+"\nsecond\n")
        self.execute("read_file",path="proof.txt",start_line=1,end_line=2)
        result = self.execute("read_file",path="proof.txt",start_line=2,end_line=2)
        self.assertNotIn("reused_result",result)
        self.assertEqual(result["result"]["content"],"2: second")

    def test_duplicate_failed_edit_not_dispatched_and_cache_stops_loop(self):
        self.create()
        read = ToolRunner.tool_read_file
        edit = ToolRunner.tool_edit_file
        with patch.object(ToolRunner,"tool_read_file",autospec=True,side_effect=read) as r, \
             patch.object(ToolRunner,"tool_edit_file",autospec=True,side_effect=edit) as e:
            self.execute("read_file",path="proof.txt")
            self.edit(old="missing",new="fixed")
            self.assertTrue(self.edit(old="missing",new="fixed")["repeated_edit"])
            for _ in range(2):
                self.assertTrue(self.execute("read_file",path="proof.txt")["reused_result"])
            self.assertEqual(r.call_count,1)
            self.assertEqual(e.call_count,1)
            self.assertEqual(self.guard.stopped,"edit_no_progress")

    def test_only_two_concrete_changed_recovery_attempts(self):
        self.create()
        edit = ToolRunner.tool_edit_file
        with patch.object(ToolRunner,"tool_edit_file",autospec=True,side_effect=edit) as e:
            for old in ("missing","missing two","missing three","missing four"):
                self.edit(old=old,new="fixed")
            self.assertEqual(e.call_count,3)
        self.assertEqual(self.guard.stopped,"edit_recovery_exhausted")

    def test_checkpoint_wording_is_not_progress(self):
        for number in range(3):
            result = self.execute("checkpoint_task",progress="different "+str(number),next_steps="action")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"],"CHECKPOINT_NO_PROGRESS")
        self.assertEqual(self.guard.stopped,"checkpoint_no_progress")

    def test_checkpoint_after_real_evidence_and_followup_is_allowed(self):
        self.execute("checkpoint_task",progress="start",next_steps="write")
        self.execute("write_file",path="proof.txt",content="real")
        self.assertTrue(self.execute("checkpoint_task",progress="written",next_steps="inspect")["ok"])
        self.guard.reset()
        self.assertTrue(self.execute("checkpoint_task",progress="new request",next_steps="inspect")["ok"])

    def test_create_and_final_is_not_overwrite_authorization(self):
        result = self.execute("write_file",path="proof.txt",content="original",final=True)
        self.assertTrue(result["ok"])
        self.human("Create proof.txt.")
        result = self.execute("write_file",path="proof.txt",content="other",final=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"],"DESTINATION_EXISTS")
        self.assertEqual((self.project/"proof.txt").read_text(),"original")

    def test_explicit_overwrite_uses_backup_and_atomic_replace(self):
        path = self.create("original")
        self.human("Sovrascrivi proof.txt con la versione corretta.")
        result = self.execute("write_file",path=str(path),content="new",overwrite=True)
        self.assertTrue(result["ok"])
        self.assertFalse(self.approvals)
        self.assertEqual(path.read_text(),"new")
        self.assertEqual((self.store.root/"backups"/result["result"]["backup"]).read_text(),"original")

    def test_new_version_skips_existing_names_and_duplicate_is_reused(self):
        path = self.create("original")
        self.create("other","proof_v2.txt")
        self.human("Create a new version of proof.txt.")
        args = {"path":str(path),"content":"new","overwrite":True}
        result = self.execute("write_file",**args)
        self.assertTrue(result["ok"])
        self.assertEqual(Path(result["result"]["path"]).name,"proof_v3.txt")
        self.assertEqual(result["result"]["conflict_resolution"],"new_version")
        self.execute("checkpoint_task",progress="done",next_steps="finish")
        self.assertTrue(self.execute("write_file",**args)["reused_result"])
        self.assertFalse((self.project/"proof_v4.txt").exists())
        self.assertEqual(path.read_text(),"original")

    def test_ambiguous_replacement_requires_normal_approval_even_full_access(self):
        path = self.create("original")
        self.human("Create proof.txt.")
        self.allowed = False
        result = self.execute("write_file",path="proof.txt",content="new",overwrite=True)
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.approvals),1)
        self.assertEqual(path.read_text(),"original")
        self.allowed = True
        self.guard.reset()
        result = self.execute("write_file",path="proof.txt",content="new",overwrite=True)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.approvals),2)
        self.assertEqual(path.read_text(),"new")

    def test_explicit_do_not_overwrite_is_respected(self):
        path = self.create("original")
        self.human("Do not overwrite proof.txt.")
        result = self.execute("write_file",path="proof.txt",content="new",overwrite=True)
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_text(),"original")

    def test_multipart_new_version_remains_invisible_until_final(self):
        path = self.create("original")
        self.human("Crea una nuova versione di proof.txt.")
        a = self.execute("write_file",path="proof.txt",content="first\n",part=1)
        self.assertFalse(a["result"]["published"])
        self.assertFalse((self.project/"proof_v2.txt").exists())
        b = self.execute("write_file",path="proof.txt",content="second",part=2,final=True)
        self.assertTrue(b["ok"])
        self.assertEqual((self.project/"proof_v2.txt").read_bytes(),b"first\nsecond")
        self.assertEqual(path.read_text(),"original")

    def test_new_version_race_does_not_clobber_external_file(self):
        self.create("original")
        self.human("Create a new version of proof.txt.")
        self.execute("write_file",path="proof.txt",content="first",part=1)
        self.create("external","proof_v2.txt")
        result = self.execute("write_file",path="proof.txt",content="second",part=2,final=True)
        self.assertFalse(result["ok"])
        self.assertEqual((self.project/"proof_v2.txt").read_text(),"external")
        self.assertEqual((self.project/"proof.txt").read_text(),"original")

    def test_backup_failure_or_external_change_preserves_original(self):
        path = self.create("original")
        self.human("Overwrite proof.txt.")
        with patch.object(self.runner,"backup",side_effect=OSError("disk full")):
            result = self.execute("write_file",path="proof.txt",content="new",overwrite=True)
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_text(),"original")
        original_backup = self.runner.backup
        def changed_backup(*args,**kwargs):
            backup = original_backup(*args,**kwargs)
            path.write_text("external")
            return backup
        with patch.object(self.runner,"backup",side_effect=changed_backup):
            result = self.execute("write_file",path="proof.txt",content="new",overwrite=True)
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_text(),"external")

    def test_write_conflict_repeated_call_and_reads_stop_without_dispatch(self):
        path = self.create("original")
        self.human("Create proof.txt.")
        write = ToolRunner.tool_write_file
        read = ToolRunner.tool_read_file
        with patch.object(ToolRunner,"tool_write_file",autospec=True,side_effect=write) as w, \
             patch.object(ToolRunner,"tool_read_file",autospec=True,side_effect=read) as r:
            self.execute("read_file",path="proof.txt")
            self.execute("write_file",path="proof.txt",content="new")
            self.assertTrue(self.execute("write_file",path="proof.txt",content="new")["repeated_write"])
            self.execute("read_file",path="proof.txt")
            self.execute("read_file",path="proof.txt")
            self.assertEqual(w.call_count,1)
            self.assertEqual(r.call_count,1)
        self.assertEqual(self.guard.stopped,"write_conflict_no_progress")
        self.assertEqual(path.read_text(),"original")

    def test_do_not_replace_and_explanation_never_grant_consent(self):
        path = self.create("original")
        for instruction in ("Do not replace proof.txt.", "Explain how to replace proof.txt."):
            self.session["history"] = [{"role":"user","content":instruction}]
            self.allowed = False
            self.guard.reset()
            result = self.execute("write_file",path="proof.txt",content="new",overwrite=True)
            self.assertFalse(result["ok"])
            self.assertEqual(path.read_text(),"original")

    def test_approval_race_preserves_external_change(self):
        path = self.create("original")
        self.human("Create proof.txt.")
        def approve(preview):
            path.write_text("external")
            return True
        self.runner.approve = approve
        result = self.execute("write_file",path="proof.txt",content="new",overwrite=True)
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_text(),"external")

    def test_write_recovery_accepts_at_most_two_changed_attempts(self):
        path = self.create("original")
        self.human("Create proof.txt.")
        write = ToolRunner.tool_write_file
        with patch.object(ToolRunner,"tool_write_file",autospec=True,side_effect=write) as w:
            for number in range(4):
                self.execute("write_file",path="proof.txt",content="new "+str(number))
            self.assertEqual(w.call_count,3)
        self.assertEqual(self.guard.stopped,"write_conflict_no_progress")
        self.assertEqual(path.read_text(),"original")

    def test_bad_arguments_do_not_break_recovery_exit(self):
        for name,args in [("read_file",{}),("write_file",{"path":"x","content":7}),
                          ("edit_file",{"path":"x","old":7,"new":"a"}),
                          ("write_file",{"path":"x","content":"\ud800"})]:
            self.guard.after(name,args,{"ok":False,"error":"schema"})
        self.assertEqual(self.guard.stopped,"")


class AgentFileRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root/"project"
        self.project.mkdir()
        self.store = Store(self.root/"profile")
        self.store.data["settings"].update(workspace=str(self.project),permission="full",auto_update=False,max_steps=0)
        self.api = DesktopAPI(self.store)
        self.sid = self.api.create_session()

    def run_agent(self, replies, prompt="Modify proof.txt."):
        with patch.object(ModelClient,"chat",side_effect=replies) as chat:
            self.api.start_run(self.sid,prompt)
            self.api._agent.worker.join(15)
            if self.api._agent.busy:
                self.api._agent.stop()
                self.api._agent.worker.join(5)
            self.assertFalse(self.api._agent.busy)
        self.session = self.api.get_session(self.sid)
        self.results = [json.loads(m["content"]) for m in self.session["history"] if m["role"]=="tool"]
        calls = [c["id"] for m in self.session["history"] for c in m.get("tool_calls",[])]
        self.assertEqual(calls,[m["tool_call_id"] for m in self.session["history"] if m["role"]=="tool"])
        return chat

    def test_failed_edit_then_precise_recovery_completes_in_four_turns(self):
        path = self.project/"proof.txt"
        path.write_text("first\nsame\nsecond\nsame\n")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        chat = self.run_agent([call("read_file",path="proof.txt"),
                              call("edit_file",path="proof.txt",old="same",new="fixed"),
                              call("edit_file",path="proof.txt",old="same",new="fixed",
                                   start_line=4,end_line=4,expected_sha256=digest),DONE])
        self.assertEqual(chat.call_count,4)
        self.assertEqual(self.api._agent.state,"completed")
        self.assertEqual(path.read_text(),"first\nsame\nsecond\nfixed\n")
        self.assertEqual(self.results[1]["code"],"AMBIGUOUS_MATCH")
        self.assertEqual(self.session["task"]["operations"][-1]["state"],"completed")

    def test_edit_read_loop_is_bounded_and_physical_reads_are_cached(self):
        path = self.project/"proof.txt"
        path.write_text("original")
        read = ToolRunner.tool_read_file
        with patch.object(ToolRunner,"tool_read_file",autospec=True,side_effect=read) as r:
            chat = self.run_agent([call("read_file",path="proof.txt"),
                                   call("edit_file",path="proof.txt",old="missing",new="fixed")] +
                                  [call("read_file",path="proof.txt") for _ in range(10)])
        self.assertEqual(r.call_count,1)
        self.assertEqual(chat.call_count,5)
        self.assertEqual(self.session["task"]["diagnostics"]["category"],"edit_no_progress")
        self.assertNotEqual(self.api._agent.state,"completed")
        self.assertEqual(path.read_text(),"original")

    def test_changed_failed_patches_stop_after_initial_plus_two_recoveries(self):
        path = self.project/"proof.txt"
        path.write_text("original")
        chat = self.run_agent([call("edit_file",path="proof.txt",old="missing "+str(n),new="fixed") for n in range(9)])
        self.assertEqual(chat.call_count,3)
        self.assertEqual(self.session["task"]["diagnostics"]["category"],"edit_recovery_exhausted")
        self.assertEqual(path.read_text(),"original")

    def test_varying_checkpoints_stop_without_fake_completion(self):
        chat = self.run_agent([call("checkpoint_task",progress="analysis "+str(n),next_steps="write") for n in range(9)])
        self.assertEqual(chat.call_count,3)
        self.assertEqual(self.results[1]["code"],"CHECKPOINT_NO_PROGRESS")
        self.assertEqual(self.session["task"]["diagnostics"]["category"],"checkpoint_no_progress")
        self.assertFalse((self.project/"proof.txt").exists())
        self.assertNotEqual(self.api._agent.state,"completed")

    def test_checkpoint_after_verified_action_and_final_reply_completes(self):
        chat = self.run_agent([call("checkpoint_task",progress="start",next_steps="write"),
                              call("write_file",path="proof.txt",content="actual"),
                              call("checkpoint_task",progress="written",next_steps="finish"),DONE],
                             "Create proof.txt.")
        self.assertEqual(chat.call_count,4)
        self.assertEqual(self.api._agent.state,"completed")

    def test_write_conflict_recovers_with_explicit_human_authorization(self):
        path = self.project/"proof.txt"
        path.write_text("original")
        self.run_agent([call("write_file",path="proof.txt",content="new"),
                        call("write_file",path="proof.txt",content="new",overwrite=True),DONE],
                       "Overwrite proof.txt.")
        self.assertEqual(self.api._agent.state,"completed")
        self.assertEqual(path.read_text(),"new")

    def test_requested_new_version_verifies_actual_destination(self):
        path = self.project/"proof.txt"
        path.write_text("original")
        self.run_agent([call("write_file",path="proof.txt",content="new"),DONE],
                       "Create a new version of proof.txt.")
        self.assertEqual(self.api._agent.state,"completed")
        self.assertEqual(path.read_text(),"original")
        self.assertEqual((self.project/"proof_v2.txt").read_text(),"new")
        operation = self.session["task"]["operations"][0]
        self.assertTrue(operation["target"].endswith("proof_v2.txt"))
        self.assertEqual(operation["state"],"completed")
        self.assertEqual(operation["evidence"],"file_readback_sha256")

    def test_multiple_calls_are_not_dispatched_after_recovery_stops(self):
        batch = call("checkpoint_task",progress="one",next_steps="write")
        batch["tool_calls"] += call("checkpoint_task",progress="two",next_steps="write")["tool_calls"]
        batch["tool_calls"] += call("checkpoint_task",progress="three",next_steps="write")["tool_calls"]
        batch["tool_calls"] += call("write_file",path="proof.txt",content="not executed")["tool_calls"]
        self.run_agent([batch],"Create proof.txt.")
        self.assertFalse((self.project/"proof.txt").exists())
        self.assertEqual(self.results[-1]["code"],"RECOVERY_STOPPED")
        self.assertTrue(self.results[-1]["not_executed"])

    def test_file_changed_during_task_can_be_read_again_and_edited(self):
        path = self.project/"proof.txt"
        path.write_text("original")
        replies = [call("read_file",path="proof.txt"),
                   call("edit_file",path="proof.txt",old="missing",new="fixed"),
                   call("read_file",path="proof.txt"),
                   call("edit_file",path="proof.txt",old="external",new="fixed"),DONE]
        def reply(messages):
            if len(replies)==3:
                path.write_text("external")
            return replies.pop(0)
        original = ToolRunner.tool_read_file
        with patch.object(ToolRunner,"tool_read_file",autospec=True,side_effect=original) as read:
            self.run_agent(reply)
        self.assertEqual(read.call_count,2)
        self.assertEqual(path.read_text(),"fixed")
        self.assertEqual(self.api._agent.state,"completed")

    def test_identical_failed_edits_stop_even_after_generic_repeat_limit(self):
        path = self.project/"proof.txt"
        path.write_text("original")
        chat = self.run_agent([call("edit_file",path="proof.txt",old="missing",new="fixed") for _ in range(10)])
        self.assertEqual(chat.call_count,4)
        self.assertEqual(self.session["task"]["diagnostics"]["category"],"edit_no_progress")
        self.assertEqual(path.read_text(),"original")

    def test_identical_failed_writes_stop_even_after_generic_repeat_limit(self):
        path = self.project/"proof.txt"
        path.write_text("original")
        chat = self.run_agent([call("write_file",path="proof.txt",content="new") for _ in range(10)],"Create proof.txt.")
        self.assertEqual(chat.call_count,4)
        self.assertEqual(self.session["task"]["diagnostics"]["category"],"write_conflict_no_progress")
        self.assertEqual(path.read_text(),"original")

    def test_existing_file_never_reported_completed_after_read_loop(self):
        path = self.project/"proof.txt"
        path.write_text("original")
        chat = self.run_agent([call("read_file",path="proof.txt"),
                              call("write_file",path="proof.txt",content="new",final=True)] +
                             [call("read_file",path="proof.txt") for _ in range(9)],"Create proof.txt.")
        self.assertEqual(chat.call_count,5)
        self.assertNotEqual(self.api._agent.state,"completed")
        self.assertEqual(self.session["task"]["diagnostics"]["category"],"write_conflict_no_progress")
        self.assertEqual(path.read_text(),"original")


if __name__ == "__main__":
    unittest.main()
