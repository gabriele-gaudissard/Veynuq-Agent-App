"""Release gate: actual installer, separate app/profile and isolated shortcuts."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0,str(ROOT))
from veyq.updater import DISTRIBUTION_ROOT_FILES,DISTRIBUTION_ASSET_FILES

def main():
    with tempfile.TemporaryDirectory(prefix='veynuq-installer-ci-') as directory:
        base=Path(directory);app=base/'app';app.mkdir()
        for name in (DISTRIBUTION_ROOT_FILES|DISTRIBUTION_ASSET_FILES)-{'build.json'}:
            target=app/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/name,target)
        shutil.copytree(ROOT/'veyq',app/'veyq',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        env={**os.environ,'VEYNUQ_DATA_DIR':str(base/'profile')}
        run=subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(app/'install.ps1'),'-ShortcutDirectory',str(base/'shortcuts')],cwd=app,env=env,timeout=1200,capture_output=True,text=True,encoding='utf-8',errors='replace')
        print(run.stdout+run.stderr,flush=True)
        if run.returncode: raise RuntimeError('Fresh installation failed.')
        runtime=json.loads((app/'runtime.json').read_text(encoding='utf-8'))['python']
        assert Path(runtime).resolve().is_relative_to(app.resolve())
        probe=subprocess.check_output([runtime,'-c','import webview,mcp,jsonschema,faster_whisper,playwright,pywinauto;from veyq.tools import TOOLS;assert len(TOOLS)==44;print("All built-in adapters import successfully")'],cwd=app,env=env,text=True)
        state=json.loads((base/'profile/state.json').read_text(encoding='utf-8'))
        assert state['settings']['lang']=='en' and state['settings']['disabled_tools']==[]
        assert (base/'shortcuts/Veynuq.lnk').is_file()
        again=subprocess.check_output(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(app/'PythonSetup.ps1')],cwd=app,text=True)
        assert 'Skipping Python installation' in again
        print(probe+again,flush=True)
        # python.org installations remain registered after this gate; the CI
        # runner is ephemeral and is discarded after the job.

if __name__=='__main__': main()
