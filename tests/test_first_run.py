import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from veyq import first_run
from veyq.storage import Store


class FirstRunTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'profile')

    def session(self,models):
        session=Mock();session.get.return_value.json.return_value={'models':[{'name':m} for m in models]}
        show=Mock();show.json.return_value={'capabilities':['tools']};session.post.return_value=show
        return session

    def test_existing_local_model_and_all_settings_preserved(self):
        self.store.data['settings'].update(model='qwen3:14b',setup_completed=True,lang='it',permission='auto')
        before=self.store.snapshot()['settings'];session=self.session(['qwen3:14b'])
        with patch('veyq.first_run.requests.Session',return_value=session),patch('veyq.setup.setup_engine'):
            result=first_run.local_model(self.store)
        self.assertEqual(result,{'model':'qwen3:14b','reused':True});self.assertEqual(self.store.snapshot()['settings'],before)
        self.assertEqual(session.post.call_count,1)

    def test_fresh_profile_downloads_starter_and_verifies_tools(self):
        session=self.session([]);download=Mock();download.iter_lines.return_value=[b'{"status":"downloading","total":100,"completed":50}',b'{"status":"success"}']
        stream=Mock();stream.__enter__=Mock(return_value=download);stream.__exit__=Mock(return_value=False)
        show=Mock();show.json.return_value={'capabilities':['tools']};session.post.side_effect=[stream,show]
        with patch('veyq.first_run.requests.Session',return_value=session),patch('veyq.setup.setup_engine'):
            result=first_run.local_model(self.store)
        self.assertEqual(result['model'],'qwen3:4b');self.assertFalse(result['reused'])
        self.assertEqual(session.post.call_args_list[0].kwargs['json']['model'],'qwen3:4b')

    def test_failed_download_does_not_mark_profile_configured(self):
        session=self.session([]);download=Mock();download.iter_lines.return_value=[b'{"error":"controlled failure"}']
        session.post.return_value.__enter__=Mock(return_value=download);session.post.return_value.__exit__=Mock(return_value=False)
        with patch('veyq.first_run.requests.Session',return_value=session),patch('veyq.setup.setup_engine'):
            with self.assertRaisesRegex(RuntimeError,'controlled failure'):first_run.local_model(self.store)
        self.assertFalse(self.store.snapshot()['settings'].get('setup_completed',False))

    def test_remote_provider_and_custom_local_endpoint_preserved(self):
        for change in ({'provider':'compatible'},{'provider':'local','url':'http://custom:11434'}):
            self.store.data['settings'].update(change)
            with patch('veyq.setup.setup_engine') as setup,patch('veyq.first_run.requests.Session') as client:
                self.assertTrue(first_run.local_model(self.store)['preserved_provider']);setup.assert_not_called();client.assert_not_called()

    def test_complete_setup_reuses_voice_and_marks_only_after_success(self):
        voice=Mock();voice.ready.return_value=True
        with patch('veyq.first_run.native_dependencies'),patch('veyq.first_run.local_model',return_value={'reused':True}),patch('veyq.voice.VoiceInput',return_value=voice),patch('veyq.first_run.git_path',return_value='git'),patch('veyq.first_run.webview_ready',return_value=True):
            self.assertTrue(first_run.prepare(self.store)['ready'])
        voice.start.assert_not_called();voice.close.assert_called_once();self.assertTrue(self.store.snapshot()['settings']['setup_completed'])

    def test_bad_publisher_never_executes_download_and_cleans_file(self):
        response=Mock();response.url='https://example.org/setup.exe';response.iter_content.return_value=[b'controlled test']
        session=Mock();session.get.return_value.__enter__=Mock(return_value=response);session.get.return_value.__exit__=Mock(return_value=False)
        with patch('veyq.first_run.requests.Session',return_value=session),patch('veyq.first_run.run',side_effect=RuntimeError('signature rejected')) as run:
            with self.assertRaisesRegex(RuntimeError,'signature rejected'):first_run.signed_installer(response.url,'Expected Publisher',[],self.store.root,'fixture.exe')
        self.assertEqual(run.call_count,1);self.assertEqual(list((self.store.root/'setup').iterdir()),[])


if __name__=='__main__':unittest.main()
