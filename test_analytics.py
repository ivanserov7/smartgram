"""Financial invariants, provenance, exact UTC cohorts and partial consumption; no real API."""
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime,timezone
from decimal import Decimal,localcontext
from pathlib import Path
from unittest.mock import patch
import httpx
from openai import OpenAI

from billing_allocation import canonical_rows,distribute
from billing_api import BillingError
from billing_history import import_calls,normalize_call,run_metadata,timestamp
from billing_report import build_analytics,save_exports
from billing_charts import aggregate_interval,series
from openai_meter import OpenAIMeter
from test_billing import SCOPE,call,cost,page,two_calls,usage


def provider(rows,kind='usage'):return canonical_rows([page(rows)],kind,SCOPE)

def web_usage(total=4,model='websearch',context=None,**extra):
    return {'project_id':'proj_test','api_key_id':'key_test','model':model,'context_level':context,'num_requests':total,'num_model_requests':2,**extra}


def web_call(number,units,model='test-model'):
    c=call(number,10,5,0,0);c.update(model=model,billed_web_requests=units,web_usage_expected=True,
        tool_usage={'web_search':{'num_requests':units}},web_calls_observed=99)
    c['units']['web_requests']=units;return c


class FinancialTests(unittest.TestCase):
    def test_generic_hosted_search_uses_billed_units_not_actions(self):
        calls=[web_call(1,1),web_call(2,3,'other-llm')]
        rows=provider([cost('.04','web search tool calls')],'costs')
        result=distribute(rows,calls,[],provider([web_usage()],'web_usage'))
        self.assertEqual(result['allocated'],{'usd':'0.04'});self.assertEqual(calls[0]['allocated_costs'],{'usd':'0.01'})
        self.assertEqual(calls[1]['allocated_costs'],{'usd':'0.03'})
        self.assertEqual(result['rows'][0]['reconciliation']['billing_model'],'websearch')
        self.assertFalse(calls[0]['allocation_complete']) # Base tokens still await Costs.

    def test_partial_search_keeps_unknown_fraction(self):
        calls=[web_call(1,1)]
        result=distribute(provider([cost('.04','web search tool calls')],'costs'),calls,[],provider([web_usage()],'web_usage'))
        self.assertEqual(result['allocated'],{'usd':'0.01'});self.assertEqual(result['unallocated'],{'usd':'0.03'})
        self.assertIn('1 из 4',result['rows'][0]['reason'])

    def test_no_tool_usage_cannot_be_replaced_by_visible_actions(self):
        c=web_call(1,None);c['tool_usage']=None;c['units']['web_requests']=None
        result=distribute(provider([cost('.04','web search tool calls')],'costs'),[c],[],provider([web_usage()],'web_usage'))
        self.assertEqual(result['unallocated'],{'usd':'0.04'})

    def test_search_requires_compatible_model_context_and_quantity_units(self):
        rows=provider([cost('.04','test-model, web_search_calls')],'costs')
        variants=[provider([web_usage(model='different')],'web_usage'),
                  provider([web_usage(model='test-model',context='small'),web_usage(model='test-model',context='large')],'web_usage')]
        for values in variants:
            self.assertEqual(distribute(rows,[web_call(1,1)],[],values)['unallocated'],{'usd':'0.04'})
        q=cost('.04','web search tool calls');q.update(quantity='4',quantity_unit='tokens')
        result=distribute(provider([q],'costs'),[web_call(1,1)],[],provider([web_usage()],'web_usage'))
        self.assertIn('размерность',result['rows'][0]['reason'])
        q['quantity_unit']='requests'
        self.assertEqual(distribute(provider([q],'costs'),[web_call(1,1)],[],provider([web_usage()],'web_usage'))['allocated'],{'usd':'0.01'})

    def test_one_unknown_local_response_does_not_block_known_category(self):
        calls=two_calls();calls[1]['units']['uncached_tokens']=None;calls[1]['units']['cache_write_tokens']=None
        result=distribute(provider([cost('9')],'costs'),calls,provider([usage()]))
        self.assertEqual(result['allocated'],{'usd':'6'});self.assertEqual(result['unallocated'],{'usd':'3'})

    def test_build_analytics_runs_keyword_preserves_unallocated_residual(self):
        calls=two_calls();calls[1]['units']['uncached_tokens']=None;calls[1]['units']['cache_write_tokens']=None
        rows=provider([cost('9')],'costs');consumption=provider([usage()])
        result=distribute(rows,calls,consumption)
        snapshot={'period':{'start_time':0,'end_time':86400},'scope':SCOPE,'costs':rows,'usage':consumption}
        runs={'saved':{'run_id':'saved','duration_seconds':7,'status':'completed'}}
        report=build_analytics(snapshot,calls,result,{}, {},[],runs=runs)
        self.assertEqual(report['actual_charges'],{'usd':'9'})
        self.assertEqual(report['allocated_charges'],{'usd':'6'})
        self.assertEqual(report['unallocated_charges'],{'usd':'3'})
        self.assertEqual(next(r for r in report['runs'] if r['run_id']=='saved')['duration_seconds'],7)
        self.assertTrue(result['rows'][0]['reason'])

    def test_unrelated_output_gap_does_not_block_input_allocation(self):
        calls=two_calls();calls[1]['units']['output_tokens']=None
        result=distribute(provider([cost('9')],'costs'),calls,provider([usage()]))
        self.assertEqual(result['allocated'],{'usd':'9'})
        self.assertFalse(calls[0]['allocation_complete'])

    def test_complete_means_every_positive_monetary_category(self):
        calls=two_calls();rows=[cost('9'),cost('8','test-model, output'),cost('3','test-model, cached input'),cost('3','test-model, input_cache_write_tokens')]
        distribute(provider(rows,'costs'),calls,provider([usage()]))
        self.assertTrue(all(c['allocation_complete'] for c in calls))
        distribute(provider(rows[:-1],'costs'),calls,provider([usage()]))
        self.assertTrue(all(not c['allocation_complete'] for c in calls))
        self.assertIn('cache_write_tokens',calls[0]['allocation_incomplete_reasons'][0])

    def test_unknown_retry_consumption_does_not_steal_known_units_or_become_complete(self):
        calls=two_calls();calls[0]['attempts']=[{'attempt_number':1,'usage':None},{'attempt_number':2,'usage':calls[0]['usage']}]
        rows=[cost('9'),cost('8','test-model, output'),cost('3','test-model, cached input'),cost('3','test-model, input_cache_write_tokens')]
        result=distribute(provider(rows,'costs'),calls,provider([usage()]))
        self.assertEqual(result['allocated'],{'usd':'23'});self.assertFalse(calls[0]['allocation_complete']);self.assertTrue(calls[1]['allocation_complete'])

    def test_exact_partial_precision_negative_credit_and_currency_balance(self):
        for amount in ['1.000000000000000001','-1.000000000000000001','0']:
            rows=provider([cost(amount)],'costs');result=distribute(rows,two_calls()[:1],provider([usage()]))
            with localcontext() as context:
                context.prec=80
                self.assertEqual(Decimal(result['actual']['usd']),Decimal(result['allocated']['usd'])+Decimal(result['unallocated']['usd']))
                self.assertEqual(sum(Decimal(s['amount']) for s in result['rows'][0]['shares']),Decimal(result['allocated']['usd']))

    def test_known_units_cannot_exceed_provider_and_modes_are_not_mixed(self):
        calls=two_calls();calls[0]['units']['uncached_tokens']=999
        self.assertEqual(distribute(provider([cost('9')],'costs'),calls,provider([usage()]))['unallocated'],{'usd':'9'})
        modes=provider([usage(),dict(usage(),service_tier='flex')])
        self.assertEqual(distribute(provider([cost('9')],'costs'),two_calls(),modes)['unallocated'],{'usd':'9'})

    def test_unique_nondefault_mode_can_be_verified_without_guessing_default(self):
        calls=two_calls()
        for c in calls:c['service_tier']='flex'
        result=distribute(provider([cost('9')],'costs'),calls,provider([dict(usage(),service_tier='flex')]))
        self.assertEqual(result['allocated'],{'usd':'9'})

    def test_zero_aggregate_can_prove_missing_cache_zero_with_provenance(self):
        c=call(1,10,5,0,0);c['units']['cache_write_tokens']=None;c['units']['uncached_tokens']=None
        u=dict(usage(),num_model_requests=1,input_tokens=10,output_tokens=5,input_cached_tokens=0,input_cache_write_tokens=0)
        result=distribute(provider([cost('1')],'costs'),[c],provider([u]))
        self.assertEqual(result['allocated'],{'usd':'1'});self.assertEqual(c['units']['cache_write_tokens'],0)
        self.assertEqual(c['unit_provenance']['cache_write_tokens']['source'],'Usage API')

    def test_provider_days_are_utc_and_endpoint_types_do_not_collide(self):
        with self.assertRaises(BillingError):canonical_rows([page([cost()],1,86401)],'costs',SCOPE)
        self.assertNotEqual(provider([usage()])[0]['id'],provider([usage()],'web_usage')[0]['id'])


