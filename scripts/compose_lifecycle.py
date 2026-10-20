"""Durable coordination between Compose shutdown and updater replacement."""
from pathlib import Path
import re
import socket
import time
from . import ark_shutdown as n

PLATFORM=Path('logs/updates/platform-shutdown')
READY=Path('logs/updates/updater-ready.json')
POST_REPLACEMENT={'new_controller_started','commit_requested','committed','handoff','handoff_replacing','completed'}


def mark_shutdown(root,job):
    if not isinstance(job,str) or not re.fullmatch('[0-9a-f]{32}',job):raise ValueError('Invalid lifecycle job.')
    path=Path(root)/PLATFORM/(job+'.json')
    if path.exists():return
    n.save(path,{'schema':1,'job':job,'boot_id':n.boot_id(),'monotonic':time.monotonic()})


def stopped(root,job,since=None):
    from .compose_platform import STATE,state
    if STATE.exists():
        try:
            if state()['status']=='closing':return True
        except (OSError,ValueError):return True
    path=Path(root)/PLATFORM/(job+'.json')
    if not path.exists():return False
    value=n.private_json(path)
    if (type(value.get('schema')) is not int or value['schema']!=1 or value.get('job')!=job
            or type(value.get('monotonic')) not in (int,float)):
        raise ValueError('Invalid lifecycle evidence.')
    if since is None:return True
    return value.get('boot_id')==n.boot_id() and value['monotonic']>=since


def controller_stopping(root):
    path=Path(root)/'logs/updates/current.json'
    if not path.exists():return
    value=n.private_json(path)
    identity=value.get('replacement_server','')
    if (value.get('phase') in POST_REPLACEMENT and isinstance(identity,str)
            and re.fullmatch('[0-9a-f]{64}',identity) and identity.startswith(socket.gethostname())):
        mark_shutdown(root,value['job'])
