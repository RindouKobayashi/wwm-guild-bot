"""Versioned, read-only local/SSH file protocol. No bot imports or arbitrary paths."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

PROTOCOL=1
PAGE=256*1024
CONTEXT=65536
ROOT=Path(__file__).resolve().parents[1]
SECRET=re.compile(r'token|secret|password|cookie|authorization|credential|session|ticket|login|uid',re.I)

class FileError(ValueError):pass

class Reader:
    def __init__(self,root=ROOT,environment='test'):
        self.root=Path(root).resolve();self.environment=environment
        if environment not in ('test','production'):raise FileError('Invalid environment.')
        self.secrets=[]
        values=dict(os.environ)
        try:
            from dotenv import dotenv_values
            values.update(dotenv_values(self.root/'.env'))
            values.update(dotenv_values(self.root/'activity'/('.env.'+environment)))
        except ImportError:pass
        for key,value in values.items():
            if SECRET.search(key) and value and 6<=len(str(value).encode())<=CONTEXT:self.secrets.append(str(value).encode())

    def registry(self):
        paths=[]
        directory=self.root/'activity/logs'/self.environment
        for name in ('bot','activity','tunnel','launcher'):
            for suffix in ('','.1','.2','.3'):
                paths.append((directory/(name+'.log'+suffix),'log',self.environment))
        for suffix in ('','.1','.2','.3'):
            paths.append((self.root/('logs/infos.log'+suffix),'log','Host/shared'))
        for relative in ('requirements.txt','requirements-panel.txt','control_panel/profiles.example.json','assets/wwm/current.json','assets/wwm/current/manifest.json'):
            paths.append((self.root/relative,'file',self.environment))
        backup=self.root/'control_panel/data'/self.environment/'backups'
        if backup.exists():paths.extend((p,'file',self.environment) for p in sorted(backup.glob('*.manifest.json'))[:7])
        items={}
        for path,kind,label in paths:
            relative=path.relative_to(self.root).as_posix()
            key=hashlib.sha256(relative.encode()).hexdigest()[:24]
            try:
                with self.open_regular(relative) as stream:
                    info=os.fstat(stream.fileno())
                items[key]=dict(id=key,name=relative,kind=kind,environment=label,size=info.st_size,modified=info.st_mtime,version=f'{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_size}')
            except (OSError,FileError):continue
        return items

    def open_regular(self,relative):
        """No-follow traversal using directory handles on Unix; checked handle on Windows."""
        parts=Path(relative).parts
        if not parts or Path(relative).is_absolute() or any(p in ('..','.') for p in parts):raise FileError('Unapproved path.')
        if os.name!='nt':
            directory=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
            try:
                for part in parts[:-1]:
                    next_fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
                    os.close(directory);directory=next_fd
                fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            finally:os.close(directory)
            if not stat.S_ISREG(os.fstat(fd).st_mode):os.close(fd);raise FileError('Only regular files are available.')
            return os.fdopen(fd,'rb')
        # OPEN_REPARSE_POINT prevents following the final component. Verify the
        # canonical path of the opened handle as well as every parent component.
        import ctypes,msvcrt
        from ctypes import wintypes
        path=self.root.joinpath(*parts)
        for parent in [path,*path.parents]:
            if parent==self.root:break
            try:attributes=parent.lstat().st_file_attributes
            except (OSError,AttributeError):attributes=0
            if parent.is_symlink() or attributes&1024:raise FileError('Links are unavailable.')
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        create=kernel.CreateFileW;create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];create.restype=wintypes.HANDLE
        handle=create(str(path),0x80000000,7,None,3,0x00200000,None)
        if handle==wintypes.HANDLE(-1).value:raise OSError('File unavailable.')
        getpath=kernel.GetFinalPathNameByHandleW;getpath.argtypes=[wintypes.HANDLE,wintypes.LPWSTR,wintypes.DWORD,wintypes.DWORD];getpath.restype=wintypes.DWORD
        close=kernel.CloseHandle;close.argtypes=[wintypes.HANDLE]
        buffer=ctypes.create_unicode_buffer(32768);length=getpath(handle,buffer,len(buffer),0)
        final=buffer.value.removeprefix('\\\\?\\')
        if not length or not Path(final).resolve().is_relative_to(self.root):close(handle);raise FileError('File is outside approved roots.')
        fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
        if not stat.S_ISREG(os.fstat(fd).st_mode):os.close(fd);raise FileError('Only regular files are available.')
        return os.fdopen(fd,'rb')

    def mask(self,data):
        # Length-preserving masks keep byte cursors stable across redaction.
        for secret in sorted(self.secrets,key=len,reverse=True):data=data.replace(secret,b'*'*len(secret))
        patterns=[rb'(?i)(bearer\s+)([^\s\r\n]{1,65536})',rb'''(?i)(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{0,64}(?:token|secret|password|cookie|credential|ticket|authorization)[A-Za-z0-9_-]{0,64}[\s"':=]{1,32})([^\r\n,;}"']{1,65536})''',rb'(-----BEGIN [^-\r\n]*PRIVATE KEY-----)([\s\S]{1,65536}?-----END [^-\r\n]*PRIVATE KEY-----)']
        for pattern in patterns:
            data=re.sub(pattern,lambda m:m[1]+b'*'*len(m[2]),data)
        return data

    def read(self,key,offset=None,limit=PAGE,raw=False,version=None,tail=False):
        item=self.registry().get(key)
        if not item:raise FileError('This registered file is missing, unreadable, or no longer retained.')
        limit=min(PAGE,max(1,int(limit)));size=item['size']
        offset=max(0,size-limit) if offset is None or tail else max(0,int(offset))
        notice=None
        if version and version.split(':')[:2]!=item['version'].split(':')[:2]:notice='File rotated. Showing the new file.';offset=0
        elif offset>size:notice='File was truncated. Reading from the start.';offset=0
        with self.open_regular(item['name']) as stream:
            start=max(0,offset-CONTEXT);stream.seek(start)
            data=stream.read(limit+2*CONTEXT)
            if not raw:data=self.mask(data)
            data=data[offset-start:offset-start+limit]
        text=data.decode('utf-8','replace')
        # The viewer uses escaped text, never terminal rendering.
        text=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',text)
        text=''.join(c if c in '\n\r\t' or ord(c)>=32 else '�' for c in text)
        return dict(file=item,text=text,offset=offset,next_offset=offset+len(data),previous_offset=max(0,offset-limit),eof=offset+len(data)>=size,notice=notice,raw=raw,fragment=offset>0,protocol=PROTOCOL)

    def search(self,keys,query,raw=False):
        if not isinstance(query,str) or not 1<=len(query)<=200:raise FileError('Search text must contain 1–200 characters.')
        if not isinstance(keys,list) or not 1<=len(keys)<=20:raise FileError('Choose 1–20 files.')
        matches=[];scanned=0;deadline=time.monotonic()+2.5;complete=True
        for key in keys:
            offset=0;line=1;carry=''
            while True:
                if scanned>=50*1024*1024 or time.monotonic()>deadline or len(matches)>=500:complete=False;break
                page=self.read(key,offset,raw=raw);scanned+=page['next_offset']-offset
                chunks=(carry+page['text']).split('\n');carry=chunks.pop()
                if page['eof']:chunks.append(carry);carry=''
                for text in chunks:
                    if query.casefold() in text.casefold():matches.append(dict(file_id=key,line=line,text=text[:1000],offset=offset))
                    line+=1
                    if len(matches)>=500:complete=False;break
                if page['eof'] or not complete:break
                offset=page['next_offset']
                if len(carry)>512*1024:carry=carry[-512*1024:]
            if not complete:break
        return dict(matches=matches[:500],scanned_bytes=scanned,complete=complete,limit_bytes=50*1024*1024)

    def dispatch(self,request):
        if request.get('protocol')!=PROTOCOL:raise FileError('File reader version differs. Deploy the matching release on the target.')
        action=request.get('action')
        if action=='list':return dict(protocol=PROTOCOL,items=list(self.registry().values()))
        if action=='read':return self.read(request['id'],request.get('offset'),request.get('limit',PAGE),bool(request.get('raw')),request.get('version'),request.get('tail',False))
        if action=='bytes':
            import base64
            item=self.registry().get(request.get('id'))
            if not item:raise FileError('Registered file unavailable.')
            if request.get('version') and request['version'].split(':')[:2]!=item['version'].split(':')[:2]:raise FileError('File rotated during download. Download the retained rotation instead.')
            offset=max(0,int(request.get('offset',0)));start=max(0,offset-CONTEXT)
            with self.open_regular(item['name']) as stream:
                stream.seek(start);data=stream.read(PAGE+2*CONTEXT)
            if not request.get('raw'):data=self.mask(data)
            data=data[offset-start:offset-start+PAGE]
            return dict(data=base64.b64encode(data).decode(),eof=offset+len(data)>=item['size'],protocol=PROTOCOL)
        if action=='search':return self.search(request.get('ids'),request.get('query'),False)
        raise FileError('Unknown file operation.')

def main():
    environment=sys.argv[sys.argv.index('--environment')+1] if '--environment' in sys.argv else 'test'
    try:
        line=sys.stdin.buffer.readline(65537)
        if len(line)>65536:raise FileError('File request exceeds its limit.')
        result=Reader(environment=environment).dispatch(json.loads(line))
    except Exception as exc:
        result={'error':str(exc) if isinstance(exc,FileError) else 'Registered file could not be read.','protocol':PROTOCOL}
    sys.stdout.write(json.dumps(result,ensure_ascii=True,allow_nan=False)+'\n');sys.stdout.flush()

if __name__=='__main__':main()
