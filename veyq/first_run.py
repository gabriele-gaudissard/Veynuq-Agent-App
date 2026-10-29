"""Complete, repeatable setup used by the single-file Windows installer."""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests


def run(argv, **kwargs):
    return subprocess.run(argv,check=True,**kwargs)


def signed_installer(url, publisher, arguments, root, name):
    """Only execute a downloaded installer after Windows publisher verification."""
    folder=Path(root)/'setup';folder.mkdir(exist_ok=True)
    target=folder/name
    session=requests.Session();session.trust_env=False
    try:
        with session.get(url,stream=True,timeout=(15,120)) as response:
            response.raise_for_status()
            if not response.url.startswith('https://'):raise ValueError('Installer download requires HTTPS.')
            total=0
            with target.open('wb') as output:
                for block in response.iter_content(1024*1024):
                    total+=len(block)
                    if total>1_000_000_000:raise ValueError('Installer download exceeds 1 GB.')
                    output.write(block)
        literal=str(target).replace("'","''")
        command="Import-Module (Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1') -ErrorAction Stop; $s=Get-AuthenticodeSignature -LiteralPath '"+literal+"';if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch '"+publisher+"'){exit 1}"
        run(['powershell','-NoProfile','-NonInteractive','-Command',command],timeout=60)
        run([str(target),*arguments],timeout=900)
    finally:
        session.close();target.unlink(missing_ok=True)


def git_path():
    candidates=[shutil.which('git')]
    for base in (os.environ.get('LOCALAPPDATA',''),os.environ.get('ProgramFiles','')):
        candidates.append(str(Path(base)/('Programs/Git/cmd/git.exe' if base==os.environ.get('LOCALAPPDATA') else 'Git/cmd/git.exe')))
    return next((p for p in candidates if p and Path(p).is_file()),None)


def webview_ready():
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER,winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY,winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive,r'Software\Microsoft\EdgeUpdate\Clients',0,winreg.KEY_READ|view) as clients:
                    for i in range(winreg.QueryInfoKey(clients)[0]):
                        with winreg.OpenKey(clients,winreg.EnumKey(clients,i)) as client:
                            try:
                                name=winreg.QueryValueEx(client,'name')[0];version=winreg.QueryValueEx(client,'pv')[0]
                                if 'webview2' in name.lower() and version and version!='0.0.0.0':return True
                            except OSError:pass
            except OSError:pass
    return False


def native_dependencies(store):
    if os.name!='nt':raise RuntimeError('The complete installer supports Windows only.')
    if not webview_ready():
        print('Preparing the Microsoft WebView2 interface runtime...',flush=True)
        signed_installer('https://go.microsoft.com/fwlink/p/?LinkId=2124703','Microsoft Corporation',
                         ['/silent','/install'],store.root,'webview2-setup.exe')
        if not webview_ready():raise RuntimeError('WebView2 installation could not be verified.')
    if not git_path():
        print('Preparing Git for coding and repository tools...',flush=True)
        from .network import public_request
        page=public_request('https://git-scm.com/install/windows',limit=2_000_000)['text']
        match=re.search(r'https://github\.com/git-for-windows/git/releases/download/[^\s"<>]+/Git-[\d.]+-64-bit\.exe',page)
        if not match:raise RuntimeError('The official Git installer could not be identified.')
        destination=Path(os.environ['LOCALAPPDATA'])/'Programs/Git'
        signed_installer(match.group(),'Johannes Schindelin|Git for Windows',
                         ['/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CURRENTUSER','/DIR='+str(destination)],store.root,'git-setup.exe')
        if not git_path():raise RuntimeError('Git installation could not be verified.')
    os.environ['PATH']=str(Path(git_path()).parent)+os.pathsep+os.environ.get('PATH','')
    from .browser import edge_available
    if not edge_available():
        print('Preparing the isolated browser...',flush=True)
        browser_root=store.root/'browser-runtime'
        env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(browser_root)}
        run([sys.executable,'-m','playwright','install','chromium'],env=env,timeout=900)


def local_model(store):
    settings=store.snapshot()['settings']
    if settings['provider']!='local' or settings['url'].rstrip('/') not in {'http://localhost:11434','http://127.0.0.1:11434'}:
        return {'preserved_provider':True}
    from .setup import setup_engine
    setup_engine(store.root)
    session=requests.Session();session.trust_env=False
    try:
        response=session.get('http://127.0.0.1:11434/api/tags',timeout=30);response.raise_for_status()
        installed={r['name'] for r in response.json().get('models',[])}
        configured=settings['model']
        selected=configured if settings.get('setup_completed') or configured in installed else 'qwen3:4b'
        reused=selected in installed or selected+':latest' in installed
        if not reused:
            print('Downloading the initial local model: '+selected+' (first setup may take several minutes)...',flush=True)
            with session.post('http://127.0.0.1:11434/api/pull',json={'model':selected,'stream':True},stream=True,timeout=(10,120)) as download:
                download.raise_for_status();finished=False;last=''
                for line in download.iter_lines():
                    if not line:continue
                    item=json.loads(line)
                    if item.get('error'):raise RuntimeError(item['error'])
                    status=item.get('status','');total=item.get('total',0)
                    progress=(str(int(item.get('completed',0)*100/total)//10*10)+'%') if total else ''
                    message=status+' '+progress
                    if message!=last:print(message,flush=True);last=message
                    if status=='success':finished=True
                if not finished:raise RuntimeError('Model download did not finish successfully.')
        response=session.post('http://127.0.0.1:11434/api/show',json={'model':selected},timeout=30);response.raise_for_status()
        if 'tools' not in response.json().get('capabilities',[]):raise RuntimeError('The configured model does not support agent tools. Choose a tool-capable model in Settings.')
        with store.lock:store.data['settings']['model']=selected;store.save()
        return {'model':selected,'reused':reused}
    finally:session.close()


def prepare(store):
    print('Preparing the complete local agent configuration...',flush=True)
    native_dependencies(store)
    model=local_model(store)
    from .voice import VoiceInput
    voice=VoiceInput(store.root)
    try:
        if not voice.ready():
            print('Preparing the local multilingual dictation model...',flush=True)
            voice.start('prepare')
            while voice.busy():time.sleep(.2)
            if not voice.ready():raise RuntimeError(voice.snapshot().get('error','Speech setup failed.'))
    finally:voice.close()
    with store.lock:
        store.data['settings']['setup_completed']=True;store.save()
    return {'ready':True,'chat':model,'voice_ready':True,'git_ready':bool(git_path()),'interface_ready':webview_ready()}
