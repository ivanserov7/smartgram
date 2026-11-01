"""Allocate actual Costs amounts by reconciled homogeneous units; retain uncovered fractions."""
import hashlib
import json
from collections import defaultdict
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation,ROUND_DOWN,localcontext

from billing_api import BillingError,money_text
from billing_units import count,monetary_categories


def canonical_rows(pages,kind,scope):
    rows={}
    for page in pages:
        for bucket in page['data']:
            start,end=bucket.get('start_time'),bucket.get('end_time')
            if type(start) is not int or type(end) is not int or start<0 or start%86400 or end-start!=86400:
                raise BillingError('API вернул некорректный суточный интервал UTC.')
            for value in bucket['results']:
                if value.get('api_key_id') not in scope['api_key_ids'] or value.get('project_id')!=scope['project_id']:
                    raise BillingError('API вернул строки вне подтверждённого проекта/ключей. Они не присвоены Smartgram.')
                row={'start_time':start,'end_time':end,**value}
                if kind=='costs':
                    try:
                        amount=Decimal(str(value['amount']['value']));currency=value['amount']['currency']
                        if not amount.is_finite() or not isinstance(currency,str) or not currency:raise ValueError
                    except (KeyError,TypeError,ValueError,InvalidOperation):raise BillingError('Строка Costs API не содержит допустимой суммы/валюты; ноль не подставлен.') from None
                    row['amount']={'value':money_text(amount),'currency':currency}
                    identity_data={n:row.get(n) for n in ['start_time','end_time','project_id','api_key_id','line_item','api_source']}
                    identity_data['currency']=currency
                else:identity_data={n:row.get(n) for n in ['start_time','end_time','project_id','api_key_id','model','service_tier','batch','context_level','api_source']}
                identity_data['kind']=kind
                identity=hashlib.sha256(json.dumps(identity_data,sort_keys=True).encode()).hexdigest()
                if identity in rows and rows[identity]!={'id':identity,**row}:raise BillingError('API вернул противоречивые дубликаты одного интервала; снимок не заменён.')
                rows[identity]={'id':identity,**row}
    return list(rows.values())


CATEGORIES={'input_tokens':'uncached_tokens','input':'uncached_tokens','output_tokens':'output_tokens','output':'output_tokens',
    'cached input':'cached_tokens','cached_input_tokens':'cached_tokens','input_cached_tokens':'cached_tokens',
    'input_cache_write_tokens':'cache_write_tokens','cache write':'cache_write_tokens','web_search_calls':'web_requests',
    'input text':'input_text_tokens','output text':'output_text_tokens','input audio':'input_audio_tokens','output audio':'output_audio_tokens'}


def classify(line_item):
    if not isinstance(line_item,str):return None
    if line_item.casefold() in {'web search tool calls','web search calls'}:
        return {'model':None,'category':'web_requests','service_tier':None,'batch':None,'mode_explicit':False,'generic_tool':True}
    parts=[p.strip() for p in line_item.split(',')]
    if not 2<=len(parts)<=3 or parts[-1].casefold() not in CATEGORIES:return None
    tier,batch='default',False
    if len(parts)==3:
        if parts[1]=='batch':batch=True
        elif parts[1] in {'default','flex','priority','scale'}:tier=parts[1]
        else:return None
    return {'model':parts[0],'category':CATEGORIES[parts[-1].casefold()],'service_tier':tier,'batch':batch,'mode_explicit':len(parts)==3,'generic_tool':False}


def in_day(call,row):
    instant=call.get('start_time')
    if instant is not None:return row['start_time']<=instant<row['end_time']
    return call.get('billing_day_utc')==datetime.fromtimestamp(row['start_time'],timezone.utc).date().isoformat()


def scope_match(value,row):
    return value.get('api_key_id')==row['api_key_id'] and value.get('project_id')==row['project_id']


def matching_calls(calls,row,category):
    return [c for c in calls if c.get('allocation_time_eligible',True) and scope_match(c,row) and in_day(c,row)
        and (category['model'] is None or c.get('model')==category['model'])
        and (category['category']=='web_requests' or (c.get('service_tier')==category['service_tier'] and c.get('batch')==category['batch']))]


