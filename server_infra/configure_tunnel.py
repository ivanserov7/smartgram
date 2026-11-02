#!/usr/bin/python3
"""Interactive root-only enrollment. Never reads a pipeline key for authentication."""
import getpass
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import datetime,timezone

ROOT=Path('/etc/smartgram-mcp')


def write(path,text):
    fd,temp=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:f.write(text);f.flush();os.fsync(f.fileno())
        os.chmod(temp,0o600);os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def tunnel_record(value,tunnel_id):
    if isinstance(value,dict):
        if value.get('id')==tunnel_id and 'organization_ids' in value and 'workspace_ids' in value:return value
        for child in value.values():
            found=tunnel_record(child,tunnel_id)
            if found:return found
    if isinstance(value,list):
        for child in value:
            found=tunnel_record(child,tunnel_id)
            if found:return found
    return None


def main():
    if os.geteuid()!=0:raise SystemExit('Use the prepared administrative SSH command.')
    tunnel_id=input('Tunnel ID (tunnel_...): ').strip()
    organization=input('Expected Platform Organization ID: ').strip()
    workspace=input('Expected ChatGPT Workspace ID: ').strip()
    if not re.fullmatch(r'tunnel_[A-Za-z0-9_-]{8,100}',tunnel_id):raise SystemExit('Invalid Tunnel ID.')
    for value in (organization,workspace):
        if not re.fullmatch(r'[A-Za-z0-9_-]{4,150}',value):raise SystemExit('Copy the non-secret scope IDs from the tunnel settings.')
    key=getpass.getpass('NEW restricted tunnel runtime API key (hidden): ').strip()
    if not key.startswith('sk-') or key.startswith('sk-admin-') or len(key)<25:raise SystemExit('A separate runtime API key is required, not an admin key.')
    # Reject accidental reuse without ever authenticating with the stored pipeline keys.
    pipeline=Path('/etc/smartgram/production.env')
    if pipeline.exists() and key in pipeline.read_text():raise SystemExit('Pipeline key reuse refused. Create a separate Tunnels Read + Use key.')
    env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/root','CONTROL_PLANE_API_KEY':key,'CONTROL_PLANE_ORGANIZATION_ID':organization}
    result=subprocess.run(['/usr/local/bin/tunnel-client','admin','--json','tunnels','get',tunnel_id],env=env,capture_output=True,text=True,timeout=40)
    if result.returncode:raise SystemExit('Tunnel metadata lookup failed. Check key permissions, scope and Tunnel ID; no key was saved.')
    try:record=tunnel_record(json.loads(result.stdout),tunnel_id)
    except ValueError:record=None
    if not record:raise SystemExit('Tunnel metadata did not provide verifiable organization/workspace associations; no key was saved.')
    if organization not in record['organization_ids'] or workspace not in record['workspace_ids']:
        raise SystemExit('Tunnel is not associated with the requested organization and ChatGPT workspace. Fix its associations in Platform; no key was saved.')
    # Owner-private installation: refuse additional scopes instead of silently broadening access.
    if set(record['organization_ids'])!={organization} or set(record['workspace_ids'])!={workspace}:
        raise SystemExit('Tunnel has additional scopes. Limit it to the intended organization/workspace or review this explicitly; no key was saved.')
    ROOT.mkdir(mode=0o700,exist_ok=True);ROOT.chmod(0o700)
    write(ROOT/'runtime-api-key',key+'\n')
    config='''config_version: 1
control_plane:
  base_url: https://api.openai.com
  tunnel_id: TUNNEL_ID
  api_key: file:/run/credentials/smartgram-tunnel.service/runtime-api-key
  organization_id: ORGANIZATION_ID
  max_inflight_requests: 4
log:
  level: warn
  format: json
health:
  unix_socket: /run/smartgram-tunnel/health.sock
  url_file: /run/smartgram-tunnel/health-url
admin_ui:
  open_browser: false
  log_buffer_events: 100
mcp:
  server_urls:
    - channel: main
      url: http://127.0.0.1:18788/mcp
  extra_headers:
    Authorization: file:/run/credentials/smartgram-tunnel.service/forward-authorization
  discovery_extra_headers:
    Authorization: file:/run/credentials/smartgram-tunnel.service/forward-authorization
  startup_wait_timeout: 30s
  max_concurrent_requests: 4
'''.replace('TUNNEL_ID',json.dumps(tunnel_id)).replace('ORGANIZATION_ID',json.dumps(organization))
    write(ROOT/'tunnel.yaml',config)
    binding={'tunnel_id':tunnel_id,'organization_ids':record['organization_ids'],'workspace_ids':record['workspace_ids'],
             'verified_at_utc':datetime.now(timezone.utc).isoformat(),'authentication_in_chatgpt':'No authentication',
             'private_target_authentication':'Local bearer injected only by tunnel-client'}
    write(ROOT/'binding.json',json.dumps(binding,ensure_ascii=False,indent=2)+'\n')
    subprocess.run(['/usr/bin/systemctl','enable','smartgram-tunnel'],check=True)
    subprocess.run(['/usr/bin/systemctl','restart','smartgram-tunnel'],check=True)
    print('Separate key saved privately; exact organization/workspace associations verified.')
    print('Tunnel ID:',tunnel_id)
    print('ChatGPT: Connection = Tunnel; Authentication = No authentication.')
    print('The service-to-service bearer is internal. Do not paste the runtime API key into the ChatGPT Authentication field.')
    print('Notify Codex to verify tunnel readiness and make an end-to-end read-only tool call.')


if __name__=='__main__':
    try:main()
    except (OSError,subprocess.SubprocessError):raise SystemExit('Enrollment failed. No secret values were printed; inspect service state privately.') from None
