"""Isolated official SteamCMD jobs and ASE Workshop installation.

Only a staging directory is writable. This module never promotes assets or
mounts the production Saved, Discord token or Docker socket into SteamCMD.
"""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import urllib.parse
import urllib.request
import zlib

from . import ark_config as c, ark_shutdown as n

STEAM_URL = 'https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz'
MAX_DOWNLOAD = 32 * 1024**2
MAX_FILE = 8 * 1024**3
MAX_TREE = 256 * 1024**3


def fetch(url, limit=MAX_DOWNLOAD, data=None):
    request = urllib.request.Request(url, data=data, headers={'User-Agent': 'ARK-Compose-Updater/1'})
    with urllib.request.urlopen(request, timeout=60) as response:
        value = response.read(limit + 1)
    if len(value) > limit:
        raise ValueError('Download exceeds the supported size.')
    return value


def extract_tar(raw, destination, max_bytes=1024**3, max_files=10000):
    """No links/devices/traversal, bounded expanded size, no overwrites."""
    destination = Path(destination)
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:*') as archive:
        members = archive.getmembers()
        if len(members) > max_files or sum(m.size for m in members) > max_bytes:
            raise ValueError('Archive exceeds limits.')
        seen = set()
        for member in members:
            path = Path(member.name)
            if (path.is_absolute() or '..' in path.parts or '\\' in member.name
                    or not (member.isfile() or member.isdir()) or member.name in seen):
                raise ValueError('Unsafe archive member.')
            seen.add(member.name)
            target = destination / path
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open('xb') as output:
                shutil.copyfileobj(archive.extractfile(member), output, 1024**2)
            target.chmod(0o700 if member.mode & 0o111 else 0o600)


def bootstrap_steam(root):
    if (root / 'steamcmd.sh').is_file():
        return
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = fetch(STEAM_URL)
    extract_tar(raw, root, max_files=500, max_bytes=128 * 1024**2)
    n.save(root / 'bootstrap-source.json', {'url': STEAM_URL, 'sha256': hashlib.sha256(raw).hexdigest(),
                                          'publisher_checksums_not_independently_signed': True})


def vdf(text, key=None):
    """Bounded text VDF parser; never executes or interpolates input."""
    if len(text) > 8 * 1024**2:
        raise ValueError('VDF exceeds limit.')
    tokens = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text)
    values = [s.replace('\\"', '"').replace('\\\\', '\\') if not brace else brace for s, brace in tokens]
    if len(values) > 500000:
        raise ValueError('VDF exceeds limit.')
    start = values.index(key) + 1 if key is not None else 0
    if key is not None:
        if start >= len(values) or values[start] != '{':
            raise ValueError('Expected VDF object.')
        start += 1
    def object_at(index, depth=0, expect_close=False):
        if depth > 32:
            raise ValueError('VDF nesting exceeds limit.')
        result = {}
        while index < len(values) and values[index] != '}':
            name = values[index]
            index += 1
            if index >= len(values) or name in ('{', '}') or name in result:
                raise ValueError('Invalid VDF.')
            if values[index] == '{':
                value, index = object_at(index + 1, depth + 1, True)
            else:
                value = values[index]
                if value=='}':raise ValueError('Missing VDF value.')
                index += 1
            result[name] = value
        if expect_close and (index>=len(values) or values[index]!='}'):
            raise ValueError('Unterminated VDF object.')
        return result, index + 1 if index<len(values) and values[index]=='}' else index
    return object_at(start,expect_close=key is not None)[0]


def steam(root, commands, log):
    bootstrap_steam(root)
    if any('\n' in line or '\r' in line for line in commands):
        raise ValueError('Steam command contains line separators.')
    home = Path('/tmp/ark-steam-home')
    home.mkdir(mode=0o700, exist_ok=True)
    with log.open('wb') as output:
        result = subprocess.run([str(root / 'steamcmd.sh')], input=('\n'.join(commands) + '\nquit\n').encode(),
            cwd=root, env=os.environ | {'HOME': str(home)}, stdout=output, stderr=subprocess.STDOUT, timeout=7200)
    if result.returncode:
        raise ValueError('SteamCMD failed; inspect the private installer log.')
    return log.read_text(errors='replace')


