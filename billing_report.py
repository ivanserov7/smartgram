"""Compatible v1/revision-2 analytics and a standalone UTC billing report."""
import hashlib
import json
from datetime import datetime,timezone
from decimal import Decimal,localcontext
from pathlib import Path

from billing_history import timestamp
from billing_api import money_text
from billing_estimates import effective_cost, summarize
from production_metrics import production_metrics
from billing_charts import aggregate_interval,money_sum,selected_calls,series
from file_io import atomic_write,atomic_write_json
from review_gallery import escape


def group_costs(calls,field):
    groups={}
    for call in calls:
        key=call.get(field) or 'unknown'
        group=groups.setdefault(key,{field:key,'allocated_costs':{},'calls':0,'errors':0,'unknown_status_calls':0,
            'known_duration_seconds':0,'unknown_duration_calls':0,'allocation_complete':True,'complete_call_count':0})
        group['calls']+=1
        group['errors']+=call.get('status') in {'failed','error','api_error','incomplete','invalid_response'}
        group['unknown_status_calls']+=call.get('status') is None
        duration=call.get('duration_seconds')
        if type(duration) in (int,float):group['known_duration_seconds']+=duration
        else:group['unknown_duration_calls']+=1
        group['complete_call_count']+=call.get('allocation_complete') is True
        group['allocation_complete']&=call.get('allocation_complete') is True
        group['allocated_costs']=money_sum([group['allocated_costs'],call.get('allocated_costs',{})])
    return list(groups.values())


def build_analytics(snapshot,calls,allocation,ready,sync_state,warnings,runs=None):
    with localcontext() as context:
        context.prec=80
        return _build_analytics(snapshot,calls,allocation,ready,sync_state,warnings,runs or {})


