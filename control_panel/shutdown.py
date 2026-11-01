"""Cooperative shutdown over the parent's owned stdin pipe, including Windows."""
import json
import os
import sys
import threading

def install_shutdown(loop, callback):
    instance = os.environ.get('WWM_PANEL_PROCESS_ID')
    if not instance:
        return
    def read():
        try:
            for line in sys.stdin:
                message = json.loads(line)
                if message == {'action':'shutdown', 'instance':instance}:
                    loop.call_soon_threadsafe(callback)
                    return
        except (OSError, ValueError, RuntimeError):
            return
    threading.Thread(target=read, daemon=True, name='panel-shutdown').start()
