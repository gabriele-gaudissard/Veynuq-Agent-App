import hashlib
import json
import os
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from veyq.desktop import DesktopAPI
from veyq.plugins import Plugins
from veyq.storage import Store
from veyq.tools import ToolRunner, TOOLS
from veyq import tool_setup


class OptionalToolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.workspace=self.root/'project';self.workspace.mkdir()
        self.store=Store(self.root/'profile');self.tokens={}
        self.store.vault.get=lambda k:self.tokens.get(k,'')
        self.store.vault.set=lambda k,v:self.tokens.__setitem__(k,v)
        self.store.data['settings'].update(workspace=str(self.workspace),permission='full',network=True,auto_update=False)
        self.api=DesktopAPI(self.store);self.approvals=[];self.allow=True
        self.runner=ToolRunner(self.store,self.store.snapshot()['settings'],'optional-test',threading.Event(),lambda *a:None,self.approve,self.root/'app',{})
        self.addCleanup(self.runner.close)

    def approve(self,value):self.approvals.append(value);return self.allow

    def plugin(self,origin='user'):
        source=self.root/('source-'+origin);(source/'skills/check').mkdir(parents=True,exist_ok=True)
        (source/'plugin.json').write_text(json.dumps({'name':'sample-'+origin}),encoding='utf-8')
        (source/'skills/check/SKILL.md').write_text('Inspect the requested file.',encoding='utf-8')
        return Plugins(self.store).install(source,installed_by=origin)

    def environment(self,origin='agent'):
        value='a' if origin=='agent' else 'b';identity=value*32
        folder=self.store.root/'tool-environments'/identity;folder.mkdir(parents=True)
        python=folder/('Scripts/python.exe' if os.name=='nt' else 'bin/python');python.parent.mkdir();python.write_text('fixture')
        row={'id':identity,'package':'sample-'+origin,'version':'1','python':str(python),'installed_by':origin}
        self.store.data['tool_environments'].append(row);self.store.save()
        return row,folder

    def test_cleanup_agent_scope_keeps_user_legacy_and_all_defaults(self):
        manual=self.plugin();agent=self.plugin('agent');env,folder=self.environment()
        with self.store.lock:
            self.store.data['plugins'][0].pop('installed_by')
            self.store.data['memory']='keep';self.store.save()
        self.tokens['plugin:'+agent['id']]='agent-secret';self.tokens['provider']='keep-secret'
        before=self.store.snapshot();preview=self.api.preview_optional_tools('agent')
        self.assertEqual(preview['count'],2)
        result=self.api.clear_optional_tools('agent',preview['digest'],True)
        self.assertTrue(result['ok']);self.assertEqual(result['removed_count'],2);self.assertFalse(folder.exists())
        after=self.store.snapshot();self.assertEqual([p['id'] for p in after['plugins']],[manual['id']])
        for key in ('settings','sessions','projects','memory'):self.assertEqual(before[key],after[key])
        self.assertEqual(self.tokens['provider'],'keep-secret');self.assertFalse(self.tokens['plugin:'+agent['id']])
        self.assertEqual(Plugins(Store(self.store.root)).catalog()[0]['installed_by'],'user')

    def test_cleanup_all_and_confirmation_inventory_race(self):
        self.plugin();self.environment('user');preview=self.api.preview_optional_tools('all')
        with self.assertRaises(ValueError):self.api.clear_optional_tools('all',preview['digest'])
        self.plugin('agent')
        with self.assertRaises(ValueError):self.api.clear_optional_tools('all',preview['digest'],True)
        self.assertEqual(len(self.store.snapshot()['plugins']),2)
        preview=self.api.preview_optional_tools('all');result=self.api.clear_optional_tools('all',preview['digest'],True)
        self.assertTrue(result['ok']);self.assertEqual(result['removed_count'],3)
        self.assertEqual(self.api.preview_optional_tools('all')['count'],0)

    def test_cleanup_busy_and_unsafe_path_refused(self):
        self.api._agent.busy=True
        with self.assertRaises(RuntimeError):self.api.preview_optional_tools()
        self.api._agent.busy=False
        outside=self.root/'outside';outside.mkdir();(outside/'keep').write_text('keep')
        self.store.data['tool_environments']=[{'id':'../outside','package':'unsafe','installed_by':'agent'}]
        self.store.save();preview=self.api.preview_optional_tools('agent')
        result=self.api.clear_optional_tools('agent',preview['digest'],True)
        self.assertFalse(result['ok']);self.assertEqual(result['removed_count'],0);self.assertTrue((outside/'keep').exists())

    def test_cleanup_missing_environment_record_and_symlink(self):
        row,folder=self.environment();import shutil;shutil.rmtree(folder)
        preview=self.api.preview_optional_tools('agent');self.assertTrue(self.api.clear_optional_tools('agent',preview['digest'],True)['ok'])
        row,folder=self.environment();shutil.rmtree(folder);outside=self.root/'outside';outside.mkdir();(outside/'keep').write_text('keep')
        try:folder.symlink_to(outside,target_is_directory=True)
        except OSError:return  # Developer Mode/admin is not required for the suite.
        preview=self.api.preview_optional_tools('agent');result=self.api.clear_optional_tools('agent',preview['digest'],True)
        self.assertFalse(result['ok']);self.assertTrue((outside/'keep').exists());folder.unlink()

    def test_binary_pe_strings_elf_and_invalid_no_execution(self):
        data=bytearray(512);data[:2]=b'MZ';struct.pack_into('<I',data,60,64);data[64:68]=b'PE\0\0'
        struct.pack_into('<HH',data,68,0x8664,1);struct.pack_into('<H',data,84,240);struct.pack_into('<H',data,88,0x20b)
        data[328:336]=b'.text\0\0\0';data[400:412]=b'Hello world!'
        target=self.workspace/'sample.exe';target.write_bytes(data);digest=hashlib.sha256(data).hexdigest()
        with patch.object(self.runner,'run_process') as run:
            result=self.runner.execute('inspect_binary',{'path':'sample.exe','mode':'strings'})
            self.assertTrue(result['ok'],result);value=result['result'];self.assertEqual(value['architecture'],'x64');self.assertFalse(value['executed'])
            self.assertEqual(value['sha256'],digest);self.assertEqual(value['sections'][0]['name'],'.text')
            self.assertTrue(any(s['text']=='Hello world!' for s in value['strings']));run.assert_not_called()
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),digest)
        target.write_bytes(b'MZ');self.assertFalse(self.runner.execute('inspect_binary',{'path':'sample.exe'})['ok'])
        target.write_bytes(b'\x7fELF\x02\x01'+b'\0'*12+b'\x3e\0')
        self.assertEqual(self.runner.execute('inspect_binary',{'path':'sample.exe'})['result']['bits'],64)

    def candidate(self):
        return {'package':'sample','version':'1.0','source':'https://pypi.org/project/sample/1.0/','license':'MIT',
                'wheels':[{'url':'https://files.pythonhosted.org/packages/sample-1.0-py3-none-any.whl','sha256':'a'*64,'bytes':100}]}

    def test_pypi_metadata_requires_compatible_official_non_yanked_wheel(self):
        wheel={'url':'https://files.pythonhosted.org/packages/sample-1.0-py3-none-any.whl','filename':'sample-1.0-py3-none-any.whl','digests':{'sha256':'a'*64},'size':100,'packagetype':'bdist_wheel'}
        metadata={'info':{'name':'sample','version':'1.0'},'urls':[wheel]}
        with patch('veyq.tool_setup.public_request',return_value={'text':json.dumps(metadata)}):self.assertEqual(tool_setup.describe('Sample')['package'],'sample')
        for mutation in ({'yanked':True},{'url':'https://example.org/a.whl'},{'filename':'sample-1.0-cp27-cp27m-win32.whl'},{'digests':{'sha256':'bad'}}):
            with patch('veyq.tool_setup.public_request',return_value={'text':json.dumps({**metadata,'urls':[{**wheel,**mutation}]})}):
                with self.assertRaises((ValueError,PermissionError)):tool_setup.describe('sample')
        for value in ('sample==1','https://pypi.org/p/sample','../sample','x;whoami'):
            with self.assertRaises(ValueError):tool_setup.package_name(value)

    def test_installer_pins_hashes_registers_origin_and_reuses(self):
        candidate=self.candidate();calls=[]
        def run(runner,argv,timeout=600):
            calls.append(argv)
            if '--report' in argv:
                report=Path(argv[argv.index('--report')+1]);report.write_text(json.dumps({'install':[{'metadata':{'name':'sample','version':'1.0'},'download_info':{'url':candidate['wheels'][0]['url'],'archive_info':{'hashes':{'sha256':'a'*64}}}}]}))
            if 'venv' in argv:
                python=Path(argv[-1])/('Scripts/python.exe' if os.name=='nt' else 'bin/python');python.parent.mkdir();python.write_text('fixture')
            return {'output':json.dumps({'version':'1.0','commands':['sample']}),'exit_code':0}
        with patch('veyq.tool_setup.run',side_effect=run):
            result=tool_setup.install(self.runner,candidate)
            self.assertEqual(result['installed_by'],'agent');self.assertTrue(any('--require-hashes' in a for a in calls))
            self.assertIn('--hash=sha256:'+'a'*64,(Path(result['python']).parent.parent/'requirements.lock').read_text())
            count=len(calls);self.assertTrue(tool_setup.install(self.runner,candidate)['reused']);self.assertEqual(count,len(calls))
        self.assertEqual(Store(self.store.root).snapshot()['tool_environments'][0]['installed_by'],'agent')

    def test_installer_rejects_resolver_tampering_before_install_rolls_back(self):
        def run(runner,argv,timeout=600):
            self.assertIn('--report',argv)
            Path(argv[argv.index('--report')+1]).write_text(json.dumps({'install':[{'metadata':{'name':'sample','version':'1.0'},'download_info':{'url':self.candidate()['wheels'][0]['url'],'archive_info':{'hashes':{'sha256':'b'*64}}}}]}))
            return {'output':'','exit_code':0}
        with patch('veyq.tool_setup.run',side_effect=run),self.assertRaises(PermissionError):tool_setup.install(self.runner,self.candidate())
        self.assertEqual(list((self.store.root/'tool-environments').iterdir()),[]);self.assertEqual(self.store.snapshot()['tool_environments'],[])

    def test_install_permission_gates_and_denied_candidate_not_reused(self):
        args={'action':'install','package':'sample'}
        with patch('veyq.tool_setup.describe',return_value=self.candidate()) as describe,patch('veyq.tool_setup.install',return_value={'ready':True}) as install:
            for change in ({'network':False},{'disabled_tools':['exec_cmd']},{'execution_environment':'sandbox'},{'_read_only':True}):
                original=dict(self.runner.settings);self.runner.settings.update(change)
                self.assertFalse(self.runner.execute('tool_setup',args)['ok']);install.assert_not_called();self.runner.settings=original
            self.runner.settings['permission']='auto';self.allow=False
            self.assertFalse(self.runner.execute('tool_setup',args)['ok']);self.assertEqual(self.approvals[-1]['package']['package'],'sample');install.assert_not_called()
            self.runner.settings['permission']='full';self.assertTrue(self.runner.execute('tool_setup',args)['ok'])
            self.assertEqual(describe.call_count,2);self.assertEqual(install.call_count,1)

    def test_marketplace_agent_origin_opaque_review_and_disabled_preserved(self):
        source={'repository':'owner/repo','commit':'a'*40,'path':'plugin','name':'sample-agent','marketplace':'owner/catalog','revision':'b'*40}
        with patch('veyq.plugin_marketplace.selection',return_value=source),patch('veyq.plugin_marketplace.install',return_value={'ready':True}) as install:
            self.assertFalse(self.runner.execute('plugin_marketplace',{'action':'install','review_id':'invented'})['ok'])
            review=self.runner.execute('plugin_marketplace',{'action':'review','repository':'owner/catalog','revision':'b'*40,'name':'sample-agent'})['result']
            result=self.runner.execute('plugin_marketplace',{'action':'install','review_id':review['review_id']});self.assertTrue(result['ok'],result)
            self.assertEqual(install.call_args.kwargs,{'installed_by':'agent'})
            row=self.plugin('agent');Plugins(self.store).set_enabled(row['id'],False);install.reset_mock()
            self.assertFalse(self.runner.execute('plugin_marketplace',{'action':'install','review_id':review['review_id']})['ok']);install.assert_not_called()

    def test_catalog_contains_all_tools_exactly_once(self):
        catalog=self.api.get_tool_catalog();names=[t['name'] for g in catalog['groups'] for t in g['tools']]
        self.assertEqual(catalog['total'],49);self.assertEqual(len(names),len(set(names)));self.assertEqual(set(names),{t['function']['name'] for t in TOOLS})


if __name__=='__main__':unittest.main()
