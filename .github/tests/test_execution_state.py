import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from veyq.desktop import DesktopAPI
from veyq.engine import ModelClient, ModelFailure
from veyq.progress import TaskState
from veyq.storage import Store
from veyq.tools import ToolRunner


def call(name, **args):
    return {"role":"assistant","content":"","tool_calls":[
        {"id":name,"function":{"name":name,"arguments":args}}]}


def text(value):
    return {"role":"assistant","content":value}


class ExecutionStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.project=self.root/"project";self.project.mkdir()
        self.store=Store(self.root/"profile")
        self.store.data["settings"].update(permission="full",workspace=str(self.project),auto_update=False)
        self.api=DesktopAPI(self.store);self.sid=self.api.create_session()

    def run_agent(self, replies, prompt="Crea un file saluto.txt che contenga esattamente Ciao mondo."):
        with patch.object(ModelClient,"chat",side_effect=replies) as chat:
            self.api.start_run(self.sid,prompt)
            self.api._agent.worker.join(10)
            if self.api._agent.busy:
                self.api._agent.stop();self.api._agent.worker.join(5)
            self.assertFalse(self.api._agent.busy)
        self.session=self.api.get_session(self.sid)
        self.execution=self.session["task"]["execution"]
        return chat

    def test_real_write_verified_and_terminal_in_two_iterations(self):
        chat=self.run_agent([call("write_file",path="saluto.txt",content="Ciao mondo."),text("File creato e verificato.")])
        self.assertEqual((self.project/"saluto.txt").read_bytes(),b"Ciao mondo.")
        self.assertEqual(chat.call_count,2)
        self.assertEqual(self.execution["phase"],"COMPLETED")
        phases=[r["phase"] for r in self.execution["transitions"]]
        for phase in ("ANALYZING","ACTION_REQUIRED","WAITING_FOR_TOOL_CALL","EXECUTING","VERIFYING","COMPLETED"):
            self.assertIn(phase,phases)
        self.assertEqual(self.session["task"]["operations"][0]["evidence"],"file_readback_sha256")

    def test_promise_without_call_cannot_create_or_complete(self):
        chat=self.run_agent([text("Ora creo il file."),text("Adesso scrivo il file richiesto.")])
        self.assertEqual(chat.call_count,2)
        self.assertFalse((self.project/"saluto.txt").exists())
        self.assertEqual(self.execution["phase"],"FAILED")
        self.assertEqual(self.session["task"]["diagnostics"]["category"],"repeated_analysis")
        self.assertGreaterEqual(self.execution["text_repetitions"],1)
        self.assertFalse(any("Ora creo" in m.get("content","") for m in self.session["history"]))

    def test_similar_analysis_then_real_action_recovers(self):
        chat=self.run_agent([text("Ho analizzato il file. Ora creo la versione corretta."),
                             call("write_file",path="saluto.txt",content="Ciao mondo."),text("Verificato.")])
        self.assertEqual(chat.call_count,3)
        self.assertEqual(self.execution["phase"],"COMPLETED")
        self.assertEqual((self.project/"saluto.txt").read_bytes(),b"Ciao mondo.")

    def test_repeated_analysis_stops_before_third_unproductive_reply(self):
        chat=self.run_agent([text("Ho analizzato il file e individuato i problemi."),
                             text("L'analisi ha identificato le criticità da correggere."),
                             text("Altra analisi.")])
        self.assertEqual(chat.call_count,2)
        self.assertEqual(self.execution["phase"],"FAILED")
        self.assertEqual(self.execution["text_repetitions"],1)

    def test_later_promises_cannot_finish_multistep_request(self):
        chat=self.run_agent([call("write_file",path="a.txt",content="A"),
                             text("Ora creo il secondo file."),
                             text("Adesso scrivo la nuova versione del secondo file.")],
                            "Crea a.txt e b.txt.")
        self.assertEqual(chat.call_count,3)
        self.assertEqual((self.project/"a.txt").read_text(),"A")
        self.assertFalse((self.project/"b.txt").exists())
        self.assertEqual(self.api._agent.state,"partial")
        self.assertEqual(self.execution["phase"],"PARTIAL")

    def test_report_can_finalize_verified_work_without_forcing_extra_actions(self):
        modes=[]
        replies=[call("write_file",path="saluto.txt",content="Ciao mondo."),
                 text("Ora creo una nuova versione corretta."),text("saluto.txt contiene Ciao mondo. Risultato verificato.")]
        def generate(client,messages):
            modes.append(client.tool_choice)
            return replies.pop(0)
        with patch.object(ModelClient,"chat",generate):
            self.api.start_run(self.sid,"Crea saluto.txt.")
            self.api._agent.worker.join(10)
        self.assertEqual(modes,["required","auto","auto"])
        self.assertEqual(self.api._agent.state,"completed")

    def test_done_text_cannot_hide_a_missing_explicit_file_target(self):
        self.run_agent([call("write_file",path="a.txt",content="A"),
                        text("Done."),text("Done.")], "Create a.txt and b.txt.")
        self.assertEqual(self.api._agent.state,"partial")
        self.assertFalse((self.project/"b.txt").exists())
        self.assertTrue(any(path.endswith("b.txt") for path in self.execution["pending_targets"]))

    def test_unverified_file_is_not_blindly_written_again(self):
        writes=ToolRunner.tool_write_file
        with patch("veyq.verification.verify_file",return_value=False), \
             patch.object(ToolRunner,"tool_write_file",autospec=True,side_effect=writes) as write, \
             patch.object(ToolRunner,"tool_read_file",side_effect=RuntimeError("Unavailable")):
            self.run_agent([call("write_file",path="saluto.txt",content="Ciao mondo."),
                            call("write_file",path="saluto.txt",content="other",overwrite=True),
                            text("Done."),text("Done."),text("Done.")])
        self.assertEqual(write.call_count,1)
        self.assertEqual((self.project/"saluto.txt").read_text(),"Ciao mondo.")
        self.assertNotEqual(self.execution["phase"],"COMPLETED")

    def test_changed_arguments_do_not_replay_an_uncertain_file_mutation(self):
        with patch.object(ToolRunner,"execute",return_value={"ok":False,"outcome_unknown":True,"error":"Uncertain fixture"}) as execute:
            self.run_agent([call("write_file",path="saluto.txt",content="first"),
                            call("write_file",path="saluto.txt",content="different",overwrite=True),
                            text("Done."),text("Done.")])
        self.assertEqual(execute.call_count,1)
        self.assertNotEqual(self.execution["phase"],"COMPLETED")

    def test_another_activity_change_is_detected_before_final_completion(self):
        count=0
        def generate(messages):
            nonlocal count
            count+=1
            if count==1:return call("write_file",path="saluto.txt",content="Ciao mondo.")
            (self.project/"saluto.txt").write_text("changed by another controlled activity")
            return text("Done.")
        self.run_agent(generate)
        self.assertEqual(self.api._agent.state,"unverified")
        self.assertEqual(self.execution["phase"],"PARTIAL")
        self.assertEqual(self.execution["last_error"],"file_changed_after_verification")
        self.assertEqual((self.project/"saluto.txt").read_text(),"changed by another controlled activity")

    def test_confirmed_results_survive_model_history_compaction(self):
        observed=[]
        def generate(messages):
            observed.append(copy.deepcopy(messages))
            if len(observed)==1:return call("write_file",path="saluto.txt",content="Ciao mondo.")
            return text("Verified.")
        from veyq.durability import compact
        def compacted(session,limit):
            # Simulate removal of older protocol exchanges from model input.
            return [m for m in compact(session,limit) if m["role"]=="user"]
        with patch("veyq.durability.compact",compacted):
            self.run_agent(generate)
        status=next(m["content"] for m in observed[1] if "Evidence ledger:" in m.get("content",""))
        self.assertIn("saluto.txt",status)
        self.assertIn("confirmed",status)
        self.assertEqual(self.execution["phase"],"COMPLETED")

    def test_existing_file_protection_and_failure_metadata(self):
        (self.project/"saluto.txt").write_text("preserve")
        self.run_agent([call("write_file",path="saluto.txt",content="Ciao mondo."),
                        text("Non è stato possibile completare."),text("Non è stato possibile completare.")])
        self.assertEqual((self.project/"saluto.txt").read_text(),"preserve")
        self.assertEqual(self.execution["phase"],"FAILED")
        self.assertTrue(self.execution["last_error"])

    def test_duplicate_verified_write_is_not_reexecuted(self):
        write=ToolRunner.tool_write_file
        with patch.object(ToolRunner,"tool_write_file",autospec=True,side_effect=write) as execution:
            self.run_agent([call("write_file",path="saluto.txt",content="Ciao mondo."),
                            call("write_file",path="saluto.txt",content="Ciao mondo."),text("Verificato.")])
        self.assertEqual(execution.call_count,1)
        self.assertEqual(self.execution["phase"],"COMPLETED")

    def test_iteration_limit_and_truncation_never_complete(self):
        self.store.data["settings"]["max_steps"]=1
        self.run_agent([call("read_file",path="missing.txt")])
        self.assertEqual(self.execution["phase"],"FAILED")
        self.assertEqual(self.api._agent.state,"limit")
        self.store.data["settings"]["max_steps"]=0
        self.run_agent([ModelFailure("truncated","Controlled truncation")])
        self.assertEqual(self.execution["phase"],"FAILED")
        self.assertEqual(self.session["task"]["diagnostics"]["category"],"truncated")

    def test_partial_work_survives_backend_failure_and_can_resume(self):
        self.run_agent([call("write_file",path="saluto.txt",content="Ciao mondo."),
                        ModelFailure("timeout","Controlled timeout")])
        self.assertEqual(self.execution["phase"],"PARTIAL")
        self.assertEqual(self.api._agent.state,"partial")
        self.assertEqual((self.project/"saluto.txt").read_text(),"Ciao mondo.")
        previous=len(self.session["history"])
        self.run_agent([text("Il lavoro registrato è disponibile.")],"Spiega i risultati precedenti.")
        self.assertGreater(len(self.session["history"]),previous)
        self.assertEqual(self.execution["phase"],"COMPLETED")

    def test_reading_and_analysis_dont_count_as_mutation(self):
        (self.project/"saluto.txt").write_text("original")
        self.run_agent([call("read_file",path="saluto.txt"),text("Ho analizzato. Ora creo una nuova versione."),
                        text("Adesso correggo la versione analizzata.")],"Modifica saluto.txt.")
        self.assertEqual(self.api._agent.state,"failed")
        self.assertEqual((self.project/"saluto.txt").read_text(),"original")

    def test_text_explanation_cannot_become_unauthorized_action(self):
        self.run_agent([text("L'esempio mostra come ora creo il file con una funzione.")],
                       "Spiegami come creare un file, senza eseguire operazioni.")
        self.assertEqual(self.execution["phase"],"COMPLETED")
        self.assertFalse((self.project/"saluto.txt").exists())

    def test_semantic_intentions_and_evidence_summary_are_distinct(self):
        state=TaskState(self.store,{"task":{"goal":"Fix the file","operations":[]},"plan":[
            {"step":"Create result","status":"pending"}]})
        for value in ["Ora creo una versione corretta.","Procedo a scrivere la nuova versione.","Now I will write the corrected file."]:
            self.assertTrue(state.text(value,True))
        self.assertEqual(state.text_repetitions,2)
        self.assertEqual(state.summary()["confirmed"],[])
        self.assertEqual(state.summary()["pending_steps"],["Create result"])
