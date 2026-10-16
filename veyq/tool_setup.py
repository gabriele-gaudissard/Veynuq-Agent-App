"""Install needed PyPI tools into a separate, disposable Python environment."""
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version
from .network import public_request

PIP = "import os,runpy,sys; [os.environ.pop(k) for k in list(os.environ) if k.startswith('PIP_')]; os.environ['PIP_CONFIG_FILE']=os.devnull; sys.argv=['pip',*sys.argv[1:]]; runpy.run_module('pip',run_name='__main__')"
TAGS=set(sys_tags())


def package_name(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',value):
        raise ValueError('Use one PyPI package name, not a URL, command or requirement expression.')
    return canonicalize_name(value)


def describe(package, version=''):
    package=package_name(package)
    if version and (len(version)>100 or not re.fullmatch(r'[A-Za-z0-9.!+-]+',version)):
        raise ValueError('Use one exact package version.')
    if version: Version(version)
    url=f'https://pypi.org/pypi/{package}/'+(version+'/' if version else '')+'json'
    meta=json.loads(public_request(url,limit=5_000_000)['text'])
    info=meta['info'];resolved=str(info['version'])
    if not version and (Version(resolved).is_prerelease or Version(resolved).is_devrelease):
        stable=[Version(v) for v,files in meta.get('releases',{}).items()
                if files and not Version(v).is_prerelease and not Version(v).is_devrelease
                and any(not f.get('yanked') for f in files)]
        if not stable:raise ValueError('No stable release found. Review the package documentation.')
        return describe(package,str(max(stable)))
    if canonicalize_name(info['name'])!=package or version and Version(resolved)!=Version(version):
        raise ValueError('PyPI metadata does not match the requested package/version.')
    wheels=[]
    for item in meta.get('urls',[]):
        parsed=urlsplit(item.get('url',''))
        digest=item.get('digests',{}).get('sha256','')
        if item.get('packagetype')!='bdist_wheel' or item.get('yanked'):continue
        wheel_name,wheel_version,_,tags=parse_wheel_filename(item['filename'])
        if canonicalize_name(wheel_name)!=package or wheel_version!=Version(resolved) or not TAGS.intersection(tags):continue
        if parsed.scheme!='https' or parsed.hostname!='files.pythonhosted.org' or parsed.username or parsed.password or parsed.port not in (None,443) or any(c.isspace() for c in item['url']):
            raise ValueError('Wheel download must use the official PyPI file host over HTTPS.')
        if not re.fullmatch(r'[a-f0-9]{64}',digest) or not 0<int(item.get('size',0))<=100_000_000:
            continue
        wheels.append({'url':item['url'],'sha256':digest,'bytes':item['size']})
    if not wheels:raise ValueError('No supported non-yanked wheel under 100 MB. Use an official native installer through the normal command gate when appropriate.')
    return {'package':package,'version':resolved,'summary':str(info.get('summary') or '')[:1000],
            'license':str(info.get('license_expression') or info.get('license') or 'See upstream license')[:1000],
            'source':f'https://pypi.org/project/{package}/{resolved}/','wheels':wheels,
            'note':'External code. A virtual environment isolates dependencies, not OS permissions. Dependencies are resolved from PyPI; downloads are verified before the environment is used.'}


def installed(store):
    root=(store.root/'tool-environments').resolve()
    rows=[]
    for row in store.snapshot().get('tool_environments',[]):
        python=Path(row.get('python','')).resolve()
        if python.is_relative_to(root) and python.is_file():rows.append(row)
    return rows


def run(runner, argv, timeout=600):
    result=runner.run_process(argv,runner.workspace,timeout)
    if result['exit_code'] or result.get('cancelled') or result.get('timed_out'):
        raise RuntimeError('Tool setup failed: '+result.get('output','')[-3000:])
    if runner.cancel.is_set():raise RuntimeError('Tool setup stopped.')
    return result


def install(runner, candidate):
    package=package_name(candidate['package']);version=candidate['version']
    for row in installed(runner.store):
        if row['package']==package and row['version']==version:
            return {**row,'reused':True}
    root=runner.store.root/'tool-environments'
    from .tools import linklike
    if root.exists() and linklike(root):raise PermissionError('Tool environment folder cannot be a link.')
    root.mkdir(exist_ok=True)
    target=root/uuid.uuid4().hex
    target.mkdir()
    expected=target.resolve()
    try:
        report=target/'resolve-report.json'
        run(runner,[sys.executable,'-I','-c',PIP,'--isolated','--disable-pip-version-check','install',
                    '--dry-run','--ignore-installed','--no-input','--no-cache-dir','--only-binary=:all:',
                    '--index-url','https://pypi.org/simple','--report',str(report),package+'=='+version])
        if report.stat().st_size>1_000_000:raise ValueError('Package report exceeds its limit.')
        items=json.loads(report.read_text(encoding='utf-8'))['install']
        if not 1<=len(items)<=50:raise ValueError('Unexpected or excessive package set.')
        verified=[];seen=set();total_bytes=0
        for item in items:
            name=package_name(item['metadata']['name']);release=item['metadata']['version']
            if name in seen:raise ValueError('Duplicate distribution in package report.')
            seen.add(name)
            source=item['download_info'];url=source['url'];digest=source['archive_info']['hashes']['sha256']
            parsed=urlsplit(url)
            if parsed.scheme!='https' or parsed.hostname!='files.pythonhosted.org' or not parsed.path.endswith('.whl'):
                raise PermissionError('Only official PyPI wheels can be installed by tool_setup.')
            approved=candidate if name==package else describe(name,release)
            wheel=next((w for w in approved['wheels'] if w['url']==url and w['sha256']==digest),None)
            if Version(release)!=Version(approved['version']) or not wheel:
                raise PermissionError('Installed download does not match the reviewed PyPI hash/version.')
            total_bytes+=wheel['bytes']
            if total_bytes>250_000_000:raise ValueError('Resolved tools exceed the 250 MB download limit. Use a reviewed native installer for larger toolchains.')
            verified.append({'package':name,'version':release,'url':url,'sha256':digest})
        if package not in seen:raise ValueError('Requested tool was not installed.')
        # Resolve metadata without running build hooks, then let native pip
        # enforce the exact reviewed wheel hashes for the entire dependency set.
        lock=target/'requirements.lock'
        lock.write_text('\n'.join(r['package']+' @ '+r['url']+' --hash=sha256:'+r['sha256'] for r in verified)+'\n',encoding='utf-8')
        run(runner,[sys.executable,'-I','-m','venv',str(target)])
        python=target/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        run(runner,[str(python),'-I','-c',PIP,'--isolated','--disable-pip-version-check','install',
                    '--no-input','--no-cache-dir','--only-binary=:all:','--no-index','--no-deps',
                    '--require-hashes','-r',str(lock)])
        run(runner,[str(python),'-I','-c',PIP,'--isolated','--disable-pip-version-check','check'],60)
        probe="import importlib.metadata as m,json;d=m.distribution("+repr(package)+");print(json.dumps({'version':d.version,'commands':[e.name for e in d.entry_points if e.group=='console_scripts']}))"
        result=run(runner,[str(python),'-I','-c',probe],30)
        details=json.loads(result['output'])
        if Version(details['version'])!=Version(version):raise ValueError('Installed tool version verification failed.')
        row={'id':target.name,'package':package,'version':version,'python':str(python),
             'scripts':str(python.parent),'commands':details['commands'],'source':candidate['source'],
             'license':candidate['license'],'distributions':verified,'ready':True,'installed_by':'agent'}
        from .storage import atomic_json
        atomic_json(target/'provenance.json',row)
        with runner.store.lock:
            runner.store.data.setdefault('tool_environments',[]).append(row);runner.store.save()
        return {**row,'reused':False,'next_step':'Use this Python executable or its Scripts/bin commands through exec_cmd, then verify the requested task.'}
    except BaseException:
        # Only remove the freshly created, owned environment; never a project.
        if target.resolve()==expected and expected.parent==root.resolve() and re.fullmatch(r'[a-f0-9]{32}',target.name):
            shutil.rmtree(target,ignore_errors=True)
        raise