def workshop_details(ids):
    if not ids:
        return {}
    if len(ids) > 32 or any(not re.fullmatch(c.MOD_ID, value) for value in ids) or len(set(ids)) != len(ids):
        raise ValueError('Invalid Workshop IDs.')
    form = {'itemcount': str(len(ids))} | {'publishedfileids[' + str(i) + ']': value for i, value in enumerate(ids)}
    raw = fetch('https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/',
                limit=2 * 1024**2, data=urllib.parse.urlencode(form).encode())
    response = json.loads(raw)['response']['publishedfiledetails']
    result = {}
    for item in response:
        identity = item['publishedfileid']
        stamp = item.get('time_updated')
        if (identity not in ids or identity in result or item.get('result') != 1
                or type(stamp) is not int or stamp <= 0 or item.get('consumer_app_id') != 346110):
            raise ValueError('Workshop metadata is unavailable or invalid.')
        result[identity] = {'time_updated': stamp, 'title': str(item['title'])[:256]}
    if set(result) != set(ids):
        raise ValueError('Workshop metadata is incomplete.')
    return result


def check(root, ids):
    text = steam(root, ['login anonymous', 'app_info_update 1', 'app_info_print 376030'], root / 'check.log')
    info = vdf(text, '376030')
    build = info['depots']['branches']['public']['buildid']
    if not isinstance(build, str) or re.fullmatch('[0-9]{1,20}', build) is None:
        raise ValueError('Steam build ID is unavailable.')
    return {'build_id': build, 'mods': workshop_details(ids)}


def tree_manifest(root, *, exclude_saved=False):
    root = Path(root)
    files = {}
    total = 0
    for parent, dirs, names in os.walk(root, followlinks=False):
        if exclude_saved and Path(parent) == root / 'ShooterGame':
            dirs[:] = [name for name in dirs if name != 'Saved']
            names = [name for name in names if name != 'Saved']
        for name in dirs + names:
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                if not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError('External asset link is not supported.')
                files[relative] = {'link': os.readlink(path)}
            elif path.is_file():
                size = path.stat().st_size
                total += size
                if size > MAX_FILE or total > MAX_TREE:
                    raise ValueError('Asset tree exceeds limits.')
                files[relative] = {'bytes': size, 'sha256': n.digest(path)}
            elif not path.is_dir():
                raise ValueError('Special asset entry is not supported.')
            if len(files) > 500000:
                raise ValueError('Too many asset entries.')
    return files


def asset_bytes(root):
    total=0
    for base,dirs,files in os.walk(root,followlinks=False):
        if Path(base).relative_to(root)==Path('ShooterGame') and 'Saved' in dirs:dirs.remove('Saved')
        for name in files:
            info=(Path(base)/name).lstat()
            if stat.S_ISREG(info.st_mode):total+=info.st_size
            elif not stat.S_ISLNK(info.st_mode):raise ValueError('Special asset file.')
    return total


def copy_assets(source, destination):
    source = Path(source)
    destination = Path(destination)
    if destination.exists():
        for parent, dirs, files in os.walk(destination, followlinks=False):
            if Path(parent) == destination / 'ShooterGame' and 'Saved' in dirs:
                dirs.remove('Saved')
            if files or any((Path(parent) / name).is_symlink() for name in dirs):
                raise ValueError('Candidate asset directory must not contain existing program files.')
    def ignore(path, names):
        return ['Saved'] if Path(path) == source / 'ShooterGame' and 'Saved' in names else []
    # Reject external links/special files before copy. Never hardlink production.
    for parent, dirs, files in os.walk(source, followlinks=False):
        if Path(parent) == source / 'ShooterGame' and 'Saved' in dirs:
            dirs.remove('Saved')
        for name in dirs + files:
            p = Path(parent) / name
            if p.is_symlink() and not p.resolve().is_relative_to(source.resolve()):
                raise ValueError('External asset link requires a separate safe import.')
            if not (p.is_dir() or p.is_file() or p.is_symlink()):
                raise ValueError('Unsafe asset entry.')
    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True, ignore=ignore)
    (destination / 'ShooterGame/Saved').mkdir(mode=0o700, parents=True, exist_ok=True)


