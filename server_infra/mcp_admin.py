#!/usr/bin/python3
"""Root-only JSON broker: fixed reads, checked deployments and drained restarts."""
import contextlib
from datetime import datetime,timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

try:from .mcp_safety import Redactor,SENSITIVE
except ImportError:from mcp_safety import Redactor,SENSITIVE

PANEL=Path('/var/lib/smartgram-panel')
STATE=Path('/var/lib/smartgram-mcp-admin')
DEV=Path('/opt/smartgram/dev')
SERVICES={'panel':'smartgram-panel','vpn':'smartgram-vpn','proxy':'smartgram-panel-proxy',
          'mcp':'smartgram-mcp','tunnel':'smartgram-tunnel'}
RESTARTABLE={'panel','vpn','proxy'}
READS={'status','runs','details','logs','operation'}


class RequestError(Exception):pass


def identifier(value):
    if not isinstance(value,str) or str(uuid.UUID(value))!=value:raise RequestError('Expected a canonical UUID.')
    return value


def validate(request):
    if not isinstance(request,dict):raise RequestError('Expected an object.')
    op=request.get('op')
    allowed={'status':set(),'runs':{'limit','offset','topic'},'details':{'job_id'},
             'logs':{'job_id','source','lines'},'operation':{'operation_id'},
             'publish':{'commit','idempotency_key'},'rollback':{'idempotency_key'},
             'restart':{'services','idempotency_key'}}
    if op not in allowed or set(request)-{'op'}-allowed[op]:raise RequestError('Operation or fields are not allowed.')
    r=dict(request)
    for k in ('job_id','operation_id','idempotency_key'):
        if k in allowed[op]:
            try:identifier(r[k])
            except (KeyError,ValueError,TypeError,AttributeError):raise RequestError('Expected a canonical UUID.') from None
    if op=='publish' and (not isinstance(r.get('commit'),str) or not re.fullmatch('[a-f0-9]{40}',r['commit'])):raise RequestError('Use an exact 40-character Git commit.')
    if op=='restart':
        services=r.get('services')
        if not isinstance(services,list) or not services or len(services)>3 or any(not isinstance(s,str) or s not in RESTARTABLE for s in services) or len(set(services))!=len(services):raise RequestError('Allowed services: panel, vpn, proxy.')
    if op=='runs':
        for key,default,lo,hi in [('limit',20,1,50),('offset',0,0,1000)]:
            n=r.get(key,default)
            if type(n) is not int or not lo<=n<=hi:raise RequestError('Pagination outside allowed range.')
            r[key]=n
        if not isinstance(r.get('topic',''),str) or len(r.get('topic',''))>100:raise RequestError('Topic filter too long.')
    if op=='logs':
        if r.get('source','diagnostic') not in {'diagnostic','pipeline','voice','visuals'}:raise RequestError('Unknown log source.')
        if type(r.get('lines',100)) is not int or not 1<=r.get('lines',100)<=200:raise RequestError('Use 1 to 200 log lines.')
    return r


@contextlib.contextmanager
def directory_fd(base,parts=()):
    fd=os.open(base,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts:
            if not isinstance(part,str) or part in {'','.','..'} or '/' in part:raise RequestError('Invalid path component.')
            nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
        yield fd
    finally:os.close(fd)


def inside_bytes(base,parts,limit=4*1024*1024,tail=False):
    with directory_fd(base,parts[:-1]) as parent:
        fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):raise RequestError('Only regular files are readable.')
            if info.st_size>limit:
                if not tail:raise RequestError('Artifact exceeds the safe read limit.')
                stream.seek(info.st_size-limit);stream.readline() # omit a potentially partial secret/token
            return stream.read(limit)


def inside_json(parts):return json.loads(inside_bytes(PANEL,parts))
def select(value,keys):return {k:value[k] for k in keys if isinstance(value,dict) and k in value}
def utc():return time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())


