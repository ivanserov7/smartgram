"""Three product metrics from verified finished videos, without API-time summation."""
from decimal import Decimal, localcontext
from collections import defaultdict

from billing_api import money_text
from billing_estimates import summarize


def production_metrics(calls, ready, runs):
    """Missing prices do not become zero-cost videos; missing dates do not hide MP4s."""
    with localcontext() as ctx:
        ctx.prec = 80
        durations = []; priced = []; priced_videos = 0
        totals = defaultdict(Decimal); currency_counts = defaultdict(int)
        for video_id, video in ready.items():
            metadata = runs.get(video.get('run_id'), {})
            duration = metadata.get('duration_seconds')
            if type(duration) in (int, float) and 0 <= duration < float('inf'):
                durations.append(duration)
            linked = [c for c in calls if c.get('video_id') == video_id]
            cost = summarize(linked)
            priced.extend(linked)
            if cost['amount'] is not None:
                priced_videos += 1
                for currency, value in cost['amount'].items():
                    totals[currency] += Decimal(value); currency_counts[currency] += 1
        cost = summarize(priced)
        cost['amount'] = {c: money_text(v / currency_counts[c]) for c, v in totals.items()} or None
        for source in ['actual', 'estimated']:
            cost[source] = {c: money_text(Decimal(v) / currency_counts[c]) for c, v in cost[source].items()}
        cost['priced_videos'] = priced_videos
        cost['total_videos'] = len(ready)
        cost['complete'] = cost['complete'] and priced_videos == len(ready)
        return {'ready_video_count': len(ready),
                'average_generation_seconds': sum(durations) / len(durations) if durations else None,
                'timed_video_count': len(durations), 'average_video_cost': cost,
                'scope': 'all_verified_ready_videos'}
