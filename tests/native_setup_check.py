"""Compile and execute a harmless installer fixture in an isolated directory."""
import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


def main(compiler):
    with tempfile.TemporaryDirectory(prefix='veynuq-native-setup-') as directory:
        root=Path(directory);payload=root/'payload';payload.mkdir();icon=payload/'assets/brand';icon.mkdir(parents=True)
        shutil.copy2(ROOT/'assets/brand/veynuq-dark.ico',icon/'veynuq-dark.ico')
        (payload/'app.py').write_text('# Controlled installer fixture. No app starts.\n')
        (payload/'build.json').write_text('{}')
        (payload/'install.ps1').write_text("param([switch]$Complete)\nif(-not $Complete){exit 11}\n[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'configured.txt'),'complete')\n",encoding='utf-8')
        subprocess.run([compiler,'/DTestBuild','/DBundleDir='+str(payload),'/DOutputDir='+str(root),str(ROOT/'installer/Veynuq.iss')],check=True,capture_output=True)
        setup=root/'Veynuq-Setup-TEST.exe';destination=root/'installed';log=root/'setup.log'
        env={**os.environ,'VEYNUQ_DATA_DIR':str(root/'profile')}
        result=subprocess.run([str(setup),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/LANG=english','/DIR='+str(destination),'/LOG='+str(log)],env=env,timeout=90,capture_output=True)
        assert result.returncode==0,log.read_text(encoding='utf-8-sig',errors='replace')
        assert (destination/'configured.txt').read_text()=='complete'
        assert not (destination/'unins000.exe').exists(), 'Fixture must not register a real uninstall entry'
        # A source repository cannot be overwritten, even silently.
        protected=root/'source';(protected/'.git').mkdir(parents=True);(protected/'keep.txt').write_text('keep')
        rejected=subprocess.run([str(setup),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/DIR='+str(protected)],env=env,timeout=30,capture_output=True)
        assert rejected.returncode!=0 and (protected/'keep.txt').read_text()=='keep'
        assert not (protected/'app.py').exists()
        import msvcrt
        profile=root/'profile';profile.mkdir();lock=profile/'instance.lock';lock.write_bytes(b'0')
        with lock.open('r+b') as stream:
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            busy=root/'busy'
            rejected=subprocess.run([str(setup),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/DIR='+str(busy)],env=env,timeout=30,capture_output=True)
            assert rejected.returncode!=0 and not (busy/'app.py').exists()
            stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
        # A failed child setup must never present a successful installation.
        (payload/'install.ps1').write_text('param([switch]$Complete)\nexit 17\n',encoding='utf-8')
        subprocess.run([compiler,'/DTestBuild','/DBundleDir='+str(payload),'/DOutputDir='+str(root),str(ROOT/'installer/Veynuq.iss')],check=True,capture_output=True)
        failure=subprocess.run([str(setup),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/DIR='+str(root/'failure')],env=env,timeout=30,capture_output=True)
        assert failure.returncode!=0,'A failed configuration must fail the native installer.'
        print(json.dumps({'native_exe_runs':True,'complete_configuration_invoked':True,'source_directory_protected':True,'running_profile_protected':True,'failed_configuration_not_success':True,'temporary_profile_only':True,'test_shortcuts_and_uninstaller_disabled':True}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--compiler',required=True);main(parser.parse_args().compiler)