def _build_analytics(snapshot,calls,allocation,ready,sync_state,warnings,runs):
    for call in calls:
        call['effective_cost'] = effective_cost(call)
    period=snapshot.get('period') if snapshot else None
    period_calls=selected_calls(calls,period['start_time'],period['end_time']) if period else []
    completed={key:video for key,video in ready.items() if period and timestamp(video.get('completed_at_utc')) is not None
        and period['start_time']<=timestamp(video['completed_at_utc'])<period['end_time']}
    videos=group_costs(period_calls,'video_id');grouped=group_costs(period_calls,'run_id')
    by_run={g['run_id']:g for g in grouped}
    for run_id,metadata in runs.items():
        # Include metadata without dated calls as well, but do not assign it to a chart day.
        if run_id not in by_run:
            by_run[run_id]={'run_id':run_id,'calls':0,'allocated_costs':{},'errors':0,'unknown_status_calls':0,
                'known_duration_seconds':0,'unknown_duration_calls':0,'allocation_complete':False,'complete_call_count':0}
        by_run[run_id].update(metadata)
    for group in by_run.values():
        for name in ['started_at_utc','ended_at_utc','duration_seconds','status','completed_at_utc']:group.setdefault(name,None)
        group.setdefault('duration_status','known' if group['duration_seconds'] is not None else 'unknown')
        group.setdefault('duration_reason',None if group['duration_seconds'] is not None else 'Нет наблюдаемых границ запуска; длительности вызовов не складываются.')
    for group in videos:
        group['video_title']=next((c.get('video_title') for c in period_calls if c['video_id']==group['video_id']),None)
        group['ready']=group['video_id'] in completed
        group['completed_at_utc']=completed.get(group['video_id'],{}).get('completed_at_utc')
    # Curated provider aggregates are graph facts, not raw API responses/snapshots.
    billing_rows=[{n:r.get(n) for n in ['id','start_time','end_time','project_id','api_key_id','line_item','amount','quantity','quantity_unit','api_source']}
        for r in (snapshot or {}).get('costs',[])]
    usage_rows=[]
    for kind in ['usage','web_usage']:
        for row in (snapshot or {}).get(kind,[]):
            item={n:v for n,v in row.items() if n in {'id','start_time','end_time','project_id','api_key_id','model','service_tier','batch','context_level','api_source','num_model_requests','num_requests'} or n.endswith('_tokens')}
            item['category']='completions' if kind=='usage' else 'web_search';usage_rows.append(item)
    details=allocation['rows'] if allocation else []
    metrics=aggregate_interval(calls,billing_rows,details,runs,ready,period['start_time'],period['end_time']) if period else None
    averages=metrics['average_ready_video_cost'] if metrics else None
    public_scope={k:v for k,v in (snapshot or {}).get('scope',{}).items() if k!='key_fingerprints'}
    result={'schema_version':1,'schema_revision':3,'source':'OpenAI Costs API','period':period,'scope':public_scope,
        'sync':sync_state,'provider_status':'Из биллинга' if snapshot and snapshot.get('costs') else 'Ожидаем данные',
        'provider_data_state':'rows_present' if snapshot and snapshot.get('costs') else 'no_rows' if snapshot else 'not_loaded',
        'actual_charges':allocation['actual'] if allocation and snapshot.get('costs') else None,
        'allocated_charges':allocation['allocated'] if allocation and snapshot.get('costs') else None,
        'unallocated_charges':allocation['unallocated'] if allocation and snapshot.get('costs') else None,
        'allocation_status':'Не распределено' if allocation and any(Decimal(v)!=0 for v in allocation['unallocated'].values()) else 'Распределено' if snapshot and snapshot.get('costs') else 'Ожидаем данные',
        'allocation_details':details,'billing_rows':billing_rows,'provider_consumption':usage_rows,'calls':calls,
        'videos':videos,'runs':list(by_run.values()),'stages':group_costs(period_calls,'stage'),
        'unfinished_video_costs':[g for g in videos if not g['ready']],
        'unfinished_run_costs':[g for g in by_run.values() if g['run_id'] not in {v['run_id'] for v in completed.values()}],
        'retry_costs':{'allocated':None,'reason':'Стоимость ненаблюдаемых/неполных HTTP-попыток отдельно неизвестна; подтверждённые ответы могут получить свою долю.',
            'observed_retries':sum(c.get('retry_attempts_observed') or 0 for c in period_calls),'unknown_attempt_calls':sum(not c.get('attempts_complete') for c in period_calls)},
        'average_ready_video_cost':averages['value'] if averages else None,'average_reason':averages['reason'] if averages else 'Нет данных биллинга/полной готовой когорты.',
        'ready_video_count':len(completed),'unbucketed_ready_video_count':sum(timestamp(v.get('completed_at_utc')) is None for v in ready.values()),'average_formula':'Распределённые расходы выбранного периода всех подтверждённых связанных вызовов полностью учтённой готовой когорты / размер этой когорты по валюте. Не пожизненная стоимость и не среднее дневных средних.',
        'metrics':metrics,'time_series':series(calls,billing_rows,details,runs,ready,period),
        'chart_contract':{'timezone':'UTC','billing_bucket_width':'1d','week_start':'Monday','money_representation':'decimal strings per currency',
            'billing_dates':'Provider UTC buckets retained. No exact Moscow-day cost attribution.',
            'averages':'Recompute numerator/denominator from source sums and complete cohort; never average daily values.',
            'unique_models':'Recompute set of actual model IDs within interval; never sum daily distinct counts.',
            'run_duration':'Explicit lifecycle endpoints/duration only, never sum of parallel call durations.',
            'unknown':'null + status/reason; absence of Costs rows is not zero.',
            'ready_video_average_scope':'Known linked calls charged within selection, for videos completed in selection; not all-time cost.'},
        'completeness':{'warnings':list(dict.fromkeys(warnings)),'historical_missing_fields':sum(bool(c.get('missing_historical_fields')) for c in calls),
            'unknown_period_calls':sum(c.get('start_time') is None and not c.get('billing_day_utc') for c in calls),
            'day_precision_calls':sum(c.get('time_precision')=='day' for c in calls),
            'recovered_field_values':sum(any(p.get('field','').startswith('response.') or p.get('source')=='confirmed_admin_scope' for p in c.get('field_provenance',{}).values()) for c in calls),
            'unknown_key_calls':sum(c.get('api_key_id') is None for c in calls),
            'complete_allocation_calls':sum(c.get('allocation_complete') is True for c in calls),
            'note':'Распределение не является отдельным начислением OpenAI на request_id. Новые начисления могут запаздывать. Факт и распределение нельзя складывать.'},
        'estimates':[c['estimated_cost'] for c in calls if c.get('estimated_cost') is not None]}
    result['effective_costs'] = summarize(period_calls)
    result['production_summary'] = production_metrics(calls, ready, runs)
    result['undated_effective_costs'] = summarize([c for c in calls if c.get('start_time') is None and not c.get('billing_day_utc')])
    for field, groups in [('video_id', result['videos']), ('run_id', result['runs']), ('stage', result['stages'])]:
        for group in groups:
            group['effective_costs'] = summarize([c for c in period_calls if (c.get(field) or 'unknown') == group[field]])
    return result


