"""Publish only curated analytics to the existing private Timeweb panel."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
MODULES = ['analytics.js', 'analytics-core.mjs', 'analytics-trends.mjs', 'analytics-charts.mjs', 'analytics-summary.css']

REMOTE = r'''
from pathlib import Path
import hashlib,json,os,shutil,subprocess,sys,time,urllib.request
upload=Path(sys.argv[1]);manifest=json.loads((upload/'manifest.json').read_text())
for name,digest in manifest.items():
 assert Path(name).name==name
 assert hashlib.sha256((upload/name).read_bytes()).hexdigest()==digest
current=Path('/opt/smartgram-panel/current');previous=current.resolve()
config=Path('/etc/smartgram-panel/config.json');before=config.read_bytes()
cfg=json.loads(before)
assert cfg.get('runner_command')==[] and cfg.get('runner_contract')==0,'Adapter is active; review deployment first'
release=previous.parent/('analytics-'+upload.name)
assert not release.exists()
shutil.copytree(previous,release)
for name in manifest:
 shutil.copyfile(upload/name,release/'dist'/name);os.chmod(release/'dist'/name,0o644)
temporary=current.with_name('.current-'+upload.name)
try:
 temporary.symlink_to(release);os.replace(temporary,current)
 subprocess.run(['systemctl','restart','smartgram-panel'],check=True)
 for n in range(30):
  try:
   with urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=1) as response:
    assert response.status==200
   break
  except Exception:
   if n==29:raise
   time.sleep(.2)
 assert config.read_bytes()==before,'Owner configuration changed'
 assert (release/'server/login.html').read_bytes()==(previous/'server/login.html').read_bytes()
except BaseException:
 if temporary.exists() or temporary.is_symlink():temporary.unlink()
 temporary.symlink_to(previous);os.replace(temporary,current)
 subprocess.run(['systemctl','restart','smartgram-panel'],check=False)
 raise
proof={'release':str(release),'previous_release_preserved':str(previous),'files_sha256':manifest,
 'owner_configuration_preserved':True,'login_quote_preserved':True,'pipeline_migrated':False}
(upload/'published.json').write_text(json.dumps(proof,indent=2)+'\n')
print(json.dumps(proof))
'''


def publish(host, identity, json_path, html_path):
    from billing_api import local_secret_values, ENV_PATH
    from build_review_archive import SECRET_VALUE
    from dotenv import dotenv_values
    from decimal import Decimal, localcontext
    raw = json_path.read_bytes(); data = json.loads(raw)
    assert data['source'] == 'OpenAI Costs API' and data['schema_revision'] >= 3
    value = dict(data); exported = value.pop('export_id')
    assert exported == hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()
    html = html_path.read_bytes()
    assert f'name="smartgram-analytics-export" content="{exported}"'.encode() in html
    secrets = local_secret_values(dotenv_values(ENV_PATH,interpolate=False),include_environment=True)
    with localcontext() as ctx:
        ctx.prec=80
        for currency,amount in (data.get('actual_charges') or {}).items():
            assert Decimal(amount)==Decimal(data['allocated_charges'][currency])+Decimal(data['unallocated_charges'][currency])
    payload = {name: (ROOT/'web_analytics'/name).read_bytes() for name in MODULES}
    payload.update({'billing-data.json':raw,'billing_report.html':html})
    for content in payload.values():
        text=content.decode()
        assert not SECRET_VALUE.search(text) and not any(secret in text for secret in secrets),'Possible secret; publication cancelled'
    name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    destination = '/root/smartgram-analytics-uploads/'+name
    options = ['-i',str(identity),'-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes']
    with tempfile.TemporaryDirectory(prefix='smartgram-analytics-publish-') as temp:
        folder=Path(temp)
        for filename,content in payload.items():(folder/filename).write_bytes(content)
        (folder/'manifest.json').write_text(json.dumps({n:hashlib.sha256(v).hexdigest() for n,v in payload.items()},indent=2))
        subprocess.run(['ssh',*options,host,'install -d -m 700 '+destination],check=True)
        subprocess.run(['scp',*options,*map(str,folder.iterdir()),host+':'+destination+'/'],check=True)
        subprocess.run(['ssh',*options,host,'python3 - '+destination],input=REMOTE,text=True,check=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='root@147.45.173.156')
    parser.add_argument('--identity',type=Path,default=Path.home()/'.ssh/smartgram_timeweb_20261101')
    parser.add_argument('--json',type=Path,default=ROOT/'outputs/billing/analytics.json')
    parser.add_argument('--html',type=Path,default=ROOT/'outputs/billing/billing_report.html')
    args=parser.parse_args()
    publish(args.host,args.identity,args.json,args.html)
