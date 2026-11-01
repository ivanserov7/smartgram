"""Offline regressions for factual allocations with a transparent tariff fallback."""
import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from billing_estimates import effective_cost, summarize
from billing_report import build_analytics, save_exports
from billing_sync import sync_billing
from test_billing import SCOPE, call, cost, usage, page, two_calls
from test_analytics import provider
from billing_allocation import distribute


def sample(**changes):
    value = call(1, 100, 50, 20, 10)
    value.update(model='gpt-6-luna', attempts_complete=True, billed_web_requests=0,
                 web_usage_expected=False, allocation_categories=[], allocated_costs={}, allocation_complete=False)
    value.update(changes)
    return value


class EstimateTests(unittest.TestCase):
    def test_observed_tokens_cache_and_reasoning_not_counted_twice(self):
        c=sample();c['units']['reasoning_tokens']=40
        v=effective_cost(c)
        expected=(Decimal(70)*Decimal('.1')+Decimal(20)*Decimal('.01')+
                  Decimal(10)*Decimal('.125')+Decimal(50)*Decimal('.5'))/1000000
        self.assertEqual(Decimal(v['amount']['usd']),expected)
        self.assertEqual(v['status'],'estimated');self.assertTrue(v['complete'])
        self.assertFalse(v['actual']);self.assertIn('2026-11-01',v['pricing_version'])

    def test_unknown_key_and_date_still_allow_estimate(self):
        v=effective_cost(sample(api_key_id=None, start_time=None, billing_day_utc=None))
        self.assertEqual(v['status'],'estimated');self.assertIsNotNone(v['amount'])

    def test_complete_actual_replaces_all_estimates_including_zero(self):
        for amount in ['0','0.12','-0.01']:
            v=effective_cost(sample(allocation_complete=True,allocated_costs={'usd':amount}))
            self.assertEqual(v['amount'],{'usd':amount});self.assertEqual(v['status'],'actual')
            self.assertFalse(v['estimated']);self.assertTrue(v['complete'])

    def test_partial_actual_replaces_only_same_category_then_full_actual(self):
        c=sample();c['allocation_categories']=[{'category':'uncached_tokens','amount':'.02','currency':'usd'}]
        v=effective_cost(c);self.assertEqual(v['status'],'mixed')
        self.assertEqual(v['actual'],{'usd':'0.02'})
        self.assertEqual({x['kind'] for x in v['components'] if x['category']=='uncached_tokens'},{'actual'})
        self.assertEqual(Decimal(v['amount']['usd']),Decimal('.02')+Decimal(v['estimated']['usd']))
        c.update(allocation_complete=True,allocated_costs={'usd':'.03'},allocation_categories=[])
        v=effective_cost(c);self.assertEqual(v['amount'],{'usd':'0.03'});self.assertFalse(v['estimated'])

    def test_unknown_search_not_replaced_with_actions_and_partial_label(self):
        c=sample(billed_web_requests=None,web_usage_expected=True,web_calls_observed=99)
        c['units']['web_requests']=None;v=effective_cost(c)
        self.assertFalse(v['complete']);self.assertIn('поисков',' '.join(v['missing']))
        self.assertFalse(any(x['category']=='web_requests' for x in v['components']))

    def test_missing_usage_and_unknown_model_never_become_free(self):
        for c in [sample(units={}),sample(model='not-a-priced-model')]:
            v=effective_cost(c);self.assertIsNone(v['amount']);self.assertEqual(v['status'],'unknown')

    def test_unknown_mode_and_cache_are_explicit_assumptions(self):
        c=sample(service_tier=None,batch=None);c['units']['cached_tokens']=None;c['units']['cache_write_tokens']=None
        v=effective_cost(c);self.assertTrue(v['assumptions']);self.assertEqual(v['status'],'estimated')

    def test_mode_long_context_and_cache_ttl(self):
        c=sample();base=Decimal(effective_cost(c)['amount']['usd'])
        self.assertEqual(Decimal(effective_cost(dict(c,batch=True))['amount']['usd']),base/2)
        c['pricing_parameters']={'cache_ttl':'12h'}
        self.assertGreater(Decimal(effective_cost(c)['amount']['usd']),base)
        c['units']['input_tokens']=300000;c['units']['uncached_tokens']=299970
        v=effective_cost(c)
        self.assertEqual(next(x['rate'] for x in v['components'] if x['category']=='output_tokens'),'0.750')

    def test_search_uses_billed_requests(self):
        c=sample(billed_web_requests=3,web_calls_observed=99)
        component=next(x for x in effective_cost(c)['components'] if x['category']=='web_requests')
        self.assertEqual(component['amount'],'0.03')

    def test_observed_previous_http_attempts_are_included_once(self):
        c=sample();base=Decimal(effective_cost(c)['amount']['usd'])
        c['attempts']=[{'usage':c['usage'],'tool_usage':{'web_search':{'num_requests':0}}},{'usage':c['usage']}]
        self.assertEqual(Decimal(effective_cost(c)['amount']['usd']),base*2)
        c['attempts'][0]['usage']=None
        value=effective_cost(c);self.assertEqual(Decimal(value['amount']['usd']),base)
        self.assertFalse(value['complete']);self.assertIn('HTTP',' '.join(value['missing']))

    def test_duration_asr_and_modality_pricing(self):
        v=effective_cost(sample(model='gpt-transcribe',usage={'seconds':40}))
        self.assertEqual(v['amount'],{'usd':'0.003'})
        c=sample(model='gpt-realtime-2.1-mini',units={'input_text_tokens':100,'input_audio_tokens':0,
            'input_image_tokens':0,'output_text_tokens':20,'output_audio_tokens':50},usage={})
        self.assertEqual(Decimal(effective_cost(c)['amount']['usd']),Decimal('.001108'))

    def test_conflicting_journals_and_invalid_cache_are_not_priced(self):
        self.assertIsNone(effective_cost(sample(conflicting_fields=['usage']))['amount'])
        c=sample();c['units']['cached_tokens']=100000
        self.assertIsNone(effective_cost(c)['amount'])

    def test_ledger_and_unallocated_residual_do_not_change(self):
        calls=two_calls();calls[1]['units']['uncached_tokens']=None;calls[1]['units']['cache_write_tokens']=None
        rows=provider([cost('9')],'costs');result=distribute(rows,calls,provider([usage()]))
        before=copy.deepcopy(result)
        snapshot={'period':{'start_time':0,'end_time':86400},'scope':SCOPE,'costs':rows,'usage':[]}
        data=build_analytics(snapshot,calls,result,{}, {},[])
        self.assertEqual(result,before);self.assertEqual(data['actual_charges'],{'usd':'9'})
        self.assertEqual(data['allocated_charges'],{'usd':'6'});self.assertEqual(data['unallocated_charges'],{'usd':'3'})

    def test_summary_no_duplicate_or_cross_currency_and_export_labels(self):
        calls=[sample(),sample(allocation_complete=True,allocated_costs={'eur':'2'})]
        for c in calls:c['effective_cost']=effective_cost(c)
        s=summarize(calls);self.assertEqual(s['status'],'mixed');self.assertEqual(s['amount']['eur'],'2')
        self.assertNotIn('usd',s['actual']);self.assertNotIn('eur',s['estimated'])

    def test_offline_rebuild_never_constructs_api_or_requires_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('billing_sync.load_billing_settings',side_effect=AssertionError('credentials')),patch('billing_sync.BillingClient',side_effect=AssertionError('API')):
                def forbidden(*args):raise AssertionError('API')
                data,paths=sync_billing(directory=Path(tmp)/'billing',output_root=Path(tmp),offline=True,client_factory=forbidden)
            self.assertEqual(data['schema_revision'],3)
            self.assertIn('Факт',paths[1].read_text());self.assertIn('иначе оценка',paths[1].read_text())


if __name__=='__main__':unittest.main()
