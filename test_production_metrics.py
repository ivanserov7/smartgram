"""Product metrics use unique finished videos and real lifecycle measurements."""
import unittest

from production_metrics import production_metrics
from billing_report import build_analytics


def call(video, amount, kind='estimated', complete=True):
    return {'video_id':video, 'duration_seconds':999, 'effective_cost':{
        'amount':{'usd':amount}, 'actual':{'usd':amount} if kind=='actual' else {},
        'estimated':{'usd':amount} if kind=='estimated' else {}, 'complete':complete}}


class ProductMetricsTests(unittest.TestCase):
    def test_only_unique_ready_videos_count_and_wall_time_comes_from_lifecycle(self):
        calls=[call('v1','1'),call('v1','3'),call('v2','2'),call('unfinished','99')]
        ready={'v1':{'run_id':'r1'},'v2':{'run_id':'r2'}}
        result=production_metrics(calls,ready,{'r1':{'duration_seconds':60},'r2':{'duration_seconds':120}})
        self.assertEqual(result['ready_video_count'],2)
        self.assertEqual(result['average_generation_seconds'],90)
        self.assertEqual(result['average_video_cost']['amount'],{'usd':'3'})
        self.assertEqual(result['average_video_cost']['estimated'],{'usd':'3'})

    def test_missing_lifecycle_does_not_use_api_time_or_media_duration(self):
        result=production_metrics([call('v','2')],{'v':{'run_id':'r','duration_seconds':36}}, {})
        self.assertIsNone(result['average_generation_seconds']);self.assertEqual(result['timed_video_count'],0)

    def test_unpriced_video_does_not_dilute_average_as_a_free_video(self):
        result=production_metrics([call('v1','2')],{'v1':{'run_id':'r1'},'v2':{'run_id':'r2'}},{})
        cost=result['average_video_cost'];self.assertEqual(cost['amount'],{'usd':'2'})
        self.assertEqual(cost['priced_videos'],1);self.assertFalse(cost['complete'])

    def test_source_labels_and_partial_data_survive_averaging(self):
        result=production_metrics([call('v1','2','actual'),call('v2','4',complete=False)],
                                  {'v1':{'run_id':'r1'},'v2':{'run_id':'r2'}},{})
        cost=result['average_video_cost'];self.assertEqual(cost['label'],'Факт + оценка')
        self.assertEqual(cost['amount'],{'usd':'3'});self.assertEqual(cost['actual'],{'usd':'1'})
        self.assertEqual(cost['estimated'],{'usd':'2'});self.assertFalse(cost['complete'])

    def test_no_ready_videos_produces_zero_count_and_unknown_averages(self):
        result=production_metrics([call('unfinished','5')],{}, {})
        self.assertEqual(result['ready_video_count'],0)
        self.assertIsNone(result['average_video_cost']['amount']);self.assertIsNone(result['average_generation_seconds'])

    def test_undated_real_mp4_is_in_lifetime_count_without_changing_utc_cohort(self):
        ready={'v':{'run_id':'r','video_id':'v','completed_at_utc':None}}
        result=build_analytics(None,[],None,ready,{},[],runs={'r':{'run_id':'r','duration_seconds':0}})
        self.assertEqual(result['ready_video_count'],0)
        self.assertEqual(result['unbucketed_ready_video_count'],1)
        self.assertEqual(result['production_summary']['ready_video_count'],1)
        self.assertEqual(result['production_summary']['average_generation_seconds'],0)


if __name__=='__main__':unittest.main()
