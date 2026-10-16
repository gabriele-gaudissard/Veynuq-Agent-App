"""Bounded static binary inspection: no executable is ever launched."""
import hashlib
import re
import struct


def inspect(target, mode, cancel):
    size=target.stat().st_size
    if size>200_000_000:raise ValueError('Binary inspection limit is 200 MB.')
    digest=hashlib.sha256()
    with target.open('rb') as file:
        sample=file.read(5_000_000);digest.update(sample)
        while block:=file.read(1_000_000):
            if cancel.is_set():raise RuntimeError('Binary inspection stopped.')
            digest.update(block)
    result={'path':str(target),'bytes':size,'sha256':digest.hexdigest(),'format':'binary','executed':False}
    if sample.startswith(b'MZ'):
        if len(sample)<64:raise ValueError('Truncated DOS/PE header.')
        offset=struct.unpack_from('<I',sample,60)[0]
        if offset>len(sample)-24 or sample[offset:offset+4]!=b'PE\0\0':raise ValueError('DOS signature present but no valid PE header in the inspected prefix.')
        machine,sections=struct.unpack_from('<HH',sample,offset+4)
        optional_size=struct.unpack_from('<H',sample,offset+20)[0]
        optional=offset+24;table=optional+optional_size
        if sections>96 or optional_size<20 or table+sections*40>len(sample):raise ValueError('Invalid/truncated PE section table.')
        magic=struct.unpack_from('<H',sample,optional)[0]
        bits={0x10b:32,0x20b:64}.get(magic)
        if bits is None:raise ValueError('Unsupported PE optional header.')
        result.update(format='PE',architecture={0x14c:'x86',0x8664:'x64',0xaa64:'ARM64'}.get(machine,hex(machine)),bits=bits,
                      entry_point_rva=hex(struct.unpack_from('<I',sample,optional+16)[0]),clr_header_present=False,sections=[])
        directory=96 if bits==32 else 112
        if optional_size>=directory+15*8:
            rva,length=struct.unpack_from('<II',sample,optional+directory+14*8)
            result['clr_header_present']=bool(rva and length)
        for i in range(sections):
            section=table+i*40
            name=sample[section:section+8].split(b'\0')[0].decode('ascii',errors='replace')
            virtual_size,rva,raw_size=struct.unpack_from('<III',sample,section+8)
            result['sections'].append({'name':name,'rva':hex(rva),'virtual_bytes':virtual_size,'stored_bytes':raw_size})
    elif sample.startswith(b'\x7fELF') and len(sample)>=20:
        if sample[4] not in (1,2) or sample[5] not in (1,2):raise ValueError('Invalid ELF header.')
        result.update(format='ELF',bits=32 if sample[4]==1 else 64,machine=struct.unpack_from('<H' if sample[5]==1 else '>H',sample,18)[0])
    if mode=='strings':
        values=[]
        for pattern,encoding in ((rb'[\x20-\x7e]{5,200}','ascii'),(rb'(?:[\x20-\x7e]\x00){5,200}','utf-16-le')):
            for match in re.finditer(pattern,sample):
                values.append({'offset':match.start(),'text':match.group().decode(encoding)})
                if len(values)>=200:break
            if len(values)>=200:break
        result.update(strings=values,sampled_bytes=len(sample),strings_limited=True)
    result['note']='Static metadata/strings only. This is not original source code. Use official decompilers or disassemblers for deeper analysis; acquire missing tools with tool_setup or plugin_marketplace. Native decompilation is approximate; .NET may retain more structure.'
    return result