def provider_quantity(provider,unit):
    names={'cached_tokens':'input_cached_tokens','cache_write_tokens':'input_cache_write_tokens','web_requests':'num_requests'}
    if unit=='uncached_tokens':
        explicit=count(provider.get('input_uncached_tokens'))
        if explicit is not None:return explicit
        values=[count(provider.get(n)) for n in ['input_tokens','input_cached_tokens','input_cache_write_tokens']]
        if all(v is not None for v in values) and values[0]>=values[1]+values[2]:return values[0]-values[1]-values[2]
        return None
    return count(provider.get(names.get(unit,unit)))


def reconcile(calls,row,category,provider_usage,web_usage):
    unit=category['category'];source=web_usage if unit=='web_requests' else provider_usage
    compatible=[u for u in source if scope_match(u,row) and u['start_time']==row['start_time'] and u['end_time']==row['end_time']
                and (category['model'] is None or u.get('model')==category['model'])]
    if not compatible:return [],None,'Нет однозначной сверки Usage API по категории, проекту, ключу и суткам.'
    category=dict(category)
    if unit=='web_requests':
        # A generic hosted-tool bill is a separate billing model, not the caller LLM.
        # One provider model/context is required; heterogeneous tariffs are never pooled.
        dimensions={(u.get('model'),u.get('context_level'),u.get('service_tier'),u.get('batch')) for u in compatible}
        if len(compatible)!=1 or len(dimensions)!=1:return [],None,'Статья поиска не разделяет неоднородные модели/контексты/режимы Usage API.'
    else:
        modes={(u.get('service_tier'),u.get('batch')) for u in compatible}
        if not category['mode_explicit']:
            if len(modes)!=1:return [],None,'Costs API не разделил статью по режимам оплаты, а Usage API содержит разные режимы.'
            tier,batch=next(iter(modes))
            if tier is None or type(batch) is not bool:return [],None,'Неизвестен режим оплаты Usage API.'
            category.update(service_tier=tier,batch=batch)
        compatible=[u for u in compatible if u.get('service_tier')==category['service_tier'] and u.get('batch')==category['batch']]
        if len(compatible)!=1:return [],None,'Нет однозначной сверки Usage API модели и режима.'
    provider=compatible[0];total=provider_quantity(provider,unit)
    if total is None:return [],None,'Usage API не раскрыл фактические единицы нужной категории.'
    # Mixed audio/text counts cannot be used as if they had a single token rate.
    if unit in {'uncached_tokens','output_tokens','cached_tokens','cache_write_tokens'} and any(provider.get(n,0) for n in ['input_audio_tokens','output_audio_tokens']):
        return [],None,'Usage API содержит аудиотокены: соответствие общей статьи текстовым/аудиокатегориям не подтверждено.'
    candidates=matching_calls(calls,row,category)
    if not candidates:
        if any(c.get('api_key_id') is None and (in_day(c,row) or c.get('start_time') is None) and (category['model'] is None or c.get('model')==category['model']) for c in calls):
            return [],None,'У исторических вызовов нет подтверждённого ID ключа и/или UTC-даты; их привязка не выдумана.'
        return [],None,'Нет подтверждённых вызовов этой категории/версии модели/режима за сутки.'
    if unit in {'uncached_tokens','output_tokens','cached_tokens','cache_write_tokens'} and any(
        (c.get('units',{}).get('input_audio_tokens') or c.get('units',{}).get('output_audio_tokens') or
         (c.get('usage') or {}).get('input_token_details',{}).get('audio_tokens') or
         (c.get('usage') or {}).get('output_token_details',{}).get('audio_tokens')) for c in candidates):
        return [],None,'Вызов содержит аудиотокены. Соответствие общей статьи категориям не подтверждено.'
    for call in candidates:
        if call.get('conflicting_fields') or 'conflicting_journals' in call.get('missing_historical_fields',[]):continue
        # Nonnegative per-request cache components are exactly zero when the provider
        # confirms a zero whole-group sum. This is evidence, not a missing-field default.
        if unit!='web_requests':
            for field,provider_field in [('cached_tokens','input_cached_tokens'),('cache_write_tokens','input_cache_write_tokens')]:
                if call['units'].get(field) is None and provider.get(provider_field)==0:
                    call['units'][field]=0;call.setdefault('unit_provenance',{})[field]={'source':'Usage API','row_id':provider['id'],'reason':'confirmed zero nonnegative category for entire matching group'}
            values=[call['units'].get(n) for n in ['input_tokens','cached_tokens','cache_write_tokens']]
            if call['units'].get('uncached_tokens') is None and all(count(v) is not None for v in values) and values[0]>=values[1]+values[2]:
                call['units']['uncached_tokens']=values[0]-values[1]-values[2]
        else:
            # Only provider-reported tool_usage supplies local billed units.
            call['units']['web_requests']=call.get('billed_web_requests',call['units'].get('web_requests'))
    targets=[c for c in candidates if not c.get('conflicting_fields') and 'conflicting_journals' not in c.get('missing_historical_fields',[])
             and count(c['units'].get(unit)) is not None
             and (unit!='web_requests' or provider.get('context_level') is None or c.get('web_context_level')==provider.get('context_level'))]
    known=sum(c['units'][unit] for c in targets)
    if known>total:return [],None,'Единицы журналов превышают Usage API: возможны дубли, неверная привязка или неполный снимок.'
    # Missing records and unknown retry consumption do not forbid allocating a known
    # response's fraction. They never change the denominator to the local journal sum.
    responses=sum(c.get('usage') is not None for c in targets) if unit!='web_requests' else sum(c['units'][unit]>0 for c in targets)
    if count(provider.get('num_model_requests')) is not None and responses>provider['num_model_requests']:
        return [],None,'Число подтверждённых ответов превышает Usage API; сверка конфликтует.'
    if row.get('quantity') is not None:
        factors=({'requests':Decimal(1),'calls':Decimal(1),'1000_requests':Decimal(1000)} if unit=='web_requests'
                 else {'tokens':Decimal(1),'1000_tokens':Decimal(1000)})
        factor=factors.get(row.get('quantity_unit'))
        try:quantity=Decimal(str(row['quantity']))
        except InvalidOperation:return [],None,'Некорректные единицы статьи Costs API.'
        if factor is None or not quantity.is_finite() or quantity*factor!=total:
            return [],None,'Единицы статьи Costs API не совпадают с Usage API либо их размерность неоднозначна.'
    if not targets or (total>0 and known==0):return [],None,'Нет подтверждённых оплачиваемых единиц вызовов; действия search/open_page не подставляются.'
    if total==0 and Decimal(row['amount']['value'])!=0:return [],None,'Положительная/отрицательная сумма при нулевых единицах Usage API.'
    evidence={'provider_row_id':provider['id'],'provider_units':total,'known_units':known,'category':unit,
              'billing_model':provider.get('model'),'service_tier':category['service_tier'],'batch':category['batch'],
              'context_level':provider.get('context_level'),'source':'Web Search Usage API' if unit=='web_requests' else 'Completions Usage API'}
    return targets,evidence,None


