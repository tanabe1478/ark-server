"""Stable, socket-free Compose shutdown witness; never replaced by the updater.

A dedicated volume is writable only here. Replacements see it read-only. This
closes the `compose down` snapshot race even if every old service ID disappeared.
"""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import time
import uuid

DIRECTORY=Path('/run/ark-platform')
STATE=DIRECTORY/'state.json'
EVIDENCE=Path('/opt/ark/logs/process')


def boot():return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def write(path,value):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    temp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(json.dumps(value,separators=(',',':')).encode());stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if temp.exists():temp.unlink()


def read(path=None):
    path=STATE if path is None else path
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:raise ValueError('Unsafe platform witness.')
        raw=stream.read(4097)
    if len(raw)>4096:raise ValueError('Oversize platform witness.')
    return json.loads(raw)


def state(generation=None):
    value=read()
    if (set(value)!={'schema','generation','boot_id','status','heartbeat'} or type(value['schema']) is not int or value['schema']!=1
            or not isinstance(value['generation'],str) or not re.fullmatch('[0-9a-f]{32}',value['generation'])
            or value['boot_id']!=boot() or value['status'] not in ('active','closing')
            or type(value['heartbeat']) not in (int,float) or not math.isfinite(value['heartbeat'])
            or generation is not None and value['generation']!=generation):raise ValueError('Platform generation is unknown.')
    if value['status']=='active' and not 0<=time.monotonic()-value['heartbeat']<10:raise ValueError('Platform witness is stale.')
    return value


def active(generation=None):
    value=state(generation)
    if value['status']!='active':raise ValueError('Platform is closing.')
    return value['generation']


def drained(generation):
    try:
        health=read(EVIDENCE/'broker-health.json');done=read(EVIDENCE/'platform-drained.json')
    except (OSError,ValueError):return False
    return (health.get('platform_generation')==generation and done.get('platform_generation')==generation
            and health.get('hostname')==done.get('hostname') and isinstance(health.get('broker_run_id'),str)
            and re.fullmatch('[0-9a-f]{32}',health['broker_run_id']) is not None
            and health['broker_run_id']==done.get('broker_run_id') and health.get('closing') is True and done.get('stopped') is True)


def serve():
    DIRECTORY.mkdir(mode=0o700,parents=True,exist_ok=True);DIRECTORY.chmod(0o700)
    with (DIRECTORY/'writer.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        generation=uuid.uuid4().hex;closing=False
        def stop(number,frame):
            nonlocal closing
            closing=True
        for number in (signal.SIGINT,signal.SIGTERM):signal.signal(number,stop)
        def save(status):write(STATE,{'schema':1,'generation':generation,'boot_id':boot(),'status':status,'heartbeat':time.monotonic()})
        while not closing:save('active');time.sleep(.25)
        save('closing')
        # No engine API. The stable marker makes new controllers refuse launch,
        # and the current controller writes this only AFTER actual guarded stop.
        deadline=time.monotonic()+1400
        while time.monotonic()<deadline:
            if drained(generation):return 0 if read(EVIDENCE/'platform-drained.json').get('startable_verified') is True else 70
            try:
                health=read(EVIDENCE/'broker-health.json')
                if health.get('platform_generation')!=generation:return 0 # no controller admitted in this generation
            except FileNotFoundError:return 0
            time.sleep(.25)
        return 70


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('serve','health'));args=parser.parse_args();os.umask(0o077)
    if args.action=='serve':return serve()
    active();return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:raise SystemExit(70)