def render_effective_costs(analytics):
    def money(values):
        return ', '.join(f'{escape(a)} {escape(c.upper())}' for c,a in (values or {}).items()) or 'Нет данных'
    summary = analytics.get('effective_costs') or {}
    undated = analytics.get('undated_effective_costs') or {}
    rows = []
    for call in analytics['calls']:
        value = call.get('effective_cost') or effective_cost(call)
        details = '<br>'.join(escape(x) for x in value['assumptions'] + value['missing'])
        components = '; '.join(escape(c['category']) + ': ' + money({c['currency']: c['amount']}) +
            (' · Факт' if c['kind'] == 'actual' else ' · Оценка') for c in value['components'])
        rows.append('<tr>' + ''.join('<td>' + x + '</td>' for x in [
            escape(call.get('video_title') or call.get('video_id') or 'Без названия'),
            escape(call.get('stage') or '') + '<br>' + escape(call.get('model') or 'Модель неизвестна'),
            money(value['amount']), escape(value['label']) + ('' if value['complete'] else ' · частично'),
            components + '<br>' + details]) + '</tr>')
    return ('<section><h2>Стоимость: факт при наличии, иначе оценка</h2>'
        f'<p>По датированным вызовам выбранного периода: {money(summary.get("amount"))} · {escape(summary.get("label", "Нет данных"))}. '
        f'Факт: {money(summary.get("actual"))}; оценка: {money(summary.get("estimated"))}. '
        f'Цена известна хотя бы частично для {summary.get("known_calls",0)} из {summary.get("calls",0)} вызовов; '
        f'полностью учтено {summary.get("complete_calls",0)}.</p>'
        f'<p>История без даты, отдельно от периода: {money(undated.get("amount"))} · {escape(undated.get("label", "Нет данных"))}.</p>'
        '<p>Факт заменяет оценку той же категории. Это стоимость известных вызовов, а не дополнительный счёт провайдера. '
        'Нераспределённый остаток биллинга не прибавляется к этому итогу. '
        'Оценки основаны на фактическом usage и публичном тарифе; допущения и пробелы показаны ниже.</p>'
        '<div class="table"><table><tr><th>Тема</th><th>Этап / модель</th><th>Стоимость</th><th>Факт или оценка</th>'
        '<th>Состав, допущения и пробелы</th></tr>' + ''.join(rows) + '</table></div></section>')


