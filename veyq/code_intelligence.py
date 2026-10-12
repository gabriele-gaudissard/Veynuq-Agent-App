"""Static Python navigation; project source is never imported or executed."""
import ast
from pathlib import Path


def analyze(path, workspace, action, line=1, column=0):
    import jedi
    path=Path(path)
    if path.suffix.lower() not in {'.py','.pyi'}:raise ValueError('Built-in code intelligence supports Python. Use a trusted LSP plugin for other languages.')
    if path.stat().st_size>500000:raise ValueError('Code intelligence requires a file under 500 KB.')
    code=path.read_text(encoding='utf-8-sig')
    if action=='diagnostics':
        try:ast.parse(code);return {'diagnostics':[],'scope':'Python syntax only; not a type checker.'}
        except SyntaxError as error:return {'diagnostics':[{'line':error.lineno,'column':error.offset,'message':error.msg}]}
    lines=code.splitlines() or ['']
    if not 1<=line<=len(lines) or not 0<=column<=len(lines[line-1]):raise ValueError('Use an existing 1-based line and 0-based column.')
    # Explicit empty sys_path avoids inspecting installed native extensions or unrelated repositories.
    project=jedi.Project(str(workspace),sys_path=[],smart_sys_path=False,load_unsafe_extensions=False)
    script=jedi.Script(code,path=str(path),project=project)
    if action=='symbols':items=script.get_names(all_scopes=True,definitions=True,references=False)
    elif action=='definition':items=script.goto(line,column,follow_imports=True)
    elif action=='references':items=script.get_references(line,column,scope='file')
    elif action=='completion':items=script.complete(line,column)
    elif action=='signature':items=script.get_signatures(line,column)
    else:raise ValueError('Unknown code intelligence action.')
    return {'items':[{'name':item.name,'type':item.type,'description':item.description[:1000],
        'path':str(item.module_path) if item.module_path else None,'line':item.line,'column':item.column} for item in items[:100]],
        'untrusted':True,'scope':'Static Python analysis; references are limited to this file.'}
