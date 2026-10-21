"""Owner-only closure of a reboot-interrupted post-commit job, not a restart.

The uncertain native invocation and its automation latch remain unchanged.
This command never reapplies assets, resends commit, starts a game, restores
Saved, clears a fault, or issues an approval/transition for another launch.
"""
import argparse
import os
from pathlib import Path
import re
import uuid

from . import ark_config as c, ark_shutdown as n
from .ark_control import exclusive_lock
from .ark_process import CONFIG, LAUNCH, STATE
from .compose_engine import ComposeEngine
from .compose_update import ACTIVE, TERMINAL, persist
from .compose_server import PENDING, rootless


def validate_interruption(record, history, application, runtime, state, launch, server, boot):
    job=record.get('job')
    if (not isinstance(job,str) or not re.fullmatch('[0-9a-f]{32}',job)
            or record.get('phase') in TERMINAL or record.get('commit_sent') is not True
            or history.get('job')!=job or history.get('phase')!='committed'
            or application.get('phase')!='applied' or application.get('saved_restored') is not False
            or application.get('runtime')!=runtime):
        raise ValueError('A bound, applied and locally committed interrupted job is required.')
    invocation=state.get('invocation_id')
    if (not isinstance(invocation,str) or not re.fullmatch('[0-9a-f]{32}',invocation)
            or invocation!=launch.get('invocation_id') or launch.get('runtime')!=runtime
            or invocation==history.get('previous_state',{}).get('InvocationID')
            or state.get('phase') not in ('starting','running') or state.get('supervisor_returncode') is not None
            or not state.get('boot_id') or state['boot_id']==boot or state['boot_id']!=launch.get('boot_id')):
        raise ValueError('No matching previous-boot uncertain invocation; do not clear or replay it.')
    actual=server.get('State',{})
    if (server.get('Id')!=record.get('replacement_server') or server.get('Image')!=record.get('images',{}).get('server')
            or actual.get('Status')!='exited' or actual.get('Running') is not False
            or type(actual.get('Pid')) is not int or actual['Pid']!=0):
        raise ValueError('The exact replacement controller must be genuinely stopped.')


def seal(root, engine, job):
    root=Path(root)
    if not isinstance(job,str) or not re.fullmatch('[0-9a-f]{32}',job):raise ValueError('Exact job ID is required.')
    # Match the updater's kernel lock; a closure cannot race a still-live job.
    with exclusive_lock(root/'logs/updates/updater.lock'),c.server_lock(root):
        record=n.private_json(root/ACTIVE)
        if record.get('job')!=job or (root/PENDING).exists():raise ValueError('Job or pending state changed.')
        history=n.private_json(root/'logs/updates/history'/(job+'.json'))
        application=n.private_json(root/'backups'/('update-'+job)/'application.json')
        runtime=n.private_json(root/CONFIG);state=n.private_json(root/STATE);launch=n.private_json(root/LAUNCH)
        server=engine.current('server');engine.validate(server,'server')
        validate_interruption(record,history,application,runtime,state,launch,server,n.boot_id())
        running=engine.call('container','ls','--filter','label=dev.ark.job='+job,
            '--filter','label=dev.ark.purpose','--format','{{.ID}}').strip()
        if running:raise ValueError('A same-job helper is still running; closure is forbidden.')
        attempt=uuid.uuid4().hex
        folder=root/'backups'/('update-'+job)/'owner-closure'/attempt
        files=n.capture(root/n.SAVED,folder/'current')
        # A stable, exclusive snapshot preserves CURRENT bytes, never an older
        # after-snapshot. Actual invocation state/latches are not written.
        from .ark_docker import verify_files
        verify_files(root,files)
        n.save(folder/'before.json',{'record':record,'history':history,'application':application,
            'runtime':runtime,'process':state,'launch':launch,'controller':server,'current_boot':n.boot_id()})
        decision={'schema':1,'job':job,'attempt':attempt,'outcome':'closed_interrupted_after_commit',
            'explicit_owner_confirmation':True,'commit_received_locally':True,'native_exit_qualified':False,
            'application_replayed':False,'commit_replayed':False,'saved_restored':False,
            'new_start_authorized':False,'automation_latch_preserved':True,
            'current_saved_snapshot':str((folder/'current').relative_to(root))}
        n.save(folder/'decision.json',decision)
        record=record|{'phase':'owner_closed_interrupted','original_phase':record['phase'],'closure':decision}
        persist(root,record)
        return decision


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('seal-interrupted',));parser.add_argument('job')
    parser.add_argument('--confirm',action='store_true');args=parser.parse_args()
    if not args.confirm:raise ValueError('Explicit owner confirmation is required.')
    os.umask(0o077);rootless()
    result=seal(n.CODE_ROOT,ComposeEngine(os.environ['ARK_PROJECT']),args.job)
    import json
    print(json.dumps(result))


if __name__=='__main__':
    try:main()
    except Exception:
        import sys
        print('Owner closure refused; original evidence and holds retained.',file=sys.stderr)
        raise SystemExit(78)
