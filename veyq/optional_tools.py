"""Inventory-bound cleanup of optional components owned by this profile."""
import hashlib
import json
import re
import shutil


def preview(store, scope):
    if scope not in {'all','agent'}:raise ValueError('Invalid cleanup scope.')
    snapshot=store.snapshot();items=[]
    for kind,key in [('plugin','plugins'),('python','tool_environments')]:
        for row in snapshot.get(key,[]):
            origin='agent' if row.get('installed_by')=='agent' else 'user'
            if scope=='agent' and origin!='agent':continue
            items.append({'kind':kind,'id':row['id'],'name':row.get('name',row.get('package','')),
                          'version':row.get('version',''),'installed_by':origin})
    items.sort(key=lambda r:(r['kind'],r['id']))
    # Bind confirmation to full records too: changed origin, payload or paths
    # invalidate an earlier preview even when display names have not changed.
    records={key:snapshot.get(key,[]) for key in ('plugins','tool_environments')}
    digest=hashlib.sha256(json.dumps([scope,records],sort_keys=True).encode()).hexdigest()
    return {'scope':scope,'items':items,'count':len(items),'digest':digest}


def clear(store, scope, digest, confirmed):
    if confirmed is not True:raise ValueError('Preview and confirm optional tool removal first.')
    with store.lock:
        selection=preview(store,scope)
        if digest!=selection['digest']:raise ValueError('Optional tools changed. Review a new preview before removing anything.')
        from .plugins import Plugins
        from .tools import linklike
        manager=Plugins(store);removed=[];errors=[]
        for item in selection['items']:
            try:
                if item['kind']=='plugin':
                    root=manager.root;target=root/item['id']
                else:
                    root=store.root/'tool-environments';target=root/item['id']
                # Check absolute targets in the same filesystem API before any
                # recursive deletion. Reparse points are never followed.
                if not re.fullmatch(r'[a-f0-9]{32}',item['id']) or (root.exists() or root.is_symlink()) and linklike(root) or (target.exists() or target.is_symlink()) and linklike(target) or target.resolve().parent!=root.resolve():
                    raise ValueError('Unsafe optional component path; removal refused.')
                if item['kind']=='plugin':manager.remove(item['id'])
                else:
                    if target.exists():shutil.rmtree(target)
                    store.data['tool_environments']=[r for r in store.data['tool_environments'] if r['id']!=item['id']]
                    store.save()
                removed.append(item)
            except Exception as error:
                errors.append({'name':item['name'],'error':str(error)[:500]})
        return {'ok':not errors,'removed_count':len(removed),'removed':removed,'errors':errors}
