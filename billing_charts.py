"""Recomputable UTC chart metrics. Money never moves across billing-day boundaries."""
from collections import defaultdict
from datetime import datetime,timedelta,timezone
from decimal import Decimal,localcontext
from billing_api import money_text
from billing_history import timestamp

TOKEN_UNITS=['input_tokens','output_tokens','cached_tokens','cache_write_tokens','uncached_tokens',
             'input_text_tokens','output_text_tokens','input_audio_tokens','output_audio_tokens','web_requests']


def money_sum(items):
    with localcontext() as context:
        context.prec=80
        return _money_sum(items)


def _money_sum(items):
    result=defaultdict(Decimal)
    for item in items:
        for currency,value in item.items():result[currency]+=Decimal(value)
    return {c:money_text(v) for c,v in result.items()}


def average_metric(population,complete,costs):
    with localcontext() as context:
        context.prec=80
        return _average_metric(population,complete,costs)


def _average_metric(population,complete,costs):
    totals=money_sum(costs);denominators={c:sum(c in item for item in costs) for c in totals}
    values={c:money_text(Decimal(v)/denominators[c]) for c,v in totals.items() if denominators[c]}
    return {'value':values or None,'numerator':totals or None,'denominator':len(complete),
            'denominator_by_currency':denominators,'population_count':population,
            'coverage':money_text(Decimal(len(complete))/population) if population else None,
            'status':'complete' if population and len(complete)==population else 'partial_cohort' if complete else 'unknown',
            'reason':('Среднее только полной по денежным категориям когорты; покрытие указано отдельно.' if complete
                      else 'Нет полной денежной когорты; частичные суммы и отсутствие начислений не считаются полной стоимостью.'),
            'formula':'Sum of fully allocated cohort costs per currency / cohort count for that currency; recomputed from source rows, never daily means.'}


def call_day(call):
    if call.get('start_time') is not None:
        try:return datetime.fromtimestamp(call['start_time'],timezone.utc).date().isoformat()
        except (ValueError,OSError):pass
    return call.get('billing_day_utc')


def selected_calls(calls,start,end):
    result=[]
    for call in calls:
        instant=call.get('start_time')
        if instant is not None:
            if start<=instant<end:result.append(call)
        elif call.get('billing_day_utc'):
            day=timestamp(call['billing_day_utc']+'T00:00:00Z')
            if day is not None and start<=day<end:result.append(call)
    return result


def summarize(calls,billing_rows,allocation_rows,runs,completed):
    models=sorted({c['model'] for c in calls if c.get('model')});run_ids=sorted({c['run_id'] for c in calls}|set(runs))
    tokens={}
    for unit in TOKEN_UNITS:
        known=[c.get('units',{}).get(unit) for c in calls if type(c.get('units',{}).get(unit)) is int]
        tokens[unit]={'value':sum(known) if calls and len(known)==len(calls) else None,'known_sum':sum(known) if known else None,
                      'known_calls':len(known),'unknown_calls':len(calls)-len(known),
                      'status':'complete' if calls and len(known)==len(calls) else 'partial' if known else 'unknown',
                      'reason':None if calls and len(known)==len(calls) else 'Не все журналы содержат подтверждённые единицы этой категории.'}
    complete=[c for c in calls if c.get('allocation_complete') is True] if billing_rows else []
    average=average_metric(len(calls),complete,[c['allocated_costs'] for c in complete])
    ready_complete=[];ready_costs=[]
    for video_id,video in completed.items():
        cohort=[c for c in calls if c['video_id']==video_id]
        if billing_rows and video.get('cost_population_complete',True) and cohort and all(c.get('allocation_complete') is True for c in cohort):
            ready_complete.append(video_id);ready_costs.append(money_sum(c['allocated_costs'] for c in cohort))
    ready_average=average_metric(len(completed),ready_complete,ready_costs)
    if not completed:ready_average['reason']='Нет одобренных готовых MP4 с известным временем завершения в выбранном периоде.'
    durations=[c['duration_seconds'] for c in calls if type(c.get('duration_seconds')) in (int,float)]
    run_durations=[r['duration_seconds'] for r in runs.values() if type(r.get('duration_seconds')) in (int,float)]
    actual=money_sum({row['amount']['currency']:row['amount']['value']} for row in billing_rows) if billing_rows else None
    allocated=money_sum({row['currency']:row.get('allocated_amount','0')} for row in allocation_rows) if billing_rows else None
    remaining=money_sum({row['currency']:row.get('unallocated_amount',row['provider_amount'])} for row in allocation_rows) if billing_rows else None
    return {'actual_charges':actual,'allocated_charges':allocated,'unallocated_charges':remaining,
        'calls_count':len(calls),'confirmed_calls_count':sum(c.get('request_observed') is True for c in calls),
        'unknown_request_observation_calls':sum(c.get('request_observed') is not True for c in calls),'run_count':len(run_ids),'run_ids':run_ids,'ready_video_count':len(completed),
        'ready_video_ids':sorted(completed),'models':models,'unique_model_count':len(models) if all(c.get('model') for c in calls) else None,
        'known_unique_model_count':len(models),'unique_model_count_status':'complete' if all(c.get('model') for c in calls) else 'partial',
        'unique_model_count_reason':None if all(c.get('model') for c in calls) else 'Есть вызовы без подтверждённой фактической модели.',
        'unknown_model_calls':sum(not c.get('model') for c in calls),
        'tokens':tokens,'errors':sum(c.get('api_status') in {'failed','incomplete','cancelled'} or c.get('status') in {'failed','error','api_error','incomplete','invalid_response','technical_error'} for c in calls),
        'observed_retries':sum(c.get('retry_attempts_observed') or 0 for c in calls),
        'failed_http_attempts':sum(type(a.get('http_status')) is int and a['http_status']>=400 for c in calls for a in c.get('attempts',[])),
        'unknown_retry_calls':sum(c.get('retry_attempts_observed') is None for c in calls),
        'call_duration_seconds':{'known_sum':sum(durations) if durations else None,'known_count':len(durations),'unknown_count':len(calls)-len(durations)},
        'run_duration_seconds':{'known_sum':sum(run_durations) if run_durations else None,'known_count':len(run_durations),'unknown_count':len(run_ids)-len(run_durations),
                                'source':'actual run lifecycle only; not sum of calls'},
        'average_call_cost':average,'average_ready_video_cost':ready_average}


