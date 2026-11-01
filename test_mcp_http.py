"""Real MCP JSON-RPC/ASGI tests; fixed broker is mocked, no sudo or external network."""
import json
from contextlib import asynccontextmanager
import unittest
from unittest.mock import patch

import httpx
from server_infra import mcp_server as module


class MCPHTTPTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def session(self):
        self.token='local-test-bearer-token-'+'x'*40
        self.app=module.server.streamable_http_app(json_response=True,stateless_http=True,host='127.0.0.1')
        async with self.app.router.lifespan_context(self.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.PrivateEndpoint(self.app,self.token)),base_url='http://127.0.0.1:18788') as client:
                self.client=client
                self.headers={'Authorization':'Bearer '+self.token,'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-06-18'}
                yield

    async def request(self,method,params=None):
        return await self.client.post('/mcp',headers=self.headers,json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})

    async def test_private_endpoint_rejects_unauthenticated_calls(self):
        async with self.session():
            response=await self.client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
            self.assertEqual(response.status_code,401)

    async def test_catalog_has_exact_tools_and_correct_mutating_annotations(self):
        async with self.session():
            init=await self.request('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'offline-regression','version':'1'}})
            self.assertEqual(init.status_code,200)
            response=await self.request('tools/list');self.assertEqual(response.status_code,200)
            tools={t['name']:t for t in response.json()['result']['tools']}
            self.assertEqual(set(tools),{'server_status','list_runs','run_details','run_logs','operation_status','publish_commit','rollback_release','restart_services'})
            for name,tool in tools.items():
                mutation=name in {'publish_commit','rollback_release','restart_services'}
                self.assertEqual(tool['annotations']['readOnlyHint'],not mutation)
                self.assertEqual(tool['annotations']['destructiveHint'],mutation)
                self.assertTrue(tool['annotations']['idempotentHint'])

    async def test_actual_call_returns_structured_sanitized_output_without_root(self):
        async with self.session():
            with patch.object(module,'broker',return_value={'release':'fixture','password':'secret','message':'Bearer '+self.token}):
                response=await self.request('tools/call',{'name':'server_status','arguments':{}})
            self.assertEqual(response.status_code,200)
            self.assertNotIn(self.token,response.text);self.assertNotIn('"secret"',response.text)
            self.assertEqual(response.json()['result']['structuredContent']['release'],'fixture')

    async def test_broker_failure_is_not_reported_as_success(self):
        async with self.session():
            with patch.object(module,'broker',side_effect=module.BrokerError('fixed broker rejected operation')):
                response=await self.request('tools/call',{'name':'server_status','arguments':{}})
            self.assertTrue(response.json()['result']['isError'])


if __name__=='__main__':unittest.main()