def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    fd,temp=tempfile.mkstemp(dir=path.parent,prefix='.'+path.name)
    try:
        with os.fdopen(fd,'w') as f:json.dump(value,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
        os.chmod(temp,0o600);os.replace(temp,path)
        d=os.open(path.parent,os.O_DIRECTORY);os.fsync(d);os.close(d)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def known_secrets():
    values=[]
    for name in ['/etc/smartgram/production.env','/etc/smartgram-dev.env']:
        path=Path(name)
        if not path.exists():continue
        for line in path.read_text().splitlines():
            key,sep,value=line.partition('=')
            if sep and re.search('KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL',key):
                try:values.extend(shlex.split(value,comments=True))
                except ValueError:pass
    def walk(v,vpn=False):
        if isinstance(v,dict):
            for k,x in v.items():
                if isinstance(x,str) and (SENSITIVE.search(k) or vpn and k in {'id','uuid'}):values.append(x)
                walk(x,vpn)
        elif isinstance(v,list):
            for x in v:walk(x,vpn)
    for name in ['/etc/smartgram-panel/config.json','/etc/smartgram-vpn/subscription.private']:
        if Path(name).exists():walk(json.loads(Path(name).read_text()),vpn='vpn' in name)
    for name in ['runtime-api-key','bearer-token','forward-authorization']:
        p=Path('/etc/smartgram-mcp')/name
        if p.exists():values.append(p.read_text().strip())
    p=Path('/etc/smartgram-vpn/subscription.url')
    if p.exists():values.append(p.read_text().strip());values.append(values[-1].rsplit('/',1)[-1])
    return values


def state():return inside_json(['state.json'])
def job(job_id):
    for j in state()['jobs']:
        if j['id']==job_id:return j
    raise RequestError('Run not found.')


def run_names(job_id):
    try:
        with directory_fd(PANEL,['jobs',job_id,'runs']) as fd:
            return sorted(n for n in os.listdir(fd) if re.fullmatch(r'research_[A-Za-z0-9_-]{1,100}',n) and stat.S_ISDIR(os.stat(n,dir_fd=fd,follow_symlinks=False).st_mode))
    except FileNotFoundError:return []


def summary(j):
    value=select(j,['id','status','created_at','started_at','ended_at','error'])
    value['topic']=j.get('request',{}).get('topic');value['runs']=run_names(j['id'])
    try:
        raw=inside_json(['jobs',j['id'],'progress.json'])
        value['progress']=select(raw,['stage','stage_id','status','stage_elapsed_seconds','stage_started_at_ms'])
    except FileNotFoundError:pass
    return value


def details(job_id):
    value=summary(job(job_id));base=['jobs',job_id]
    try:value['result']=select(inside_json(base+['result.json']),['status','quality_passed','reason','duration_seconds'])
    except FileNotFoundError:pass
    value['artifacts']=[]
    for name in value['runs']:
        paths=base+['runs',name];item={'run':name}
        for filename,keys in {
            'run.json':['status','stage','reason'],
            'audio_manifest.json':['status','script_version_id','text_verified','reason','audio_sha256','duration_seconds'],
            'visual_materials.json':['status','script_version_id','reason'],
            'render_manifest.json':['status','reason','script_version_id'],
            'quality_report.json':['status','ready','reason']}.items():
            try:
                raw=inside_json(paths+[filename]);item[filename]=select(raw,keys)
                if filename=='visual_materials.json':item['fragments']=[select(f,['fragment_index','status','reason','error']) for f in raw.get('fragments',[])]
            except FileNotFoundError:pass
        value['artifacts'].append(item)
    return value


def logs(request,redactor):
    j=job(request['job_id']);base=['jobs',j['id']];source=request.get('source','diagnostic')
    if source=='diagnostic':parts=base+['diagnostic.log']
    else:
        names=run_names(j['id'])
        if not names:raise RequestError('No pipeline run saved.')
        parts=base+['runs',names[-1],{'pipeline':'run.json','voice':'gemini_usage.json','visuals':'visual_materials_state.json'}[source]]
    if source=='diagnostic':text=inside_bytes(PANEL,parts,limit=256*1024,tail=True).decode(errors='replace');text=redactor.text(text)
    else:text=json.dumps(redactor.value(inside_json(parts)),ensure_ascii=False,indent=2)
    lines=text.splitlines()[-request.get('lines',100):]
    # Redaction is complete before clipping a response, so tokens cannot leak as fragments.
    return {'job_id':j['id'],'source':source,'text':'\n'.join(lines)[-64000:],'untrusted_log_data':True}


def status():
    states={}
    for alias,service in SERVICES.items():
        raw=subprocess.run(['/usr/bin/systemctl','show',service,'-p','ActiveState','-p','SubState','-p','UnitFileState','-p','MemoryCurrent'],capture_output=True,text=True,timeout=5)
        states[alias]=dict(line.split('=',1) for line in raw.stdout.splitlines() if '=' in line)
    jobs=state()['jobs'];memory={}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key,rest=line.split(':',1)
        if key in {'MemTotal','MemAvailable'}:memory[key]=int(rest.split()[0])*1024
    disk=os.statvfs('/var/lib/smartgram')
    tunnel={}
    binding=Path('/etc/smartgram-mcp/binding.json')
    if binding.exists():tunnel=select(json.loads(binding.read_text()),['tunnel_id','organization_ids','workspace_ids','verified_at_utc'])
    try:
        import http.client
        class UnixConnection(http.client.HTTPConnection):
            def connect(self):
                self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(1);self.sock.connect('/run/smartgram-tunnel/health.sock')
        for name,route in [('healthy','/healthz'),('ready','/readyz')]:
            connection=UnixConnection('localhost',timeout=1)
            try:connection.request('GET',route);response=connection.getresponse();tunnel[name]=response.status==200;response.read(65536)
            finally:connection.close()
    except OSError:tunnel.update(healthy=False,ready=False)
    try:
        with socket.create_connection(('127.0.0.1',17890),timeout=.2):vpn_listener=True
    except OSError:vpn_listener=False
    return {'release':Path('/opt/smartgram/current').resolve().name,'services':states,'tunnel':tunnel,'vpn_listener_ready':vpn_listener,
            'queue':{s:sum(j['status']==s for j in jobs) for s in ['running','queued','completed','failed','interrupted','cancelled']},
            'maintenance':(PANEL/'maintenance.json').exists(),'load_average':os.getloadavg(),
            'memory_bytes':memory,'disk_available_bytes':disk.f_bavail*disk.f_frsize}


def operation(op_id):
    identifier(op_id)
    p=STATE/'operations'/(op_id+'.json')
    if not p.exists():raise RequestError('Operation not found.')
    record=json.loads(p.read_text())
    if record['status'] in {'queued','running'} and (datetime.now(timezone.utc)-datetime.fromisoformat(record['created_at'].replace('Z','+00:00'))).total_seconds()>20:
        result=subprocess.run(['/usr/bin/systemctl','show','smartgram-admin-'+op_id,'-p','ActiveState','--value'],capture_output=True,text=True,timeout=5)
        if result.stdout.strip() not in {'active','activating','reloading'}:
            record.update(status='failed',phase='interrupted',ended_at=utc(),error='Worker interrupted. No automatic retry; inspect maintenance before another operation.')
            atomic(p,record)
    return record


def submit(request):
    STATE.mkdir(mode=0o700,exist_ok=True)
    with (STATE/'submit.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=STATE/'idempotency'/(hashlib.sha256(request['idempotency_key'].encode()).hexdigest()+'.json')
        if path.exists():
            prior=json.loads(path.read_text())
            if prior['request']!=request:raise RequestError('Idempotency key already belongs to a different request.')
            return operation(prior['operation_id'])
        operations=STATE/'operations';operations.mkdir(mode=0o700,exist_ok=True)
        for p in operations.glob('*.json'):
            v=operation(p.stem)
            if v['status'] in {'queued','running'}:raise RequestError('Another administrative operation is active.')
        op_id=str(uuid.uuid4());record={'operation_id':op_id,'request':request,'status':'queued','phase':'queued','created_at':utc()}
        atomic(operations/(op_id+'.json'),record);atomic(path,{'request':request,'operation_id':op_id})
        command=['/usr/bin/systemd-run','--quiet','--collect','--unit=smartgram-admin-'+op_id,
                 '--property=Type=exec','--property=RuntimeMaxSec=4200','--property=MemoryMax=768M',
                 '--property=StandardOutput=null','--property=StandardError=null',
                 '/usr/bin/python3','/opt/smartgram-mcp/current/mcp_admin.py','--worker',op_id]
        result=subprocess.run(command,capture_output=True,timeout=10)
        if result.returncode:
            record.update(status='failed',phase='launch_failed',error='Could not start the fixed administrative worker.',ended_at=utc());atomic(operations/(op_id+'.json'),record)
        return record


def worker(op_id):
    record=operation(op_id);request=validate(record['request']);redactor=Redactor(known_secrets())
    path=STATE/'operations'/(op_id+'.json')
    def phase(name,**fields):record.update(phase=name,updated_at=utc(),**fields);atomic(path,redactor.value(record))
    control=runpy.run_path('/usr/local/sbin/smartgram')
    def command(args,**kwargs):
        result=subprocess.run(list(map(str,args)),capture_output=True,text=True,**kwargs)
        if result.stdout or result.stderr:
            output=redactor.text(result.stdout+result.stderr)
            log=STATE/'operations'/(op_id+'.log')
            with log.open('a') as stream:stream.write(output[-256000:])
            log.chmod(0o600)
        result.check_returncode();return result
    control['run'].__globals__['run']=command
    try:
        phase('waiting_for_deployment_lock',status='running',started_at=utc())
        with Path('/var/lib/smartgram-deploy/deploy.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if control['marker']().exists():raise RequestError('Existing maintenance is preserved; finish it before this operation.')
            before=(str(control['CURRENT'].resolve()),str(control['PANEL'].resolve()))
            try:
                phase('waiting_for_active_video');control['drain']()
                if request['op']=='publish':
                    commit=request['commit'];proof=Path('/var/lib/smartgram-deploy/checks')/(commit+'.json')
                    if not proof.exists():
                        if control['git']('rev-parse','HEAD').decode().strip()!=commit:raise RequestError('This commit needs successful CI/server checks first.')
                        phase('checking_commit');control['check']()
                    phase('publishing');control['deploy'](commit)
                elif request['op']=='rollback':phase('rolling_back');control['rollback']()
                elif request['op']=='restart':
                    for service in ['vpn','proxy','panel']:
                        if service in request['services']:phase('restarting_'+service);command(['/usr/bin/systemctl','restart',SERVICES[service]])
                    control['health']();control['marker']().unlink(missing_ok=True)
                else:raise RequestError('Worker operation is not allowed.')
            except BaseException:
                # Resume only when the exact original code is healthy. Uncertain recovery stays paused.
                if before==(str(control['CURRENT'].resolve()),str(control['PANEL'].resolve())):
                    try:
                        control['health']()
                        for service in ['panel','vpn','proxy']:
                            command(['/usr/bin/systemctl','is-active','--quiet',SERVICES[service]])
                        control['marker']().unlink(missing_ok=True)
                    except Exception:pass
                raise
        phase('completed',status='completed',ended_at=utc(),release=Path('/opt/smartgram/current').resolve().name)
    except Exception as error:phase('failed',status='failed',ended_at=utc(),error=redactor.text(str(error)))


def dispatch(request,redactor):
    r=validate(request)
    if r['op']=='status':return status()
    if r['op']=='runs':
        entries=[j for j in reversed(state()['jobs']) if r.get('topic','').casefold() in j.get('request',{}).get('topic','').casefold()]
        return {'runs':[summary(j) for j in entries[r['offset']:r['offset']+r['limit']]],'total':len(entries)}
    if r['op']=='details':return details(r['job_id'])
    if r['op']=='logs':return logs(r,redactor)
    if r['op']=='operation':return operation(r['operation_id'])
    return submit(r)


if __name__=='__main__':
    if os.geteuid()!=0:raise SystemExit('The fixed broker must run through its exact sudo rule.')
    if len(sys.argv)==3 and sys.argv[1]=='--worker':identifier(sys.argv[2]);worker(sys.argv[2]);raise SystemExit(0)
    if len(sys.argv)!=1:raise SystemExit('Arguments are not accepted.')
    redactor=Redactor(known_secrets())
    try:
        data=sys.stdin.buffer.read(8193)
        if len(data)>8192:raise RequestError('Request exceeds the safe input limit.')
        result=dispatch(json.loads(data),redactor)
        print(redactor.serialized({'ok':True,'result':result}))
    except (RequestError,ValueError,TypeError,KeyError,FileNotFoundError,OSError) as error:
        message=str(error) if isinstance(error,RequestError) else 'Requested state is unavailable or invalid.'
        print(redactor.serialized({'ok':False,'error':message}));raise SystemExit(1)