def aggregate_interval(calls,billing_rows,allocation_rows,runs,ready,start,end):
    if start%86400:raise ValueError('Billing selection must begin at a UTC day boundary; exact Moscow-day allocation is unavailable.')
    selected=selected_calls(calls,start,end)
    # Include complete provider day buckets only. A current partial day remains a UTC
    # bucket with its original boundaries; the export records the provider observation end.
    rows=[r for r in billing_rows if start<=r['start_time']<end]
    details=[r for r in allocation_rows if start<=r['start_time']<end]
    active={c['run_id'] for c in selected}
    chosen_runs={key:value for key,value in runs.items() if key in active or timestamp(value.get('started_at_utc')) is not None and start<=timestamp(value['started_at_utc'])<end}
    completed={key:value for key,value in ready.items() if timestamp(value.get('completed_at_utc')) is not None and start<=timestamp(value['completed_at_utc'])<end}
    for video_id,metadata in completed.items():
        completed[video_id]={**metadata,'cost_population_complete':not any(c['video_id']==video_id and c.get('start_time') is None and not c.get('billing_day_utc') for c in calls)}
    return summarize(selected,rows,details,chosen_runs,completed)


def series(calls,billing_rows,allocation_rows,runs,ready,period):
    if not period:return {'day':[],'week':[],'month':[]}
    start,end=period['start_time'],period['end_time'];days=[]
    cursor=datetime.fromtimestamp(start,timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
    while int(cursor.timestamp())<end:
        days.append(cursor);cursor+=timedelta(days=1)
    result={}
    for unit in ['day','week','month']:
        boundaries={}
        for day in days:
            left=day if unit=='day' else day-timedelta(days=day.weekday()) if unit=='week' else day.replace(day=1)
            if unit=='month':right=(left.replace(day=28)+timedelta(days=4)).replace(day=1)
            else:right=left+timedelta(days=1 if unit=='day' else 7)
            boundaries[int(left.timestamp())]=int(right.timestamp())
        values=[]
        for left,right in sorted(boundaries.items()):
            effective_start=max(start,left);effective_end=min(end,right)
            metric=aggregate_interval(calls,billing_rows,allocation_rows,runs,ready,effective_start,effective_end)
            values.append({'start_time':left,'end_time':right,'start_utc':datetime.fromtimestamp(left,timezone.utc).isoformat(),
                'end_utc':datetime.fromtimestamp(right,timezone.utc).isoformat(),'observed_start_time':effective_start,'observed_end_time':effective_end,
                'coverage_truncated':effective_start!=left or effective_end!=right,**metric})
        result[unit]=values
    return result
