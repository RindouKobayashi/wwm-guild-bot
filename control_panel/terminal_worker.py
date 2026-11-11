"""Dedicated local PTY process. Stdin control frames; stdout terminal frames only."""
import base64
import json
import os
import signal
import sys
import threading

def main():
    # Import no bot settings; terminal environment excludes panel-injected secrets.
    names=('PATH','SystemRoot','WINDIR','COMSPEC','PATHEXT','HOME','USERPROFILE','USERNAME','USER','LOGNAME','LANG','LC_ALL','TEMP','TMP','APPDATA','LOCALAPPDATA')
    env={k:os.environ[k] for k in names if k in os.environ};env.update(TERM='xterm-256color',COLORTERM='truecolor')
    output=sys.stdout.buffer;guard=threading.Lock()
    def send(data):
        with guard:output.write(json.dumps({'data':base64.b64encode(data).decode()}).encode()+b'\n');output.flush()
    if os.name=='nt':
        import shutil
        from winpty import PtyProcess
        shell=shutil.which('pwsh.exe') or shutil.which('powershell.exe')
        process=PtyProcess.spawn([shell,'-NoLogo'],cwd=os.getcwd(),env=env,dimensions=(24,80))
        def read():
            try:
                while process.isalive():send(process.read(16384).encode('utf-8'))
            except (EOFError,OSError):pass
            finally:
                with guard:output.write(b'{"exit":true}\n');output.flush()
        threading.Thread(target=read,daemon=True).start()
        try:
            for line in sys.stdin.buffer:
                message=json.loads(line)
                if 'data' in message:process.write(base64.b64decode(message['data']).decode('utf-8','replace'))
                elif 'resize' in message:process.setwinsize(*message['resize'])
                else:break
        finally:
            import psutil
            try:
                host=psutil.Process(process.pid)
                for child in reversed(host.children(recursive=True)):
                    try:child.terminate()
                    except psutil.Error:pass
                process.close(force=True)
            except (psutil.Error,OSError):pass
    else:
        # fork occurs here, in a dedicated single-threaded worker, never in aiohttp.
        import pty,fcntl,termios,struct
        pid,fd=pty.fork()
        if pid==0:os.execve('/bin/bash',['bash','-i'],env)
        def read():
            try:
                while True:
                    data=os.read(fd,16384)
                    if not data:break
                    send(data)
            except OSError:pass
            finally:
                with guard:output.write(b'{"exit":true}\n');output.flush()
        threading.Thread(target=read,daemon=True).start()
        try:
            for line in sys.stdin.buffer:
                message=json.loads(line)
                if 'data' in message:os.write(fd,base64.b64decode(message['data']))
                elif 'resize' in message:fcntl.ioctl(fd,termios.TIOCSWINSZ,struct.pack('HHHH',*message['resize'],0,0))
                else:break
        finally:
            import time
            try:
                foreground=os.tcgetpgrp(fd)
                if foreground!=pid:os.killpg(foreground,signal.SIGHUP)
            except OSError:pass
            for sig in (signal.SIGHUP,signal.SIGTERM,signal.SIGKILL):
                try:os.killpg(pid,sig)
                except ProcessLookupError:break
                time.sleep(.2)
            os.close(fd)

if __name__=='__main__':main()
