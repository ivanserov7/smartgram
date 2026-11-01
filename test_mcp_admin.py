"""Offline security regressions for the privileged broker; never touches a real server."""
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from server_infra import mcp_admin as admin
from server_infra.mcp_safety import Redactor
from server_infra.configure_tunnel import tunnel_record


class BrokerSecurityTests(unittest.TestCase):
    def test_only_fixed_operations_exact_arguments_and_commits(self):
        for request in [dict(op='shell',command='id'),dict(op='status',path='/etc/shadow'),
                        dict(op='publish',commit='main',idempotency_key=str(uuid.uuid4())),
                        dict(op='restart',services=['ssh'],idempotency_key=str(uuid.uuid4())),
                        dict(op='restart',services=['vpn; id'],idempotency_key=str(uuid.uuid4())),
                        dict(op='logs',job_id='../../etc',source='diagnostic'),
                        dict(op='logs',job_id=str(uuid.uuid4()),source='/etc/shadow'),
                        dict(op='runs',limit=True),dict(op='runs',limit=51)]:
            with self.subTest(op=request['op']),self.assertRaises((admin.RequestError,ValueError)):admin.validate(request)
        self.assertEqual(admin.validate({'op':'publish','commit':'a'*40,'idempotency_key':str(uuid.uuid4())})['commit'],'a'*40)

    def test_no_file_or_parent_symlink_can_escape_the_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'inside').mkdir();(root/'outside').mkdir();(root/'outside/key').write_text('do not expose')
            (root/'inside/alias').symlink_to(root/'outside',target_is_directory=True)
            (root/'inside/key').symlink_to(root/'outside/key')
            for parts in [['alias','key'],['key'],['..','outside','key']]:
                with self.subTest(parts=parts),self.assertRaises((OSError,admin.RequestError)):
                    admin.inside_bytes(root/'inside',parts)

    def test_log_redaction_precedes_clipping_and_limits_reads(self):
        secret='a-very-private-opaque-provider-credential'
        redactor=Redactor([secret]);text='\n'.join(['safe']*5+[secret])
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);job_id=str(uuid.uuid4());(root/'jobs'/job_id).mkdir(parents=True)
            (root/'jobs'/job_id/'diagnostic.log').write_text(text)
            with patch.object(admin,'PANEL',root),patch.object(admin,'job',return_value={'id':job_id}):
                result=admin.logs({'job_id':job_id,'lines':1},redactor)
                self.assertNotIn(secret,result['text']);self.assertIn('REDACTED',result['text'])

    def test_partial_first_line_is_omitted_from_a_large_log_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'log').write_bytes(b'prefix-'+b's'*200+b'\nsafe\n')
            self.assertEqual(admin.inside_bytes(root,['log'],limit=30,tail=True),b'safe\n')

    def test_fifo_artifact_is_rejected_without_blocking_the_privileged_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);os.mkfifo(root/'diagnostic.log')
            with self.assertRaises(admin.RequestError):admin.inside_bytes(root,['diagnostic.log'])

    def test_idempotency_never_starts_a_second_mutation(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(admin,'STATE',Path(folder)),patch.object(admin.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as launch:
            request=admin.validate({'op':'restart','services':['vpn'],'idempotency_key':str(uuid.uuid4())})
            first=admin.submit(request);second=admin.submit(request)
            self.assertEqual(first['operation_id'],second['operation_id']);self.assertEqual(launch.call_count,1)
            changed={**request,'services':['panel']}
            with self.assertRaises(admin.RequestError):admin.submit(changed)
            self.assertEqual(launch.call_count,1)

    def test_busy_worker_is_preserved_instead_of_overwritten(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(admin,'STATE',Path(folder)),patch.object(admin.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as launch:
            admin.submit({'op':'rollback','idempotency_key':str(uuid.uuid4())})
            with self.assertRaises(admin.RequestError):admin.submit({'op':'rollback','idempotency_key':str(uuid.uuid4())})
            self.assertEqual(launch.call_count,1)

    def test_redaction_removes_known_tokens_auth_signed_urls_and_vpn_data(self):
        secret='opaque-credential-that-must-not-be-returned'
        data={'status':'failed','message':secret+' vless://identity@example.invalid:443 Bearer '+secret,
              'cookie':'private-cookie','nested':{'password':'hidden','url':'https://example.invalid/a?api_key=private'}}
        result=Redactor([secret]).serialized(data)
        for forbidden in [secret,'vless://identity','private-cookie','hidden','api_key=private']:
            self.assertNotIn(forbidden,result)

    def test_tunnel_binding_requires_known_id_and_both_scopes(self):
        expected={'id':'tunnel_example','organization_ids':['org_example'],'workspace_ids':['workspace_example']}
        self.assertEqual(tunnel_record({'data':expected},'tunnel_example'),expected)
        self.assertIsNone(tunnel_record({'data':expected},'tunnel_other'))
        self.assertIsNone(tunnel_record({'id':'tunnel_example'},'tunnel_example'))

    def test_mutations_are_marked_and_read_tools_remain_read_only(self):
        source=Path('server_infra/mcp_server.py').read_text()
        import ast
        tree=ast.parse(source)
        tools={n.name:ast.unparse(n.decorator_list[0]) for n in tree.body if isinstance(n,ast.FunctionDef) and n.decorator_list}
        for name in ['publish_commit','rollback_release','restart_services']:self.assertIn('annotations=WRITE',tools[name])
        for name in ['server_status','list_runs','run_details','run_logs','operation_status']:self.assertIn('annotations=READ',tools[name])


if __name__=='__main__':unittest.main()
