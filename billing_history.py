"""Evidence-based import: original responses and complementary copies, never today's key by assumption."""
import hashlib
import json
import math
import re
from datetime import datetime,timezone
from pathlib import Path

from openai_meter import run_context
from billing_units import consumption,billed_searches,object_value

JOURNALS={'research_usage.json','script_usage.json','vision_usage.json','openai_usage.json'}


def timestamp(value):
    try:
        moment=datetime.fromisoformat(value.replace('Z','+00:00'))
        return int(moment.timestamp()) if moment.tzinfo is not None else None
    except (AttributeError,TypeError,ValueError,OverflowError):return None


def iso_from_epoch(value):
    if type(value) not in (int,float) or not math.isfinite(value):return None
    try:return datetime.fromtimestamp(value,timezone.utc).isoformat().replace('+00:00','Z')
    except (ValueError,OSError,OverflowError):return None


def normalize_call(record,path,index,scope):
    telemetry=object_value(record.get('telemetry'));raw=object_value(record.get('response') or record.get('raw_response'))
    request=object_value(record.get('request'));context=run_context(path.parent);provenance={}
    def choose(name,options):
        for source,value in options:
            if value is not None:
                provenance[name]={'source':str(path),'field':source};return value
        return None
    response_id=choose('provider_response_id',[('telemetry.provider_response_id',telemetry.get('provider_response_id')),
        ('response_id',record.get('response_id')),('response.id',raw.get('id'))])
    last_attempt=(telemetry.get('attempts') or [{}])[-1]
    request_id=choose('provider_request_id',[('telemetry.provider_request_id',telemetry.get('provider_request_id')),
        ('telemetry.attempts[-1].provider_request_id',last_attempt.get('provider_request_id')),
        ('api_request_id',record.get('api_request_id')),('response.request_id',raw.get('request_id'))])
    identifier=response_id or request_id
    call_id=telemetry.get('call_id') or 'import_'+hashlib.sha256((('provider:'+identifier) if identifier else str(path.resolve())+':'+str(index)).encode()).hexdigest()
    key_id=choose('api_key_id',[('telemetry.api_key_id',telemetry.get('api_key_id')),('api_key_id',record.get('api_key_id')),
        ('response.api_key_id',raw.get('api_key_id'))])
    fingerprint=telemetry.get('api_key_fingerprint') or record.get('api_key_fingerprint')
    if key_id is None and fingerprint in scope.get('key_fingerprints',{}):
        key_id=scope['key_fingerprints'][fingerprint]
        provenance['api_key_id']={'source':'confirmed_admin_scope','field':'key_fingerprints','journal':str(path)}
    project=choose('project_id',[('telemetry.project_id',telemetry.get('project_id')),('project_id',record.get('project_id')),
        ('response.project_id',raw.get('project_id'))])
    if project is None and key_id is not None and key_id in scope.get('api_key_ids',[]):
        project=scope.get('project_id');provenance['project_id']={'source':'confirmed_admin_scope','field':'api_key_ids membership'}
    usage=choose('usage',[('telemetry.usage',telemetry.get('usage')),('usage',record.get('usage')),('response.usage',raw.get('usage'))])
    tool_usage=choose('tool_usage',[('telemetry.tool_usage',telemetry.get('tool_usage')),('tool_usage',record.get('tool_usage')),('response.tool_usage',raw.get('tool_usage'))])
    units=consumption(usage);web=billed_searches(tool_usage);units['web_requests']=web
    if web is not None:provenance['billed_web_requests']={**provenance['tool_usage'],'field':provenance['tool_usage']['field']+'.web_search.num_requests'}
    tools=telemetry.get('tools')
    if tools is None and isinstance(raw.get('output'),list):tools=[i for i in raw['output'] if isinstance(i,dict) and i.get('type','').endswith('_call')]
    start=choose('started_at_utc',[('telemetry.started_at_utc',telemetry.get('started_at_utc')),('started_at_utc',record.get('started_at_utc'))])
    provider_created=choose('created_at_utc',[('telemetry.created_at',iso_from_epoch(telemetry.get('created_at'))),('response.created_at',iso_from_epoch(raw.get('created_at')))])
    if timestamp(start) is None:start=provider_created
    if start==provider_created and start is not None:provenance['started_at_utc']=provenance['created_at_utc']
    ended=choose('ended_at_utc',[('telemetry.ended_at_utc',telemetry.get('ended_at_utc')),('ended_at_utc',record.get('ended_at_utc'))])
    duration=choose('duration_seconds',[('telemetry.duration_seconds',telemetry.get('duration_seconds')),('duration_seconds',record.get('duration_seconds'))])
    if type(duration) not in (int,float) or not math.isfinite(duration) or duration<0:duration=None
    # Only actual observed endpoints establish a duration. Server creation alone is not a client start.
    if duration is None and timestamp(start) is not None and timestamp(ended) is not None:
        elapsed=(datetime.fromisoformat(ended.replace('Z','+00:00'))-datetime.fromisoformat(start.replace('Z','+00:00'))).total_seconds()
        if elapsed>=0 and provenance.get('started_at_utc',{}).get('field') not in {'response.created_at','telemetry.created_at'}:
            duration=elapsed;provenance['duration_seconds']={'source':str(path),'field':'observed ended_at_utc - started_at_utc'}
    model=choose('model',[('telemetry.model',telemetry.get('model')),('model',record.get('model')),('response.model',raw.get('model'))])
    tier=choose('service_tier',[('telemetry.service_tier',telemetry.get('service_tier')),('response.service_tier',raw.get('service_tier'))])
    batch=choose('batch',[('telemetry.batch',telemetry.get('batch')),('batch',record.get('batch'))])
    if batch is None and (telemetry or record.get('endpoint','').endswith('/responses')):
        batch=False;provenance['batch']={'source':str(path),'field':'synchronous Responses/observed transport; not Batch API'}
    instant=timestamp(provider_created or start)
    day=datetime.fromtimestamp(instant,timezone.utc).date().isoformat() if instant is not None else None
    day_value=record.get('billing_day_utc') or telemetry.get('billing_day_utc')
    if day is None and isinstance(day_value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',day_value):
        try:datetime.strptime(day_value,'%Y-%m-%d');day=day_value;provenance['billing_day_utc']={'source':str(path),'field':'explicit billing_day_utc'}
        except ValueError:pass
    if day is not None and 'billing_day_utc' not in provenance:
        provenance['billing_day_utc']={'source':str(path),'field':'provider created_at UTC day' if provider_created else 'observed client start UTC day'}
    attempts=telemetry.get('attempts') or []
    observed=None if tools is None else sum(i.get('type')=='web_search_call' for i in tools)
    if observed is None and isinstance(record.get('web_search_calls'),int):observed=record['web_search_calls']
    requested_tools=request.get('tools') or object_value(telemetry.get('parameters')).get('tools') or []
    web_expected=bool(web or observed or any(isinstance(t,dict) and str(t.get('type','')).startswith('web_search') for t in requested_tools))
    contexts={t.get('search_context_size') for t in requested_tools if isinstance(t,dict) and str(t.get('type','')).startswith('web_search') and t.get('search_context_size')}
    known={'api_key_id':key_id,'started_at_utc':start,'usage':usage,'service_tier':tier}
    call={**context,**{n:telemetry[n] for n in ('video_id','video_title','run_id') if telemetry.get(n)},
        'provider':record.get('provider') or telemetry.get('provider') or 'openai','script_version_id':record.get('script_version_id') or telemetry.get('script_version_id'),
        'audio_sha256':record.get('audio_sha256') or telemetry.get('audio_sha256'),'call_id':call_id,
        'stage':record.get('stage') or record.get('operation') or telemetry.get('stage') or 'unknown','step':record.get('step') or telemetry.get('step'),
        'api_key_id':key_id,'project_id':project,'model':model,'configured_model':record.get('configured_model') or telemetry.get('configured_model') or request.get('model'),
        'service_tier':tier,'batch':batch,'started_at_utc':start,'created_at_utc':provider_created,'ended_at_utc':ended,
        'start_time':instant,'billing_day_utc':day,'time_precision':'second' if instant is not None else 'day' if day else 'unknown',
        'duration_seconds':duration,'status':record.get('status') or telemetry.get('status'),'api_status':telemetry.get('status') or raw.get('status'),
        'provider_request_id':request_id,'provider_response_id':response_id,'usage':usage,'tool_usage':tool_usage,'units':units,
        'attempts':attempts,'attempt_consumption':[{'attempt_number':a.get('attempt_number'),'provider_request_id':a.get('provider_request_id'),
            'usage':a.get('usage'),'tool_usage':a.get('tool_usage'),'units':consumption(a.get('usage')),
            'billed_web_requests':billed_searches(a.get('tool_usage'))} for a in attempts],'retry_attempts_observed':max(len(attempts)-1,0) if telemetry.get('transport_attempts_observed') else None,
        'attempts_complete':telemetry.get('transport_attempts_observed',False),'tools':tools,'web_calls_observed':observed,
        'billed_web_requests':web,'web_usage_expected':web_expected,'web_context_level':next(iter(contexts)) if len(contexts)==1 else None,
        'missing_historical_fields':[n for n,v in known.items() if v is None], 'field_provenance':provenance,'source_journals':[str(path)],
        'request_observed':True if response_id or request_id or attempts or usage else None,
        'error':telemetry.get('error') or record.get('error'),'allocation_status':'Ожидаем данные','allocated_costs':{},'allocation_complete':None,
        'estimated_cost':record.get('estimated_cost'),
        'pricing_parameters': {'cache_ttl': (request.get('prompt_cache_options') or
            object_value(telemetry.get('parameters')).get('prompt_cache_options') or {}).get('ttl'),
            'web_tool_types': [t.get('type','') for t in requested_tools if isinstance(t,dict) and t.get('type','').startswith('web_search')]}}
    return call


def merge_calls(previous,current,warnings):
    previous['source_journals']=list(dict.fromkeys(previous['source_journals']+current['source_journals']))
    conflicts=previous.setdefault('conflicting_fields',[])
    if current.get('created_at_utc') and not previous.get('created_at_utc'):
        previous['billing_day_utc']=current['billing_day_utc'];previous['start_time']=current['start_time']
        previous['field_provenance']['billing_day_utc']=current['field_provenance'].get('billing_day_utc',{})
    for name in ['api_key_id','project_id','model','service_tier','batch','usage','tool_usage','billed_web_requests','billing_day_utc']:
        a,b=previous.get(name),current.get(name)
        if a is not None and b is not None and a!=b:
            # Complementary usage dictionaries can legitimately contain more fields.
            if isinstance(a,dict) and isinstance(b,dict):
                def combine(left,right):
                    merged=dict(left)
                    for k,v in right.items():
                        if k not in merged or merged[k] is None:merged[k]=v
                        elif isinstance(merged[k],dict) and isinstance(v,dict):merged[k]=combine(merged[k],v)
                        elif v is not None and merged[k]!=v:raise ValueError
                    return merged
                try:
                    previous[name]=combine(a,b)
                    previous['field_provenance'].setdefault(name,{}).setdefault('complementary_sources',[]).append(current['field_provenance'].get(name,{'source':current['source_journals'][0]}))
                    continue
                except ValueError:pass
            if name not in conflicts:conflicts.append(name)
    for name,value in current.items():
        if (previous.get(name) is None or previous.get(name)==[]) and value is not None and name not in {'allocated_costs','source_journals','field_provenance'}:
            previous[name]=value
            if name in current['field_provenance']:previous['field_provenance'][name]=current['field_provenance'][name]
    previous['web_usage_expected']=previous['web_usage_expected'] or current['web_usage_expected']
    if len(current['attempts'])>len(previous['attempts']) or len(current['attempts'])==len(previous['attempts']) and current.get('attempts_complete') and not previous.get('attempts_complete'):
        previous['attempts']=current['attempts'];previous['attempts_complete']=current['attempts_complete'];previous['retry_attempts_observed']=current['retry_attempts_observed']
    previous['units']=consumption(previous.get('usage'));previous['units']['web_requests']=billed_searches(previous.get('tool_usage'))
    previous['billed_web_requests']=previous['units']['web_requests']
    previous['missing_historical_fields']=[n for n in ['api_key_id','started_at_utc','usage','service_tier'] if previous.get(n) is None]
    if conflicts:
        previous['units']={n:None for n in previous['units']};previous['billed_web_requests']=None
        previous['missing_historical_fields'].append('conflicting_journals')
        warning='Противоречивые копии вызова '+str(previous.get('provider_response_id') or previous['call_id'])+': '+', '.join(conflicts)
        if warning not in warnings:warnings.append(warning)
    return previous


def import_calls(root,scope):
    calls=[];aliases={};warnings=[]
    roots=[root] if isinstance(root,(str,Path)) else list(root)
    paths={p for source in roots for p in Path(source).rglob('*_usage.json')}
    for path in sorted(paths):
        if path.name not in JOURNALS:continue
        try:
            records=json.loads(path.read_text())['requests']
            if not isinstance(records,list):raise ValueError
            for index,record in enumerate(records):
                if not isinstance(record,dict):raise ValueError
                if record.get('cache_hit') or record.get('request_count')==0:continue
                if record.get('mode') in {'demo','test'} or record.get('simulation') is True:continue
                if (record.get('provider') or object_value(record.get('telemetry')).get('provider') or 'openai')!='openai':continue
                # A real saved run may explicitly mark the whole directory as synthetic.
                try:
                    run=json.loads((path.parent/'run.json').read_text())
                    if run.get('mode') in {'demo','test'}:continue
                except (OSError,ValueError):pass
                call=normalize_call(record,path,index,scope)
                keys=[(label,call[label]) for label in ['provider_response_id','provider_request_id','call_id'] if call.get(label)]
                found={aliases[key] for key in keys if key in aliases}
                # Distinct actual responses cannot be collapsed by a reused logical ID.
                found={i for i in found if not (call.get('provider_response_id') and calls[i].get('provider_response_id') and call['provider_response_id']!=calls[i]['provider_response_id'])}
                if len(found)>1:
                    responses={calls[i].get('provider_response_id') for i in found if calls[i].get('provider_response_id')}
                    if len(responses)<=1:
                        keep=min(found)
                        for other in sorted(found-{keep}):
                            merge_calls(calls[keep],calls[other],warnings);calls[other]=None
                            aliases={alias:(keep if target==other else target) for alias,target in aliases.items()}
                        found={keep}
                    else:
                        warnings.append('Несогласованные идентификаторы одного вызова; связь не угадана.');call['missing_historical_fields'].append('conflicting_identity');found=set()
                if found:
                    number=next(iter(found));merge_calls(calls[number],call,warnings)
                else:number=len(calls);calls.append(call)
                for key in keys:aliases[key]=number
        except (OSError,ValueError,KeyError,TypeError,AttributeError):warnings.append(f'Не удалось полностью импортировать {path}; полнота журналов не подтверждена.')
    return [c for c in calls if c is not None],warnings


def ready_videos(root):
    result={}
    roots=[root] if isinstance(root,(str,Path)) else list(root)
    for path in sorted({p for source in roots for p in Path(source).rglob('run.json')}):
        try:
            run=json.loads(path.read_text())
            if run.get('mode') in {'test','demo'} or run.get('video_status')!='approved':continue
            video=(path.parent/run['video_path']).resolve()
            if video.suffix.lower()!='.mp4' or not video.is_relative_to(path.parent.resolve()) or not video.is_file():continue
            context=run_context(path.parent)
            result[context['video_id']]={**context,'completed_at_utc':run.get('completed_at_utc')}
        except (OSError,ValueError,KeyError,TypeError):continue
    for path in sorted({p for source in roots for p in Path(source).rglob('render_manifest.json')}):
        try:
            render=json.loads(path.read_text());quality=json.loads((path.parent/'quality_report.json').read_text());video=path.parent/'video.mp4'
            if render.get('mode')!='real' or render.get('status')!='ready' or render.get('test_only') or quality.get('ready') is not True or quality.get('technical',{}).get('passed') is not True or not video.is_file():continue
            expected=render.get('output_hashes',{}).get('video.mp4')
            if not expected or hashlib.sha256(video.read_bytes()).hexdigest()!=expected:continue
            context=run_context(path.parent);result[context['video_id']]={**context,'completed_at_utc':render.get('completed_at_utc')}
        except (OSError,ValueError,KeyError,TypeError):continue
    return result


def run_metadata(root):
    """Wall time is taken only from run lifecycle metadata, never from a sum of calls."""
    result={}
    roots=[root] if isinstance(root,(str,Path)) else list(root)
    paths={p for source in roots for name in ['run.json','render_manifest.json'] for p in Path(source).rglob(name)}
    for path in sorted(paths,key=lambda p:(str(p.parent),p.name=='render_manifest.json')):
        try:
            record=json.loads(path.read_text())
            if record.get('mode') in {'test','demo'}:continue
            context=run_context(path.parent);run_id=context['run_id'];entry=result.setdefault(run_id,{**context,
                'started_at_utc':None,'ended_at_utc':None,'duration_seconds':None,'status':None,'completed_at_utc':None,'field_provenance':{}})
            # render duration_seconds is media length, not the wall duration of a pipeline run.
            for field in ['started_at_utc','ended_at_utc','completed_at_utc','status']+(['duration_seconds'] if path.name=='run.json' else []):
                value=record.get(field)
                if value is None:continue
                if field.endswith('_at_utc') and timestamp(value) is None:continue
                if field=='duration_seconds' and (type(value) not in (int,float) or not math.isfinite(value) or value<0):continue
                entry[field]=value;entry['field_provenance'][field]={'source':str(path),'field':field}
            if entry['duration_seconds'] is None and timestamp(entry['started_at_utc']) is not None and timestamp(entry['ended_at_utc']) is not None:
                elapsed=(datetime.fromisoformat(entry['ended_at_utc'].replace('Z','+00:00'))-datetime.fromisoformat(entry['started_at_utc'].replace('Z','+00:00'))).total_seconds()
                if elapsed>=0:entry['duration_seconds']=elapsed;entry['field_provenance']['duration_seconds']={'source':str(path),'field':'observed run end - start'}
            entry['duration_status']='known' if entry['duration_seconds'] is not None else 'unknown'
            entry['duration_reason']=None if entry['duration_seconds'] is not None else 'Нет наблюдаемых границ или длительности запуска; длительности вызовов не складываются.'
        except (OSError,ValueError,TypeError):continue
    return result
