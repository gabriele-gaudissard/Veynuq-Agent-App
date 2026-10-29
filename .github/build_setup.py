"""Compile a single native installer from the exact signed release payload."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from veyq.updater import validate_bundle


def build(release,compiler):
    release=Path(release).resolve()
    manifest=json.loads((release/'veyq-update.json').read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory(prefix='veynuq-setup-build-') as directory:
        payload=Path(directory)/'payload'
        validate_bundle((release/'veyq-update.zip').read_bytes(),manifest,payload)
        subprocess.run([str(compiler),'/DBundleDir='+str(payload),'/DOutputDir='+str(release),
                        '/DAppVersion='+manifest['version'],str(ROOT/'installer/Veynuq.iss')],check=True)
    setup=release/'Veynuq-Setup.exe'
    digest=hashlib.sha256(setup.read_bytes()).hexdigest()
    (release/'Veynuq-Setup.exe.sha256').write_text(digest+'  Veynuq-Setup.exe\n',encoding='ascii')
    print(json.dumps({'setup':str(setup),'bytes':setup.stat().st_size,'sha256':digest,'commit':manifest['commit']}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('release');parser.add_argument('--compiler',default=str(Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'Inno Setup 6/ISCC.exe'))
    args=parser.parse_args();build(args.release,args.compiler)