class ProvenanceTests(unittest.TestCase):
    def test_response_restores_time_and_tool_usage_but_not_current_key(self):
        raw={'id':'resp_saved','created_at':1793491200,'model':'test-model','service_tier':'default','usage':{'input_tokens':10,'output_tokens':2},
             'tool_usage':{'web_search':{'num_requests':1}},'output':[{'type':'web_search_call'}]*7}
        with tempfile.TemporaryDirectory() as tmp:
            c=normalize_call({'response':raw},Path(tmp)/'research_usage.json',0,SCOPE)
        self.assertEqual(c['started_at_utc'],'2026-11-01T00:00:00Z');self.assertEqual(c['billed_web_requests'],1)
        self.assertEqual(c['web_calls_observed'],7);self.assertIsNone(c['api_key_id'])
        self.assertEqual(c['field_provenance']['tool_usage']['field'],'response.tool_usage')

    def test_day_only_is_not_a_precise_time_and_naive_time_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=normalize_call({'billing_day_utc':'2026-11-01'},Path(tmp)/'research_usage.json',0,SCOPE)
        self.assertIsNone(c['start_time']);self.assertIsNone(c['started_at_utc']);self.assertEqual(c['time_precision'],'day')
        self.assertIsNone(timestamp('2026-11-01T10:00:00'));self.assertEqual(timestamp('2026-11-01T03:00:00+03:00'),1793491200)

    def test_complementary_copies_merge_fields_and_conflicts_remain_sticky(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);r={'response_id':'resp_same','usage':{'input_tokens':10,'output_tokens':2}}
            (root/'research_usage.json').write_text(json.dumps({'requests':[r]}))
            richer={'response':{'id':'resp_same','created_at':1793491200,'service_tier':'default','model':'test-model',
                'tool_usage':{'web_search':{'num_requests':2}}},'api_key_id':'key_test'}
            (root/'script_usage.json').write_text(json.dumps({'requests':[richer]}))
            calls,_=import_calls(root,SCOPE);self.assertEqual(len(calls),1);c=calls[0]
            self.assertEqual(c['api_key_id'],'key_test');self.assertEqual(c['usage']['input_tokens'],10);self.assertEqual(c['billed_web_requests'],2)
            (root/'vision_usage.json').write_text(json.dumps({'requests':[dict(r,usage={'input_tokens':20,'output_tokens':2})]}))
            calls,warnings=import_calls(root,SCOPE);self.assertTrue(warnings);self.assertTrue(calls[0]['conflicting_fields']);self.assertIsNone(calls[0]['units']['input_tokens'])

    def test_fingerprint_is_evidence_and_legacy_identity_survives_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);r={'response_id':'resp_id','api_key_fingerprint':'known'}
            for folder in ['a','b']:
                p=root/folder;p.mkdir();(p/'openai_usage.json').write_text(json.dumps({'requests':[r]}))
            calls,_=import_calls([root/'a',root/'b'],dict(SCOPE,key_fingerprints={'known':'key_test'}))
            self.assertEqual(len(calls),1);self.assertEqual(calls[0]['api_key_id'],'key_test')
            old=calls[0]['call_id'];calls,_=import_calls(root/'b',SCOPE)
            self.assertEqual(calls[0]['call_id'],old);self.assertIsNone(calls[0]['api_key_id'])

    def test_run_wall_time_is_observed_and_render_length_is_not_wall_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'run.json').write_text(json.dumps({'mode':'real','started_at_utc':'2026-11-01T00:00:00Z','ended_at_utc':'2026-11-01T00:01:00Z','status':'stopped'}))
            result=next(iter(run_metadata(root).values()));self.assertEqual(result['duration_seconds'],60)
            (root/'render_manifest.json').write_text(json.dumps({'mode':'real','status':'blocked','duration_seconds':5}))
            result=next(iter(run_metadata(root).values()));self.assertEqual(result['duration_seconds'],60);self.assertEqual(result['status'],'blocked')
            (root/'run.json').write_text('{"mode":"real"}')
            result=next(iter(run_metadata(root).values()));self.assertIsNone(result['duration_seconds'])

    def test_meter_preserves_billable_hosted_usage_including_observed_transport(self):
        from test_research import search_payload
        with tempfile.TemporaryDirectory() as tmp:
            payload=search_payload();payload['tool_usage']={'web_search':{'num_requests':2}}
            client=OpenAI(api_key='test-placeholder',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=payload,headers={'x-request-id':'request'}))))
            try:
                meter=OpenAIMeter(Path(tmp),'collection','test-placeholder');meter.create(client,model='test-model',input='data')
            finally:client.close()
            saved=json.loads((Path(tmp)/'openai_usage.json').read_text())['requests'][0]['telemetry']
            self.assertEqual(saved['tool_usage']['web_search']['num_requests'],2);self.assertEqual(saved['billed_web_requests'],2)
            self.assertEqual(saved['attempts'][0]['tool_usage'],payload['tool_usage']);self.assertEqual(saved['provider'],'openai')
            self.assertNotIn('test-placeholder',json.dumps(saved))


