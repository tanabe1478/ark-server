"""Two real independent Saved-copy save/stop/reload cycles. Never production."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from . import ark_shutdown as n
from .ark_process import ProcessBackend, bootstrap, build_identity, contract
from .compose_assets import tree_manifest


def digest_value(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def qualify(root=n.CODE_ROOT, result=Path('/result/qualification.json')):
    from .compose_server import rootless
    rootless()
    # No process, controller state, live evidence or stop receipt is imported.
    source = {key: {'bytes': meta['bytes'], 'sha256': n.digest(root / n.SAVED / key)}
              for key, meta in n.inventory(root / n.SAVED).items()}
    # Prune before walking/hashing: Saved is separately captured and qualified.
    assets = tree_manifest(root / 'server', exclude_saved=True)
    from .compose_integrity import progress,verify_progress
    original_progress=progress(root)
    input_configuration=n.configuration(root)
    runtime = bootstrap(root)
    backend = ProcessBackend(root)
    cycles = []
    for index in range(2):
        backend._launch(private=True)
        backend._ready()
        stopped = backend.shutdown_guard().stop(backend)
        verified = backend.shutdown_guard().verify_startable(backend.state())
        verify_progress(root,original_progress)
        if not stopped['changed'] or verified.get('warning') != 'expected_native_abort':
            raise n.ShutdownError('Candidate did not complete a qualified save/exit.')
        cycles.append({'state': backend.state(), 'receipt': n.private_json(root / n.DIRECTORY / 'last-stop.json'),
                       'clean_exit': False, 'warning': 'expected_native_abort'})
    value = {'schema': 1, 'two_cycles_passed': True, 'progress_shape_verified':True, 'runtime': runtime, 'native_policy': n.policy(root),
             'image_build': build_identity(), 'source_contract': contract(),
             'input_saved': source, 'input_saved_digest': digest_value(source),
             'input_configuration': input_configuration,
             'assets_digest': digest_value(assets), 'cycles': cycles,
             'in_game_content_verified': False}
    n.save(result, value)
    return value


def main():
    os.umask(0o077)
    try:
        qualify()
        print('Independent candidate completed two real save/exit/reload cycles; clean_exit=false; in-game correctness unverified.')
        return 0
    except Exception:
        # Do not kill/restart a timed-out candidate or clear its launch ledger.
        # Container/PDEATH termination remains an unqualified outcome.
        from .ark_process import STATE, LAUNCH
        state=n.private_json(n.CODE_ROOT/STATE) if (n.CODE_ROOT/STATE).exists() else None
        n.save(Path('/result/failure.json'), {'schema': 1, 'phase': 'qualification_failed',
            'binary_sha256': n.digest(n.CODE_ROOT / n.BINARY) if (n.CODE_ROOT / n.BINARY).is_file() else None,
            'last_process_state':state,'native_launch_intent_created':(n.CODE_ROOT/LAUNCH).exists(),
            'candidate_retry_performed': False})
        print('Candidate qualification failed; production must not be promoted.', file=sys.stderr)
        if (n.CODE_ROOT/LAUNCH).exists() and (state is None or state.get('phase')!='exited' or state.get('supervisor_returncode') is None):
            # Returning would kill the live worker through PDEATHSIG. A
            # readiness/configuration timeout is NOT permission to do that.
            # Retain the helper and launch ledger for explicit owner action;
            # the engine wait timeout reports unknown and does not replay it.
            print('Live or uncertain invocation held; no timeout-driven termination or retry.',file=sys.stderr)
            while True:time.sleep(1)
        return 78


if __name__ == '__main__':
    raise SystemExit(main())