def render_billing_report(analytics):
    def money(values):return (', '.join(f'{escape(amount)} {escape(currency.upper())}' for currency,amount in (values or {}).items()) + ' · Факт') if values else 'Ожидаем данные'
    rows=''.join('<tr>'+''.join('<td>'+escape(str(v) if v is not None else '—')+'</td>' for v in [
        datetime.fromtimestamp(row['start_time'],timezone.utc).strftime('%Y-%m-%d'),row['line_item'],row['provider_amount'],
        row.get('allocated_amount','0'),row.get('unallocated_amount',row['provider_amount']),row['status'],row.get('reason') or 'По сверенным единицам категории'])+'</tr>' for row in analytics['allocation_details'])
    period,scope,sync=analytics.get('period') or {},analytics.get('scope') or {},analytics['sync']
    metrics=analytics.get('metrics') or {};mean=metrics.get('average_call_cost') or {}
    warnings=''.join('<li>'+escape(message)+'</li>' for message in analytics['completeness']['warnings'])
    panels=[]
    for unit,label in [('day','Дни'),('week','Недели'),('month','Месяцы')]:
        values=analytics.get('time_series',{}).get(unit,[]);table=[]
        for value in values:
            average=value['average_call_cost']
            table.append('<tr>'+''.join('<td>'+str(v)+'</td>' for v in [escape(value['start_utc'][:10]+('*' if value['coverage_truncated'] else '')),money(value['actual_charges']),money(value['allocated_charges']),money(value['unallocated_charges']),
                value['calls_count'],value['run_count'],value['ready_video_count'],value['unique_model_count'] if value['unique_model_count'] is not None else 'Известно '+str(value['known_unique_model_count'])+'; есть неизвестные',value['errors'],value['observed_retries'],
                money(average['value']),escape(str(average['denominator']))+'/'+str(average['population_count'])])+'</tr>')
        panels.append(f'<details><summary>{label}, UTC — суммы и исходные счётчики для графиков</summary><div class="table"><table><tr><th>Начало</th><th>Факт</th><th>Распределено</th><th>Остаток</th><th>Вызовы</th><th>Запуски</th><th>Готовые</th><th>Модели</th><th>Ошибки</th><th>Повторы</th><th>Среднее полного вызова</th><th>Покрытие</th></tr>'+''.join(table)+'</table></div></details>')
    return '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'''+f'<meta name="smartgram-analytics-export" content="{escape(analytics.get("export_id", ""))}">'+'''
<title>Smartgram — расходы OpenAI</title><style>body{max-width:1280px;margin:auto;padding:25px;background:#f4f6f9;color:#1e293b;font:16px/1.6 system-ui}section{background:white;padding:22px;margin:20px 0;border-radius:12px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid #ccd;padding:9px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}.table{overflow:auto}summary{cursor:pointer;font-weight:bold;padding:10px 0}</style><main><h1>Smartgram · расходы OpenAI</h1>'''+(
        render_effective_costs(analytics) +
        f'<section><p>Факт · источник: OpenAI Costs API. Последнее успешное чтение: {escape(sync.get("last_success_at_utc") or "Нет")}. Ошибка: {escape(sync.get("error") or "Нет")}.</p>'
        f'<p>Период UTC: {escape(period.get("start_utc","Не загружен"))} → {escape(period.get("end_utc","Не загружен"))}</p>'
        f'<p>Проект: {escape(scope.get("project_id","Не определён"))}; ключи (ID): {escape(", ".join(scope.get("api_key_ids",[])) or "Не определены")}</p>'
        f'<p><strong>Из биллинга:</strong> {money(analytics["actual_charges"])}<br><strong>Распределено:</strong> {money(analytics["allocated_charges"])}<br><strong>Остаток:</strong> {money(analytics["unallocated_charges"])}</p>'
        '<p>Распределено + остаток = фактические начисления. Не складывайте факт и распределение: это одна сумма в двух разрезах. Тарифные оценки не подмешиваются.</p></section>'
        f'<section><h2>Покрытие и средние</h2><p>Вызовов в журнале: {len(analytics["calls"])}; с неизвестным ключом: {analytics["completeness"].get("unknown_key_calls",0)}; без даты: {analytics["completeness"]["unknown_period_calls"]}.</p>'
        f'<p>Среднее полного вызова: {money(mean.get("value"))}. Числитель: {money(mean.get("numerator"))}; знаменатель: {mean.get("denominator",0)}; вся когорта: {mean.get("population_count",0)}; покрытие: {escape(mean.get("coverage") or "Неизвестно")}.</p>'
        f'<p>Среднее готового ролика: {money(analytics["average_ready_video_cost"])}. {escape(analytics["average_reason"])}</p><p>{escape(analytics["average_formula"])}</p><ul>{warnings}</ul>'
        '<p>Без Costs используется отдельная тарифная оценка выше. Фактическая стоимость остаётся неподтверждённой; баланс биллинга и его остаток сохраняются.</p></section>'
        '<section><h2>Статьи и конкретные причины остатка</h2><div class="table"><table><tr><th>День UTC</th><th>Статья</th><th>Факт</th><th>Распределено</th><th>Остаток</th><th>Статус</th><th>Причина</th></tr>'+rows+'</table></div></section>'
        '<section><h2>Данные для графиков</h2><p>Начисления сохраняют суточные границы UTC; точный разрез расходов по московским дням недоступен. Уникальные модели пересчитаны для каждого интервала. Средние пересчитаны из исходных сумм и размеров полных когорт, а не из дневных средних. Длительность запуска берётся только из журнала жизненного цикла. Звёздочка у даты обозначает усечённый или ещё незавершённый интервал.</p>'+''.join(panels)+'</section>'
        '<section><details><summary>Полный JSON: вызовы, usage/tool_usage, происхождение, категории, запуски и суммы</summary><pre>'+escape(json.dumps(analytics,ensure_ascii=False,indent=2))+'</pre></details></section></main></html>')


def save_exports(directory,analytics):
    # A generation ID binds the exact public JSON to its HTML without raw admin responses.
    def decimal_value(value):
        if isinstance(value,Decimal):return money_text(value)
        raise TypeError('Unsupported public analytics value: '+type(value).__name__)
    analytics=json.loads(json.dumps(analytics,ensure_ascii=False,allow_nan=False,default=decimal_value));analytics.pop('export_id',None)
    analytics['export_id']=hashlib.sha256(json.dumps(analytics,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()
    json_path,html_path=directory/'analytics.json',directory/'billing_report.html'
    atomic_write_json(json_path,analytics);atomic_write(html_path,render_billing_report(analytics).encode('utf-8'))
    return json_path,html_path