class ChartTests(unittest.TestCase):
    def data(self):
        calls=two_calls()
        for c in calls:
            c.update(allocation_complete=True,billing_day_utc='1970-01-01',allocated_costs={'usd':'1'})
        rows=provider([cost('2')],'costs')
        details=[{'start_time':0,'end_time':86400,'currency':'usd','provider_amount':'2','allocated_amount':'2','unallocated_amount':'0'}]
        return calls,rows,details

    def test_interval_unique_models_and_weighted_mean_not_daily_means(self):
        calls,rows,details=self.data();calls[1].update(start_time=86401,allocated_costs={'usd':'9'})
        calls.append(dict(call(3,10,2,0,0),start_time=86402,allocation_complete=True,allocated_costs={'usd':'1'}))
        result=aggregate_interval(calls,rows,details,{}, {},0,172800)
        self.assertEqual(result['unique_model_count'],1);self.assertEqual(result['average_call_cost']['numerator'],{'usd':'11'})
        self.assertEqual(result['average_call_cost']['denominator'],3)
        with localcontext() as context:
            context.prec=80
            self.assertEqual(Decimal(result['average_call_cost']['value']['usd']),Decimal(11)/3)
        self.assertNotEqual(Decimal(result['average_call_cost']['value']['usd']),Decimal('3'))

    def test_partial_cost_is_excluded_from_full_call_average(self):
        calls,rows,details=self.data();calls[1]['allocation_complete']=False;calls[1]['allocated_costs']={'usd':'999'}
        result=aggregate_interval(calls,rows,details,{}, {},0,86400)
        self.assertEqual(result['average_call_cost']['value'],{'usd':'1'});self.assertEqual(result['average_call_cost']['coverage'],'0.5')
        self.assertEqual(result['average_call_cost']['denominator'],1)

    def test_empty_costs_stay_null_even_when_usage_and_calls_exist(self):
        calls,_,_=self.data()
        result=aggregate_interval(calls,[],[],{}, {},0,86400)
        self.assertIsNone(result['actual_charges'])
        for c in calls:c['allocation_complete']=False
        result=aggregate_interval(calls,[],[],{}, {},0,86400)
        self.assertIsNone(result['average_call_cost']['value'])

    def test_utc_day_boundary_and_non_moscow_shift(self):
        calls,rows,details=self.data();calls[0]['start_time']=86399;calls[1]['start_time']=86400
        self.assertEqual(aggregate_interval(calls,rows,details,{}, {},0,86400)['calls_count'],1)
        self.assertEqual(aggregate_interval(calls,rows,details,{}, {},86400,172800)['calls_count'],1)
        with self.assertRaises(ValueError):aggregate_interval(calls,rows,details,{}, {},-10800,75600)

    def test_parallel_run_duration_is_metadata_not_sum(self):
        calls,rows,details=self.data()
        for c in calls:c['run_id']='one';c['duration_seconds']=60
        runs={'one':{'run_id':'one','duration_seconds':60,'started_at_utc':'1970-01-01T00:00:00Z'}}
        result=aggregate_interval(calls,rows,details,runs,{},0,86400)
        self.assertEqual(result['call_duration_seconds']['known_sum'],120);self.assertEqual(result['run_duration_seconds']['known_sum'],60)

    def test_week_month_use_fresh_model_sets_and_averages(self):
        calls,rows,details=self.data();calls[1]['start_time']=86401
        result=series(calls,rows,details,{}, {},{'start_time':0,'end_time':172800})
        self.assertEqual(len(result['day']),2);self.assertEqual(result['month'][0]['unique_model_count'],1)
        self.assertEqual(result['month'][0]['average_call_cost']['denominator'],2)

    def test_export_pair_has_same_generation_id_and_compatible_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=build_analytics(None,[],None,{}, {'error':None},[])
            paths=save_exports(Path(tmp),data);saved=json.loads(paths[0].read_text())
            self.assertIn(saved['export_id'],paths[1].read_text());self.assertEqual(saved['schema_version'],1);self.assertEqual(saved['schema_revision'],3)
            for name in ['source','allocation_details','calls','videos','sync','period']:self.assertIn(name,saved)

