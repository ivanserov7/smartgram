#!/usr/bin/python3
"""Root-owned deployment helper; sudo exposes only check/deploy/rollback/status."""
import argparse
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time

DEV=Path('/opt/smartgram/dev');CURRENT=Path('/opt/smartgram/current')
PANEL=Path('/opt/smartgram-panel/current');CONFIG=Path('/etc/smartgram-panel/config.json')
STATE=Path('/var/lib/smartgram-deploy');ENV=Path('/opt/smartgram/python-env')
RUNNER_KEYS=['runner_command','runner_cwd','runner_contract','runner_readiness_file']


def run(args,**kwargs):return subprocess.run(list(map(str,args)),check=True,**kwargs)
def git(*args):return subprocess.check_output(['runuser','-u','smartgram-dev','--','git','-C',str(DEV),*args])
def read(path):return json.loads(Path(path).read_text())
def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.'+path.name+'-')
    try:
        with os.fdopen(fd,'w') as f:json.dump(value,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        os.chmod(name,0o640);os.chown(name,0,CONFIG.stat().st_gid);os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)
def switch(link,target):
    temp=link.with_name('.'+link.name+'-switch')
    if temp.is_symlink():temp.unlink()
    temp.symlink_to(target);os.replace(temp,link)
def revision(value):
    if not re.fullmatch('[a-f0-9]{7,40}',value):raise ValueError('Use a concrete hexadecimal Git commit.')
    return git('rev-parse',value+'^{commit}').decode().strip()
def clean():
    if git('status','--porcelain').strip():raise ValueError('Dev has uncommitted files; commit them before check/deploy.')
def marker():return Path(read(CONFIG)['data_dir'])/'maintenance.json'
def active_jobs():return [j for j in read(Path(read(CONFIG)['data_dir'])/'state.json')['jobs'] if j['status']=='running']
def drain():
    atomic(marker(),{'maintenance':True,'reason':'deploy'})
    # Bootstrap is safe only while the old panel cannot launch any paid job.
    if 'SMARTGRAM_DRAIN_PROTOCOL_V1' not in (PANEL/'server/app.mjs').read_text():
        if read(CONFIG).get('runner_command') or active_jobs():raise ValueError('Install drain while legacy runner is disabled first.')
    deadline=time.monotonic()+3600
    while active_jobs():
        if time.monotonic()>deadline:raise ValueError('Active job still running; publication cancelled without killing it.')
        time.sleep(1)
def adapter_check(release, publish=True):
    raw=subprocess.check_output(['runuser','-u','smartgram-panel','--',str(ENV/'bin/python'),str(release/'web_adapter.py'),'--check'],cwd=release)
    proof=json.loads(raw);assert proof['ready'] and proof['release']==str(release.resolve())
    if publish:atomic('/etc/smartgram/adapter-ready.json',proof)
    return proof
def health():
    import urllib.request
    for n in range(50):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=1) as r:assert r.status==200
            return
        except Exception:
            if n==49:raise
            time.sleep(.2)


def check():
    clean();rev=git('rev-parse','HEAD').decode().strip()
    run(['runuser','-u','smartgram-dev','--',ENV/'bin/python',DEV/'server_infra/check.py'],cwd=DEV)
    run(['runuser','-u','smartgram-dev','--','/opt/smartgram-panel/node/bin/node','--test',DEV/'server_infra/test_panel.mjs'],cwd=DEV)
    clean();assert rev==git('rev-parse','HEAD').decode().strip()
    atomic(STATE/'checks'/f'{rev}.json',{'revision':rev,'passed':True})
    print(json.dumps({'checked_revision':rev,'passed':True}))


def prepare(rev):
    release=Path('/opt/smartgram/releases')/rev
    if release.exists():return release
    archive=git('archive','--format=tar',rev);release.mkdir(mode=0o755)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            p=Path(member.name)
            if p.is_absolute() or '..' in p.parts or not (member.isfile() or member.isdir()) or set(p.parts)&{'.env','.git','outputs'}:raise ValueError('Unsafe release archive entry')
            if member.isdir():(release/p).mkdir(parents=True,exist_ok=True);continue
            target=release/p;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(tar.extractfile(member).read());target.chmod(0o644)
    if hashlib.sha256((release/'requirements.txt').read_bytes()).hexdigest()!=(ENV/'requirements.sha256').read_text().strip():raise ValueError('Dependencies changed; prepare a reviewed Linux environment first.')
    (release/'.env').symlink_to('/etc/smartgram/production.env')
    (release/'.venv').symlink_to(ENV)
    (release/'outputs').symlink_to('/var/lib/smartgram/outputs')
    return release