def read_string(stream):
    raw = stream.read(4)
    if len(raw) != 4:
        raise ValueError('Truncated MOD string.')
    count, = struct.unpack('<i', raw)
    if not count or abs(count) > 4096:
        raise ValueError('Invalid MOD string length.')
    value = stream.read(abs(count) * (2 if count < 0 else 1))
    if len(value) != abs(count) * (2 if count < 0 else 1):
        raise ValueError('Truncated MOD string.')
    text = value.decode('utf-16-le' if count < 0 else 'utf-8')
    if not text.endswith('\0'):
        raise ValueError('Unterminated MOD string.')
    return text[:-1]


def string(value):
    if len(value) > 4095 or '\0' in value:
        raise ValueError('Invalid MOD string.')
    if value.isascii():
        raw = value.encode() + b'\0'
        return struct.pack('<i', len(raw)) + raw
    raw = value.encode('utf-16-le') + b'\0\0'
    return struct.pack('<i', -(len(raw) // 2)) + raw


def decompress(source, destination):
    with source.open('rb') as stream:
        header = stream.read(32)
        if len(header) != 32:
            raise ValueError('Truncated UE4 compressed header.')
        magic, block, compressed, expanded = struct.unpack('<QQQQ', header)
        if magic != 0x9E2A83C1 or not 1 <= block <= 16 * 1024**2 or expanded > MAX_FILE:
            raise ValueError('Unsupported UE4 compressed asset.')
        count = math.ceil(expanded / block)
        if count > 1000000:
            raise ValueError('Too many compressed chunks.')
        chunks = []
        for index in range(count):
            chunk = stream.read(16)
            if len(chunk) != 16:
                raise ValueError('Truncated chunk table.')
            a, b = struct.unpack('<QQ', chunk)
            if a > 32 * 1024**2 or b > block:
                raise ValueError('Invalid compressed chunk.')
            chunks.append((a, b))
        if sum(a for a, b in chunks) != compressed or sum(b for a, b in chunks) != expanded:
            raise ValueError('Compressed totals do not match.')
        with destination.open('xb') as output:
            for a, b in chunks:
                raw = stream.read(a)
                if len(raw) != a:
                    raise ValueError('Truncated compressed chunk.')
                decoder = zlib.decompressobj()
                value = decoder.decompress(raw, b + 1)
                if len(value) != b or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                    raise ValueError('Compressed chunk does not match its declared size.')
                output.write(value)
            if stream.read(1):
                raise ValueError('Unexpected compressed trailer.')
        destination.chmod(0o600)


def install_mod(cache, destination, identity, title):
    if not re.fullmatch(c.MOD_ID, identity):
        raise ValueError('Invalid MOD ID.')
    source = cache / identity
    # ASE's Linux dedicated server consumes the WindowsNoEditor data cook.
    # Some Workshop Linux cooks have incompatible material/static-mesh data:
    # native startup rejects them with LinkerLoad/Bad name index (SIGSEGV).
    branch=source/'WindowsNoEditor'
    if not branch.is_dir():raise ValueError('Supported ASE WindowsNoEditor data cook is missing.')
    info = next((p for p in (branch / 'mod.info', source / 'mod.info') if p.is_file()), None)
    meta = next((p for p in (branch / 'modmeta.info', source / 'modmeta.info') if p.is_file()), None)
    if info is None or meta is None or info.stat().st_size > 65536 or meta.stat().st_size > 1024**2:
        raise ValueError('Downloaded MOD metadata is missing or oversized.')
    stream = io.BytesIO(info.read_bytes())
    read_string(stream)  # Internal mod name; display name comes from Workshop.
    raw = stream.read(4)
    if len(raw) != 4:
        raise ValueError('MOD map list is missing.')
    count, = struct.unpack('<I', raw)
    if count > 64:
        raise ValueError('Too many MOD map entries.')
    maps = [read_string(stream) for _ in range(count)]
    target = destination / identity
    if target.exists():
        shutil.rmtree(target)  # Staging ONLY; never the live asset tree.
    target.mkdir(mode=0o700, parents=True)
    total = 0
    for parent, dirs, files in os.walk(branch, followlinks=False):
        for name in dirs + files:
            p = Path(parent) / name
            if p.is_symlink() or not (p.is_file() or p.is_dir()):
                raise ValueError('Unsafe Workshop entry.')
        relative = Path(parent).relative_to(branch)
        out = target / relative
        out.mkdir(mode=0o700, exist_ok=True)
        for name in files:
            p = Path(parent) / name
            if name.endswith('.z.uncompressed_size'):
                continue
            q = out / (name[:-2] if name.endswith('.z') else name)
            if name.endswith('.z'):
                decompress(p, q)
            else:
                if q.exists():
                    raise ValueError('Duplicate Workshop output.')
                shutil.copy2(p, q)
            total += q.stat().st_size
            if total > MAX_TREE:
                raise ValueError('MOD exceeds size limit.')
    for source_file in (info, meta):
        q = target / source_file.name
        if not q.exists():
            shutil.copy2(source_file, q)
    header = (struct.pack('<Q', int(identity)) + string(title)
              + string('../../../ShooterGame/Content/Mods/' + identity)
              + struct.pack('<I', len(maps)) + b''.join(string(x) for x in maps)
              + struct.pack('<IIB', 0xFF22FF33, 2, 1) + meta.read_bytes())
    c.atomic_write(destination / (identity + '.mod'), header)


def prepare_sdk(server, steam_root):
    candidates=(server/'linux64/steamclient.so',server/'ShooterGame/Binaries/Linux/steamclient.so',steam_root/'linux64/steamclient.so')
    def elf64(path):
        if not path.is_file() or path.is_symlink():return False
        with path.open('rb') as stream:return stream.read(5)==b'\x7fELF\x02'
    source=next((path for path in candidates if elf64(path)),None)
    target=server/'.runtime-sdk/steamclient.so'
    if source is None:
        if elf64(target):return
        raise ValueError('Official 64-bit Steam SDK is missing.')
    target.parent.mkdir(mode=0o700,exist_ok=True)
    shutil.copy2(source,target)


def download(stage, steam_root, plan, source=None):
    if set(plan) != {'schema', 'game', 'mods', 'versions'} or type(plan['schema']) is not int or plan['schema'] != 1 or type(plan['game']) is not bool:
        raise ValueError('Invalid installer plan.')
    ids = plan['mods']
    if not isinstance(ids, list) or len(ids) > 32 or any(not isinstance(x, str) or not re.fullmatch(c.MOD_ID, x) for x in ids):
        raise ValueError('Invalid Workshop list.')
    server = stage / 'server'
    server.mkdir(mode=0o700, parents=True, exist_ok=True)
    if source is not None:
        copy_assets(source, server)
    commands = ['force_install_dir ' + str(server), 'login anonymous']
    if plan['game']:
        commands.append('app_update 376030 validate')
    commands += ['workshop_download_item 346110 ' + identity + ' validate' for identity in ids]
    output=steam(steam_root, commands, stage / 'steam-install.log')
    if not (server / 'ShooterGame/Binaries/Linux/ShooterGameServer').is_file():
        raise ValueError('Server download is incomplete.')
    if plan['game']:
        manifest = vdf((server / 'steamapps/appmanifest_376030.acf').read_text())['AppState']
        if (manifest.get('buildid') != plan['versions']['build_id'] or manifest.get('appid')!='376030'
                or manifest.get('StateFlags')!='4'):
            raise ValueError('Downloaded game version differs from the update plan or is incomplete.')
    cache = server / 'steamapps/workshop/content/346110'
    if ids:
        installed = vdf((server / 'steamapps/workshop/appworkshop_346110.acf').read_text())['AppWorkshop']['WorkshopItemsInstalled']
        latest=workshop_details(ids)
        for identity in ids:
            marker=str(installed.get(identity,{}).get('manifest',''))
            if (not marker.isdigit() or int(marker)<=0 or re.search(r'Success\. Downloaded item '+identity+r'\b',output) is None
                    or latest[identity]['time_updated']!=plan['versions']['mods'][identity]['time_updated']):
                raise ValueError('SteamCMD did not confirm a stable Workshop download.')
            install_mod(cache, server / 'ShooterGame/Content/Mods', identity, plan['versions']['mods'][identity]['title'])
    prepare_sdk(server,steam_root)
    n.save(stage / 'asset-manifest.json', {'schema': 1, 'versions': plan['versions'],
                                         'files': tree_manifest(server), 'in_game_content_verified': False})
    return server