class AdditionalInvariantTests(unittest.TestCase):
    def test_same_response_request_bridge_deduplicates_existing_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            data=[('research_usage.json',{'response_id':'resp_bridge','usage':{'input_tokens':10,'output_tokens':2}}),
                  ('script_usage.json',{'api_request_id':'req_bridge','model':'test-model'}),
                  ('vision_usage.json',{'response_id':'resp_bridge','api_request_id':'req_bridge','api_key_id':'key_test'})]
            for name,row in data:(root/name).write_text(json.dumps({'requests':[row]}))
            calls,warnings=import_calls(root,SCOPE)
            self.assertEqual(len(calls),1);self.assertFalse(warnings)
            self.assertEqual(calls[0]['usage']['input_tokens'],10);self.assertEqual(calls[0]['api_key_id'],'key_test')

    def test_no_allocation_to_call_after_billing_observation_boundary(self):
        calls=two_calls();calls[1]['start_time']=100
        result=distribute(provider([cost('9')],'costs'),calls,provider([usage()]),observed_period={'start_time':0,'end_time':50})
        self.assertEqual(result['allocated'],{'usd':'6'});self.assertEqual(calls[1]['allocated_costs'],{})
        self.assertFalse(calls[1]['allocation_complete'])

    def test_currency_totals_are_independent(self):
        euros=cost('2');euros['amount']['currency']='eur'
        result=distribute(provider([cost('1'),euros],'costs'),two_calls()[:1],provider([usage()]))
        with localcontext() as context:
            context.prec=80
            for currency in ['usd','eur']:
                self.assertEqual(Decimal(result['actual'][currency]),Decimal(result['allocated'][currency])+Decimal(result['unallocated'][currency]))

    def test_explicit_payment_modes_do_not_prevent_other_mode_completeness(self):
        calls=[call(1,10,5,0,0),call(2,10,5,0,0)];calls[1]['service_tier']='flex'
        u=dict(usage(),num_model_requests=1,input_tokens=10,output_tokens=5,input_cached_tokens=0,input_cache_write_tokens=0)
        rows=[cost('1','test-model, default, input'),cost('2','test-model, default, output'),cost('3','test-model, flex, input'),cost('4','test-model, flex, output')]
        distribute(provider(rows,'costs'),calls,provider([u,dict(u,service_tier='flex')]))
        self.assertTrue(all(c['allocation_complete'] for c in calls))
        self.assertEqual(calls[0]['allocated_costs'],{'usd':'3'});self.assertEqual(calls[1]['allocated_costs'],{'usd':'7'})

    def test_pair_mismatch_cannot_replace_existing_analytics_pointer(self):
        import analytics_sync
        from review_archive_sync import MARKER_NAME,DRIVE_FOLDER_ID
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);drive=root/'drive';drive.mkdir()
            (drive/MARKER_NAME).write_text(json.dumps({'drive_folder_id':DRIVE_FOLDER_ID}))
            old=b'{"old":"preserved"}';(drive/'latest_analytics.json').write_bytes(old)
            data=build_analytics(None,[],None,{}, {'error':None},[]);paths=save_exports(root,data)
            paths[1].write_text('<html>another export</html>')
            with patch.object(analytics_sync,'configured_sync_directory',return_value=drive),patch.object(analytics_sync,'dotenv_values',return_value={}):
                self.assertFalse(analytics_sync.sync_analytics(*paths))
            self.assertEqual((drive/'latest_analytics.json').read_bytes(),old)
            self.assertFalse(list(drive.glob('Smartgram_analytics_*.json')))


class DecimalExportTests(unittest.TestCase):
    def test_actual_decimal_cost_quantity_exports_without_float_and_pair_id_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            row=cost('0.0438088');row.update(quantity=Decimal('109522.0'),quantity_unit='tokens')
            costs=provider([row],'costs')
            snapshot={'period':{'start_time':0,'end_time':86400},'scope':SCOPE,'costs':costs,'usage':[],'web_usage':[]}
            data=build_analytics(snapshot,[],distribute(costs,[],[]),{}, {'error':None},[])
            paths=save_exports(Path(tmp),data);export=json.loads(paths[0].read_text())
            self.assertEqual(export['billing_rows'][0]['quantity'],'109522')
            self.assertEqual(export['actual_charges'],{'usd':'0.0438088'})
            value=dict(export);export_id=value.pop('export_id')
            self.assertEqual(hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest(),export_id)
            self.assertIn(export_id,paths[1].read_text())