def deploy(rev):
    clean();rev=revision(rev)
    if not read(STATE/'checks'/f'{rev}.json').get('passed'):raise ValueError('Run check on this revision first.')
    release=prepare(rev);adapter_check(release,publish=False)
    old={'pipeline':str(CURRENT.resolve()) if CURRENT.exists() else None,'panel':str(PANEL.resolve()),
         'runner':{k:read(CONFIG).get(k) for k in RUNNER_KEYS}}
    panel_release=Path('/opt/smartgram-panel/releases')/('pipeline-'+rev[:16]+'-'+str(time.time_ns()))
    shutil.copytree(PANEL.resolve(),panel_release)
    shutil.copyfile(release/'server_infra/panel_app.mjs',panel_release/'server/app.mjs')
    drain();resumed=False
    try:
        cfg=read(CONFIG);cfg.update(runner_command=[str(ENV/'bin/python'),str(CURRENT/'web_adapter.py')],
            runner_cwd=str(CURRENT),runner_contract=1,runner_readiness_file='/etc/smartgram/adapter-ready.json')
        switch(CURRENT,release);switch(PANEL,panel_release);atomic(CONFIG,cfg)
        adapter_check(release);run(['systemctl','restart','smartgram-panel']);health()
        atomic(STATE/'previous.json',old);atomic(STATE/'active.json',{'revision':rev,'pipeline':str(release),'panel':str(panel_release)})
        resumed=True
    except BaseException:
        cfg=read(CONFIG);cfg.update(old['runner']);atomic(CONFIG,cfg);switch(PANEL,old['panel'])
        if old['pipeline']:switch(CURRENT,old['pipeline']);adapter_check(Path(old['pipeline']))
        run(['systemctl','restart','smartgram-panel']);health();resumed=True;raise
    finally:
        # No queued work is discarded; its selection resumes after a healthy switch.
        if resumed:marker().unlink(missing_ok=True)
    print(json.dumps({'deployed':rev,'drained':True,'previous_preserved':True}))


def rollback():
    old=read(STATE/'previous.json');now={'pipeline':str(CURRENT.resolve()),'panel':str(PANEL.resolve()),'runner':{k:read(CONFIG).get(k) for k in RUNNER_KEYS}}
    drain();resumed=False
    try:
        cfg=read(CONFIG);cfg.update(old['runner']);atomic(CONFIG,cfg);switch(PANEL,old['panel'])
        if old['pipeline']:switch(CURRENT,old['pipeline']);adapter_check(Path(old['pipeline']))
        run(['systemctl','restart','smartgram-panel']);health();atomic(STATE/'previous.json',now)
        atomic(STATE/'active.json',{'pipeline':old['pipeline'],'panel':old['panel']})
        resumed=True
    except BaseException:
        cfg=read(CONFIG);cfg.update(now['runner']);atomic(CONFIG,cfg);switch(PANEL,now['panel']);switch(CURRENT,now['pipeline']);adapter_check(Path(now['pipeline']))
        run(['systemctl','restart','smartgram-panel']);health();resumed=True;raise
    finally:
        if resumed:marker().unlink(missing_ok=True)
    print(json.dumps({'rolled_back':True,'data_preserved':True}))


if __name__=='__main__':
    if os.geteuid()!=0:raise SystemExit('Use sudo smartgram check|deploy|rollback|status.')
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['check','deploy','rollback','status']);parser.add_argument('revision',nargs='?');args=parser.parse_args()
    STATE.mkdir(mode=0o700,exist_ok=True)
    with (STATE/'deploy.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.action=='check':check()
        elif args.action=='deploy':deploy(args.revision or '')
        elif args.action=='rollback':rollback()
        else:print(json.dumps({'pipeline':str(CURRENT.resolve()) if CURRENT.exists() else None,'panel':str(PANEL.resolve()),'active_jobs':len(active_jobs()),'maintenance':marker().exists(),'adapter_ready':read('/etc/smartgram/adapter-ready.json').get('ready') if Path('/etc/smartgram/adapter-ready.json').exists() else False}))
