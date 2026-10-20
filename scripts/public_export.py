"""Inspect/export public source only, never .git or installation/operation data.

A clean export is intentionally history-free: making the existing repository
public can disclose old host information even when today's tree is sanitized.
No git init, commit, push, publication or history rewriting is performed here.
"""
import argparse
import configparser
import ipaddress
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TOP = {'README.md','NETWORK.md','SECURITY.md','LICENSE','NOTICE','.gitignore','.dockerignore',
       'requirements-discord.txt','requirements-discord.lock'}
DIRS = ('scripts','tests','docs','examples','deploy','.github')
ALLOWED_SUFFIX = {'.py','.sh','.gdb','.md','.json','.yml','.yaml','.in','.j2','.ini','.cfg','.txt','.toml'}
TOKEN = re.compile(rb'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,100}(?![A-Za-z0-9_-])|mfa\.[A-Za-z0-9_-]{40,}')
IPV4 = re.compile(rb'(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])')


def candidates(root):
    root=Path(root)
    for name in sorted(TOP):
        path=root/name
        if path.exists() or path.is_symlink():yield path
    for name in DIRS:
        base=root/name
        if base.is_symlink():raise ValueError('Symlinked public source directory.')
        if not base.exists():continue
        for path in sorted(base.rglob('*')):
            if path.is_symlink():raise ValueError('Symlinked public source.')
            if not path.is_file() or '__pycache__' in path.parts:continue
            relative=path.relative_to(root).as_posix()
            if path.suffix not in ALLOWED_SUFFIX and path.name not in ('Dockerfile.runtime','Dockerfile.bot'):continue
            if relative.startswith('deploy/'):
                ordinary={'deploy/README.md','deploy/site.yml','deploy/ansible.cfg','deploy/requirements.txt',
                          'deploy/inventory.example.ini','deploy/secrets.example.yml','deploy/group_vars/all.yml'}
                template=(relative.startswith(('deploy/templates/','deploy/docker/','deploy/discord/'))
                          and (path.suffix in ('.j2','.in') or path.name in ('Dockerfile.runtime','Dockerfile.bot')))
                if relative not in ordinary and not template:continue
            yield path


def private_values(root):
    # Compare values only in memory; NEVER emit a match, snippet or fingerprint.
    values=[]
    token=root/'config/discord-bot.token'
    if token.exists():values.append(token.read_bytes().strip())
    config=root/'config/discord-bot.json'
    if config.exists():
        data=json.loads(config.read_bytes())
        values.extend(str(data[k]).encode() for k in ('guild_id','notification_channel_id') if data.get(k))
    ini=root/'server/ShooterGame/Saved/Config/LinuxServer/GameUserSettings.ini'
    if ini.exists():
        raw=ini.read_bytes();text=raw.decode('utf-16') if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else raw.decode('utf-8-sig')
        for match in re.finditer(r'(?im)^Server(?:Admin)?Password\s*=\s*([^\r\n]+)',text):values.append(match[1].strip().encode())
    return [v for v in values if len(v)>=4]


def inspect(data, secrets=()):
    reasons=[]
    if any(re.search(rb'(?<![A-Za-z0-9_])'+re.escape(value)+rb'(?![A-Za-z0-9_])',data) for value in secrets):
        reasons.append('private_local_value')
    if TOKEN.search(data):reasons.append('token_shape')
    if re.search(rb'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----',data):reasons.append('private_key')
    if re.search(rb'/home/(?!YOUR_USER(?:/|\b))[a-zA-Z0-9_.-]+/',data):reasons.append('personal_home_path')
    networks=tuple(ipaddress.ip_network(v) for v in ('127.0.0.0/8','192.0.2.0/24','198.51.100.0/24','203.0.113.0/24'))
    for match in IPV4.finditer(data):
        try:address=ipaddress.ip_address(match[0].decode())
        except ValueError:continue
        if str(address) in ('0.0.0.0','255.255.255.255','8.8.8.8','1.1.1.1'):continue
        if not any(address in net for net in networks):
            reasons.append('non_example_network_address');break
    return sorted(set(reasons))


def check(root):
    root=Path(root);secrets=private_values(root);findings=[];files=list(candidates(root))
    for path in files:
        reasons=inspect(path.read_bytes(),secrets)
        if reasons:findings.append({'path':path.relative_to(root).as_posix(),'reasons':reasons})
    return files,findings


def history_findings(root):
    # No values, snippets, subjects, email addresses or blobs are returned.
    secrets=private_values(root);findings=[]
    commits=subprocess.check_output(['git','rev-list','--all'],cwd=root,text=True).splitlines()
    seen=set()
    for commit in commits:
        entries=subprocess.check_output(['git','ls-tree','-r',commit],cwd=root,text=True).splitlines()
        for entry in entries:
            metadata,path=entry.split('\t',1);mode,kind,oid=metadata.split()
            if kind!='blob' or (oid,path) in seen:continue
            seen.add((oid,path))
            size=int(subprocess.check_output(['git','cat-file','-s',oid],cwd=root,text=True))
            if size>2*1024*1024:
                findings.append({'path':path,'reasons':['historical_large_blob_needs_review']});continue
            data=subprocess.check_output(['git','cat-file','blob',oid],cwd=root)
            reasons=inspect(data,secrets)
            if reasons:findings.append({'path':path,'reasons':reasons})
    return {'commits_checked':len(commits),'findings':findings,'existing_history_safe_to_publish':not findings}


def export(root,destination):
    files,findings=check(root)
    if findings:raise ValueError('Public source check failed. No export was made.')
    destination=Path(destination)
    if destination.exists() or destination.is_symlink():raise ValueError('A new destination is required.')
    destination.mkdir(mode=0o700,parents=True)
    for source in files:
        target=destination/source.relative_to(root);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,target);target.chmod(0o755 if source.stat().st_mode&0o111 else 0o644)
    return {'exported_files':len(files),'git_history_included':False,'private_installation_data_included':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',type=Path)
    parser.add_argument('--history',action='store_true')
    args=parser.parse_args()
    try:
        if args.history:result=history_findings(ROOT)
        elif args.destination:result=export(ROOT,args.destination)
        else:
            files,findings=check(ROOT);result={'files_checked':len(files),'findings':findings,'ok':not findings}
        print(json.dumps(result));return 1 if result.get('findings') else 0
    except (OSError,ValueError,subprocess.SubprocessError):
        print(json.dumps({'ok':False,'error':'Publication inspection failed; no values were displayed.'}));return 1


if __name__=='__main__':sys.exit(main())