def completeness(calls,row,category,provider_usage,web_usage=None):
    targets,evidence,reason=reconcile(calls,row,category,provider_usage,web_usage or [])
    return targets,reason


def distribute(rows,calls,provider_usage,web_usage=None,observed_period=None):
    with localcontext() as context:
        context.prec=80
        for call in calls:
            call.update(allocated_costs={},allocation_status='Ожидаем данные',allocation_complete=False,allocation_categories=[],allocation_incomplete_reasons=[])
            if observed_period:
                instant=call.get('start_time');day=call.get('billing_day_utc')
                if instant is not None:
                    call['allocation_time_eligible']=observed_period['start_time']<=instant<observed_period['end_time']
                elif day:
                    day_start=int(datetime.fromisoformat(day+'T00:00:00+00:00').timestamp())
                    call['allocation_time_eligible']=observed_period['start_time']<=day_start and day_start+86400<=observed_period['end_time']
                else:call['allocation_time_eligible']=True
            else:call.pop('allocation_time_eligible',None)
        actual,allocated,residual=defaultdict(Decimal),defaultdict(Decimal),defaultdict(Decimal);details=[]
        for row in rows:
            currency,amount=row['amount']['currency'],Decimal(row['amount']['value']);actual[currency]+=amount
            category=classify(row.get('line_item'))
            targets,evidence,reason=reconcile(calls,row,category,provider_usage,web_usage or []) if category else ([],None,'Статья расходов не имеет однозначного соответствия модели, режиму и категории потребления.')
            shares=[];distributed=Decimal(0)
            if targets:
                total,known=evidence['provider_units'],evidence['known_units'];unit=category['category']
                quantum=Decimal(1).scaleb(min(amount.as_tuple().exponent,-24))
                part=amount if total==known else (amount*Decimal(known)/Decimal(total)).quantize(quantum,rounding=ROUND_DOWN)
                remaining=part;ordered=sorted(targets,key=lambda c:(c['units'][unit]>0,c['call_id']))
                for i,call in enumerate(ordered):
                    share=remaining if i==len(ordered)-1 else (amount*Decimal(call['units'][unit])/Decimal(total)).quantize(quantum,rounding=ROUND_DOWN) if total else Decimal(0)
                    remaining-=share
                    call['allocated_costs'][currency]=money_text(Decimal(call['allocated_costs'].get(currency,'0'))+share)
                    call['allocation_categories'].append({'billing_row_id':row['id'],'category':unit,'currency':currency,'amount':money_text(share),
                        'units':call['units'][unit],'billing_model':evidence['billing_model'],'provider_units':total,'allocation_kind':'share_of_actual_costs'})
                    shares.append({'call_id':call['call_id'],'amount':money_text(share),'units':call['units'][unit]})
                distributed=part
                if known<total:reason=f'Подтверждена часть единиц: {known} из {total}. Остаток не распределён на известные вызовы.'
            allocated[currency]+=distributed;residual[currency]+=amount-distributed
            details.append({'billing_row_id':row['id'],'line_item':row.get('line_item'),'api_key_id':row['api_key_id'],'project_id':row['project_id'],
                'start_time':row['start_time'],'end_time':row['end_time'],'currency':currency,'provider_amount':money_text(amount),
                'allocated_amount':money_text(distributed),'unallocated_amount':money_text(amount-distributed),
                'status':'Частично распределено' if targets and amount!=distributed else 'Распределено' if targets else 'Не распределено',
                'category':({**category,'service_tier':evidence['service_tier'],'batch':evidence['batch']} if category and evidence else category),'reason':reason,'reconciliation':evidence,'shares':shares})
        for call in calls:
            required,unknown=monetary_categories(call);covered={c['category'] for c in call['allocation_categories']}
            missing=sorted(set(required)-covered);reasons=call['allocation_incomplete_reasons']
            if call.get('allocation_time_eligible') is False:reasons.append('Вызов вне наблюдаемого периода биллинга либо известны только сутки незавершённого интервала.')
            if missing:reasons.append('Нет начислений/распределения категорий: '+', '.join(missing))
            if unknown:reasons.append('Неподтверждённые категории потребления: '+', '.join(unknown))
            if call.get('conflicting_fields') or 'conflicting_journals' in call.get('missing_historical_fields',[]):reasons.append('Противоречивые исторические данные.')
            if not call.get('attempts_complete') or len(call.get('attempts',[]))>1:
                reasons.append('Потребление ненаблюдаемых или предыдущих HTTP-попыток известно не полностью.')
            for detail in details:
                if detail['category'] is None and Decimal(detail['provider_amount']) and scope_match(call,detail) and in_day(call,detail):
                    reasons.append('В той же области есть неподтверждённая денежная категория: '+str(detail['line_item']))
            if not required and not call['allocation_categories']:reasons.append('Нет подтверждённого полного денежного состава вызова.')
            # If another bill for the same expected category could not be reconciled,
            # a successful share in one row does not make that category complete.
            for detail in details:
                cat=detail['category']
                if cat and cat['category'] in required and scope_match(call,detail) and in_day(call,detail) and (cat['model'] is None or cat['model']==call.get('model')) and (cat['category']=='web_requests' or not cat['mode_explicit'] or (cat['service_tier']==call.get('service_tier') and cat['batch']==call.get('batch'))):
                    if not any(s['call_id']==call['call_id'] for s in detail['shares']):
                        reasons.append('Не учтена совместимая статья: '+str(detail['line_item']))
            call['allocation_complete']=bool(call['allocation_categories']) and not reasons
            compatible_cost=any(scope_match(call,row) and in_day(call,row) and (
                (classify(row.get('line_item')) or {}).get('model')==call.get('model') or
                (classify(row.get('line_item')) or {}).get('generic_tool') and call.get('web_usage_expected')) for row in rows)
            waiting=not compatible_cost and call.get('api_key_id') is not None and (call.get('start_time') is not None or call.get('billing_day_utc'))
            call['allocation_status']='Распределено' if call['allocation_complete'] else 'Частично распределено' if call['allocation_categories'] else 'Ожидаем данные' if waiting or not rows else 'Не распределено'
        for currency in actual:
            if allocated[currency]+residual[currency]!=actual[currency]:raise BillingError('Нарушен баланс распределения; экспорт не принят.')
        return {'actual':{c:money_text(v) for c,v in actual.items()},'allocated':{c:money_text(allocated[c]) for c in actual},
                'unallocated':{c:money_text(residual[c]) for c in actual},'rows':details}
