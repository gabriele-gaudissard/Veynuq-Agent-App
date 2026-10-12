"""Read public GitHub marketplaces and install one plugin at a reviewed commit."""
import io
import json
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .network import public_request
from .plugins import Plugins, IDENTIFIER


def repository(value):
    value = value.removeprefix('https://github.com/').removesuffix('.git').rstrip('/') if isinstance(value,str) else ''
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',value) or '..' in value.split('/'):
        raise ValueError('Use a public GitHub owner/repository marketplace.')
    return value


def github_source(value):
    if not isinstance(value,dict):return None
    kind=value.get('source')
    if kind=='github':repo=repository(value.get('repo'))
    elif kind in {'git-subdir','url'} and isinstance(value.get('url'),str) and value['url'].startswith('https://github.com/'):
        repo=repository(value['url'])
    else:return None
    return {**value,'repo':repo}


def catalog(value, revision=''):
    repo = repository(value)
    if not revision:
        meta=json.loads(public_request('https://api.github.com/repos/'+repo)['text'])
        ref=meta['default_branch']
        from urllib.parse import quote
        revision=json.loads(public_request('https://api.github.com/repos/'+repo+'/commits/'+quote(ref,safe=''))['text'])['sha']
    if not re.fullmatch(r'[a-f0-9]{40}',revision): raise ValueError('Use an exact marketplace commit.')
    url=f'https://raw.githubusercontent.com/{repo}/{revision}/.claude-plugin/marketplace.json'
    manifest=json.loads(public_request(url,limit=300000)['text'])
    if not isinstance(manifest,dict) or not isinstance(manifest.get('plugins'),list) or len(manifest['plugins'])>500:
        raise ValueError('Invalid or excessive marketplace manifest.')
    rows=[]
    for entry in manifest['plugins']:
        if not isinstance(entry,dict) or not isinstance(entry.get('name'),str) or not IDENTIFIER.fullmatch(entry['name']):
            raise ValueError('Invalid marketplace plugin name.')
        source=entry.get('source')
        supported=isinstance(source,str) and source.startswith('./') and '..' not in PurePosixPath(source).parts and '\\' not in source and ':' not in source
        if isinstance(source,dict):
            try: supported=bool(github_source(source))
            except ValueError: pass
        rows.append({'name':entry['name'],'description':str(entry.get('description',''))[:500],'source':source,'supported':supported})
    if len({row['name'] for row in rows})!=len(rows): raise ValueError('Duplicate marketplace plugin name.')
    return {'repository':repo,'revision':revision,'name':str(manifest.get('name',repo))[:100],'plugins':rows,'untrusted':True}


def selection(value, revision, name):
    reviewed=catalog(value,revision)
    row=next((p for p in reviewed['plugins'] if p['name']==name and p['supported']),None)
    if not row: raise ValueError('Selected plugin is unavailable or uses an unsupported source.')
    repo=reviewed['repository'];source=row['source'];commit=revision
    if isinstance(source,dict):
        source=github_source(source)
        if not source:raise ValueError('Unsupported plugin source.')
        repo=source['repo']
        ref=source.get('sha') or source.get('ref') or 'HEAD'
        from urllib.parse import quote
        if not isinstance(ref,str) or len(ref)>200: raise ValueError('Invalid plugin source revision.')
        commit=ref if re.fullmatch(r'[a-f0-9]{40}',ref) else json.loads(public_request(f'https://api.github.com/repos/{repo}/commits/'+quote(ref,safe=''))['text'])['sha']
        source='./'+source.get('path','')
    path=PurePosixPath(source)
    if '\\' in source or ':' in source or path.is_absolute() or '..' in path.parts: raise ValueError('Invalid plugin source path.')
    if not re.fullmatch(r'[a-f0-9]{40}',commit): raise ValueError('Invalid plugin commit.')
    return {'repository':repo,'commit':commit,'path':str(path),'name':name,'marketplace':reviewed['repository'],'revision':revision}


def install(store, value, revision, name, reviewed_source):
    # The user confirms an immutable resolved source, never a moving branch.
    manifest=catalog(value,revision)
    entry=next((p for p in manifest['plugins'] if p['name']==name and p['supported']),None)
    if not entry or not isinstance(reviewed_source,dict):raise ValueError('Review the selected plugin source first.')
    repo=repository(reviewed_source.get('repository'));commit=reviewed_source.get('commit','');path=PurePosixPath(reviewed_source.get('path',''))
    if not re.fullmatch(r'[a-f0-9]{40}',commit) or path.is_absolute() or '..' in path.parts or '\\' in str(path) or ':' in str(path):raise ValueError('Invalid reviewed plugin source.')
    source=github_source(entry['source']) if isinstance(entry['source'],dict) else entry['source']
    expected_repo=repository(source['repo']) if isinstance(source,dict) else manifest['repository']
    expected_path=PurePosixPath(source.get('path','')) if isinstance(source,dict) else PurePosixPath(source)
    if repo!=expected_repo or path!=expected_path or not isinstance(source,dict) and commit!=revision or isinstance(source,dict) and source.get('sha') and source['sha']!=commit:
        raise ValueError('Reviewed source does not match the marketplace manifest.')
    blob=public_request(f'https://codeload.github.com/{repo}/zip/{commit}',limit=25_000_000,timeout=60)['data']
    with tempfile.TemporaryDirectory(prefix='veynuq-marketplace-') as folder:
        root=Path(folder);unpacked=root/'plugin';unpacked.mkdir()
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            count=total=0
            seen=set()
            for item in archive.infolist():
                archive_entry=PurePosixPath(item.filename)
                if archive_entry.is_absolute() or '..' in archive_entry.parts or '\\' in item.filename or ':' in item.filename or stat.S_ISLNK(item.external_attr>>16):
                    raise ValueError('Unsafe marketplace archive path.')
                relative=PurePosixPath(*archive_entry.parts[1:])
                try:relative=relative.relative_to(path)
                except ValueError:continue
                if item.is_dir():continue
                if str(relative) in seen:raise ValueError('Duplicate marketplace archive file.')
                seen.add(str(relative));count+=1;total+=item.file_size
                if count>2000 or total>20_000_000 or item.file_size>2_000_000: raise ValueError('Plugin exceeds installation limits.')
                dest=unpacked/str(relative)
                if not dest.resolve().is_relative_to(unpacked):raise ValueError('Plugin archive escaped its folder.')
                dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(archive.read(item))
        if not (unpacked/'plugin.json').exists() and not (unpacked/'.claude-plugin/plugin.json').exists():
            (unpacked/'plugin.json').write_text(json.dumps({'name':name,'description':entry.get('description','')}),encoding='utf-8')
        result=Plugins(store).install(unpacked)
        provenance={'repository':repo,'commit':commit,'path':str(path),'marketplace':manifest['repository'],'revision':revision}
        with store.lock:
            next(p for p in store.data['plugins'] if p['id']==result['id'])['source']=provenance
            store.save()
        result['source']=provenance
        return result
