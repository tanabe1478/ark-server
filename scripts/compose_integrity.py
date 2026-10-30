"""Preserve every pre-start INI entry, including duplicate Unreal arrays.

Added defaults and equivalent representations of the seven managed numeric
settings (including omission of their documented 1.0 defaults) are allowed.
All other entries, MOD order, credentials and duplicate arrays must remain.
Full shutdown configuration hashes stay strict, without semantic filtering.
"""
from collections import Counter
from pathlib import Path

from . import ark_config as c, ark_shutdown as n


def entries(path):
    if not path.exists():return Counter()
    raw=path.read_bytes()
    if len(raw)>8*1024**2:raise ValueError('INI exceeds the supported size.')
    text=raw.decode('utf-16') if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else raw.decode('utf-8-sig')
    section=''
    result=Counter()
    for line in text.splitlines():
        line=line.strip()
        if not line or line.startswith((';','#')):continue
        if line.startswith('[') and line.endswith(']'):
            section=line[1:-1].casefold();continue
        if '=' in line:
            key,value=line.split('=',1)
            result[(section,key.strip().casefold(),value.strip())]+=1
    return result


def unmanaged(values, filename):
    managed={(rule['section'].casefold(),rule['ini_key'].casefold()) for rule in (*c.RULES.values(),*c.TOGGLES.values()) if rule['file']==filename}
    return Counter({item:count for item,count in values.items() if item[:2] not in managed})


def capture(root):
    numeric=c.load_editable_settings(root)
    return {'entries':{str(path):unmanaged(entries(root/path),path.name) for path in (c.INI_PATH,c.GAME_INI_PATH)},
            'numeric':{name:numeric[name] for name in (*c.RULES,*c.TOGGLES)}}


def progress(root):
    files=n.inventory(root/n.SAVED)
    world='SavedArks/'+c.load_settings(root)['map']+'.ark'
    size=files.get(world,{}).get('bytes')
    if size is not None and size<4096:raise ValueError('Existing world is empty or implausibly small.')
    return {'world':world,'world_bytes':size,
            'roster':frozenset(name for name in files if name.endswith(('.arkprofile','.arktribe')))}


def verify_progress(root, required):
    files=n.inventory(root/n.SAVED)
    size=required['world_bytes']
    if size is not None and files.get(required['world'],{}).get('bytes',0)*10<size*9:
        raise ValueError('World shrank substantially; public startup or promotion is blocked.')
    if (not required['roster']<=files.keys() or any(files[name]['bytes']==0 for name in required['roster'])):
        raise ValueError('A pre-start profile or tribe disappeared or became empty.')


def verify(root, required):
    for path,values in required['entries'].items():
        if values-unmanaged(entries(root/path),Path(path).name):
            raise ValueError('Game removed or changed existing INI entries. Public readiness is blocked.')
    numeric=c.load_editable_settings(root)
    if any(numeric[name]!=value for name,value in required['numeric'].items()):
        raise ValueError('Game changed a managed numeric setting. Public readiness is blocked.')
    text=c.decode_ini((root/c.INI_PATH).read_bytes())[0]
    c.validate_ini_initialization(text)
    c.validate_ini_credentials(text)
    c.launch_url(root)
