// All chart buckets use UTC: provider daily charges cannot be split into local days.
export const finite = x => typeof x === 'number' && Number.isFinite(x);
const numeric = x => x == null || x === '' ? null : Number.isFinite(Number(x)) ? Number(x) : null;
export const day = x => x && Number.isFinite(Date.parse(x)) ? new Date(x).toISOString().slice(0,10) : null;
export function bucket(x, grain='day') {
  const d=day(x); if(!d)return null;
  if(grain==='month')return d.slice(0,7)+'-01';
  if(grain==='week'){const t=new Date(d+'T00:00:00Z');t.setUTCDate(t.getUTCDate()-(t.getUTCDay()+6)%7);return day(t.toISOString());}
  return d;
}
export function normalizeBilling(raw) {
  if(typeof raw?.source!=='string'||!Array.isArray(raw.allocation_details))return raw;
  if(!Array.isArray(raw.calls)||raw.calls.length>100000)throw Error('Некорректный журнал биллинга.');
  const runs=new Map();
  const stages=s=>['research','scriptwriter','voiceover','visuals','editing'].includes(s)?s:/^(collection|assessment|source_search|SourceReviews|ExtractedFacts|EvidenceChecks|ResearchAssessment)$/.test(s)?'research':/^script|^write$|^check$/.test(s)?'scriptwriter':/audio|synth|transcrib|comparison|voice/.test(s)?'voiceover':/visual|vision|image/.test(s)?'visuals':'editing';
  const calls=raw.calls.map(c=>{
    if(!c.call_id||!c.run_id)throw Error('У вызова нет идентификатора.');
    const start=day(c.started_at_utc)?c.started_at_utc:null;
    if(!runs.has(c.run_id))runs.set(c.run_id,{id:c.run_id,video_id:c.video_id||c.run_id,title:c.video_title||'Без названия',started_at:start,ended_at:null,duration_seconds:null,status:'unknown',video_completed:raw.videos?.some(v=>v.video_id===c.video_id&&v.ready)===true,is_demo:false});
    const run=runs.get(c.run_id);if(start&&(!run.started_at||start<run.started_at))run.started_at=start;
    const amount=numeric(c.allocated_costs?.usd),u=c.units||{},e=c.effective_cost;
    const cost=e?{status:({actual:'allocated',estimated:'estimated',mixed:'mixed',unknown:'unknown'})[e.status],amount:numeric(e.amount?.usd),currency:'USD',complete:e.complete,actual_amount:numeric(e.actual?.usd),estimated_amount:numeric(e.estimated?.usd),pricing_version:e.pricing_version,evidence:e.actual&&Object.keys(e.actual).length?'Costs API · распределение по потреблению':null,note:[e.note,...e.assumptions,...e.missing].join(' '),components:e.components}:
      {status:amount==null?'unknown':'allocated',amount,currency:'USD',complete:c.allocation_complete===true,evidence:amount==null?null:'Costs API · распределение по потреблению'};
    return {id:c.call_id,run_id:c.run_id,stage:stages(c.stage||''),step:({script_writing:'write',script_verification:'check'})[c.stage]||c.stage||'unknown',provider:({openai:'OpenAI',google:'Google',deepgram:'Deepgram'})[c.provider]||c.provider||'OpenAI',model:c.model||null,status:c.status==='error'?'failed':c.status||'unknown',started_at:start,ended_at:c.ended_at_utc||null,duration_seconds:numeric(c.duration_seconds),attempt:null,retry_count:numeric(c.retry_attempts_observed),input_tokens:numeric(u.input_tokens),output_tokens:numeric(u.output_tokens),cached_tokens:numeric(u.cached_tokens),cache_write_tokens:numeric(u.cache_write_tokens),web_requests:numeric(c.billed_web_requests),cost,error:c.error?String(typeof c.error==='string'?c.error:c.error.type||'Ошибка вызова'):null};
  });
  for(const meta of raw.runs||[]){
    const r=runs.get(meta.run_id||meta.id);if(!r)continue;
    if(day(meta.started_at_utc))r.started_at=meta.started_at_utc;
    if(day(meta.ended_at_utc))r.ended_at=meta.ended_at_utc;
    if(finite(meta.duration_seconds)&&meta.duration_seconds>=0)r.duration_seconds=meta.duration_seconds;
    if(typeof meta.status==='string'&&meta.status)r.status=meta.status;
  }
  const billing=raw.allocation_details.map(r=>{
    const amount=numeric(r.provider_amount),shares=(r.shares||[]).map(s=>numeric(s.amount));
    if(!finite(amount)||!Number.isFinite(r.start_time)||shares.some(s=>!finite(s)))throw Error('Некорректная статья биллинга.');
    const allocated=shares.reduce((a,b)=>a+b,0);
    return {id:r.billing_row_id,date:new Date(r.start_time*1000).toISOString(),amount,allocated,unallocated:amount-allocated,currency:r.currency,reason:r.reason||null};
  });
  return {schema_version:1,generated_at:raw.sync?.last_success_at_utc,source:{kind:'report_snapshot',label:'Costs API и журналы исследователя, сценариста, озвучки',url:raw.report_url||null},runs:[...runs.values()],calls,billing,billing_period:raw.period,warnings:raw.completeness?.warnings||[],billing_error:raw.sync?.error||null};
}
export const METRICS = {
  billed:{label:'Факт · начислено OpenAI',unit:'money',ledger:true,note:'Сумма опубликованных начислений OpenAI. Свежие начисления могут ещё отсутствовать.'},
  unallocated:{label:'Факт · не распределено',unit:'money',ledger:true,note:'Часть счёта OpenAI, ещё не связанная с вызовами.'},
  allocated:{label:'Факт · распределено',unit:'money',note:'Известная часть фактических расходов, связанная с вызовами. Это не отдельный счёт за request_id.'},
  estimated:{label:'Оценка по тарифам',unit:'money',note:'Отдельная расчётная оценка. Не входит в начисления OpenAI.'},
  effective:{label:'Стоимость · факт или оценка',unit:'money',note:'Факт по каждой категории при наличии, иначе оценка по usage. Частичная цена явно отмечена.'},
  avgEffectiveCall:{label:'Среднее вызова · факт или оценка',unit:'money',note:'Среднее только полностью учтённых вызовов; источник указан рядом с суммой.'},
  avgCallCost:{label:'Средняя стоимость вызова',unit:'money',note:'Подтверждённые и распределённые расходы / все вызовы. Доступно только при полном покрытии ценой.'},
  avgRunCost:{label:'Средняя стоимость запуска',unit:'money',note:'Расходы всех вызовов запусков, начавшихся в интервале / число таких запусков. Только при полном покрытии.'},
  avgVideoCost:{label:'Средняя стоимость готового ролика',unit:'money',note:'Расходы запусков выбранного интервала / уникальные готовые ролики. Включает ошибки и незавершённые попытки; только при полном покрытии.'},
  calls:{label:'Количество вызовов',unit:'count',note:'Записанные обращения, включая ошибки. На графике — по времени вызова.'},
  runs:{label:'Количество запусков',unit:'count',note:'Уникальные запуски по дате начала. Дубли вызовов не увеличивают число запусков.'},
  videos:{label:'Готовые ролики',unit:'count',note:'Уникальные готовые ролики среди запусков, начавшихся в интервале.'},
  models:{label:'Количество моделей',unit:'count',note:'Уникальные пары провайдер + модель. За неделю и месяц считаются заново, не складываются по дням.'},
  tokens:{label:'Объём токенов',unit:'count',note:'Вход + выход по вызовам с обоими счётчиками. Кеш и reasoning повторно не добавляются.'},
  input:{label:'Входные токены',unit:'count',note:'Сумма известных входных токенов, включая кеш.'},
  output:{label:'Выходные токены',unit:'count',note:'Сумма известных выходных токенов.'},
  cached:{label:'Токены из кеша',unit:'count',note:'Сумма известных токенов чтения кеша.'},
  avgDuration:{label:'Среднее время вызова',unit:'seconds',note:'Сумма известных длительностей / число вызовов с известной длительностью.'},
  p90Duration:{label:'P90 времени вызова',unit:'seconds',note:'90% измеренных вызовов быстрее этого значения.'},
  avgRunDuration:{label:'Среднее время запуска',unit:'seconds',note:'Среднее измеренное время запуска. Не заменяется суммой параллельных вызовов.'},
  errors:{label:'Количество ошибок',unit:'count',note:'Записанные вызовы со статусом failed или error.'},
  errorRate:{label:'Доля ошибок',unit:'percent',note:'Ошибки / завершённые вызовы с известным результатом.'},
  retries:{label:'Повторные попытки',unit:'count',note:'Сумма наблюдаемых повторов. Неизвестные попытки не считаются нулевыми.'}
};
const sum=a=>a.length?a.reduce((s,x)=>s+x,0):null;
function measure(metric,calls,runs,ledger,allCalls) {
  let value=null,known=0,total=calls.length;
  const values=key=>calls.map(c=>c[key]).filter(finite);
  const actual=c=>['billed','allocated','mixed'].includes(c.cost?.status)&&finite(c.cost.actual_amount??c.cost.amount);
  let costStatus=null,partial=false;
  if(METRICS[metric].ledger){total=ledger.length;known=total;value=sum(ledger.map(r=>metric==='billed'?r.amount:r.unallocated));}
  else if(['allocated','estimated'].includes(metric)){
    const amounts=calls.map(c=>metric==='estimated'?(c.cost?.estimated_amount??(c.cost?.status==='estimated'?c.cost.amount:null)):(c.cost?.actual_amount??(['billed','allocated'].includes(c.cost?.status)?c.cost.amount:null))).filter(finite);known=amounts.length;value=sum(amounts);
  } else if(['effective','avgEffectiveCall'].includes(metric)){
    const c=calls.filter(c=>finite(c.cost?.amount));known=c.length;partial=c.length!==calls.length||c.some(c=>c.cost.complete===false);
    const facts=c.some(c=>['billed','allocated','mixed'].includes(c.cost.status)),estimates=c.some(c=>['estimated','mixed'].includes(c.cost.status));
    costStatus=facts&&estimates?'Факт + оценка':facts?'Факт':estimates?'Оценка':'Нет данных';
    value=metric==='effective'?sum(c.map(c=>c.cost.amount)):(!partial&&c.length?sum(c.map(c=>c.cost.amount))/c.length:null);
  }
  else if(['avgCallCost','avgRunCost','avgVideoCost'].includes(metric)){
    const ids=new Set(runs.map(r=>r.id));const c=metric==='avgCallCost'?calls:allCalls.filter(c=>ids.has(c.run_id));
    total=c.length;known=c.filter(x=>['billed','allocated'].includes(x.cost?.status)&&x.cost.complete!==false).length;
    const n=metric==='avgCallCost'?c.length:metric==='avgRunCost'?runs.length:new Set(runs.filter(r=>r.video_completed).map(r=>r.video_id)).size;
    value=n&&c.length&&known===c.length?sum(c.map(c=>c.cost.amount))/n:null;
  } else if(metric==='calls'){known=total;value=calls.length||null;}
  else if(metric==='runs'||metric==='videos'){total=runs.length;known=total;value=total?(metric==='runs'?total:new Set(runs.filter(r=>r.video_completed).map(r=>r.video_id)).size):null;}
  else if(metric==='models'){known=calls.filter(c=>c.model).length;value=known?new Set(calls.filter(c=>c.model).map(c=>c.provider+'/'+c.model)).size:null;}
  else if(['tokens','input','output','cached'].includes(metric)){
    const v=metric==='tokens'?calls.filter(c=>finite(c.input_tokens)&&finite(c.output_tokens)).map(c=>c.input_tokens+c.output_tokens):values({input:'input_tokens',output:'output_tokens',cached:'cached_tokens'}[metric]);known=v.length;value=sum(v);
  } else if(['avgDuration','p90Duration','avgRunDuration'].includes(metric)){
    const v=(metric==='avgRunDuration'?runs:calls).map(c=>c.duration_seconds).filter(finite).sort((a,b)=>a-b);total=metric==='avgRunDuration'?runs.length:total;known=v.length;
    value=v.length?(metric==='p90Duration'?v[Math.ceil(v.length*.9)-1]:sum(v)/v.length):null;
  } else if(metric==='errors'||metric==='errorRate'){
    const c=calls.filter(c=>['completed','failed','error','succeeded'].includes(c.status));known=c.length;const n=c.filter(c=>['failed','error'].includes(c.status)).length;value=c.length?(metric==='errors'?n:100*n/c.length):null;
  } else if(metric==='retries'){const v=calls.map(c=>finite(c.retry_count)?c.retry_count:finite(c.attempt)?Number(c.attempt>1):null).filter(finite);known=v.length;value=sum(v);}
  return {value,known,total,costStatus,partial};
}
export function buildTrends(data,{from='',to='',video='',run='',query='',stage='',provider='',model='',grain='day'}={}){
  const inDate=x=>{const d=day(x);return d&&(!from||d>=from)&&(!to||d<=to);};
  const selectedRuns=data.runs.filter(r=>!r.is_demo&&(!video||r.video_id===video)&&(!run||r.id===run)&&(!query||r.title.toLowerCase().includes(query.toLowerCase())));
  const ids=new Set(selectedRuns.map(r=>r.id));
  const allCalls=data.calls.filter(c=>ids.has(c.run_id)&&(!stage||c.stage===stage)&&(!provider||c.provider===provider)&&(!model||c.model===model));
  const activeIds=new Set(allCalls.map(c=>c.run_id));
  const runs=selectedRuns.filter(r=>inDate(r.started_at)&&(!(stage||provider||model)||activeIds.has(r.id)));
  const calls=allCalls.filter(c=>inDate(c.started_at));
  const scoped=!!(video||run||query||stage||provider||model);
  const ledger=scoped?[]:(data.billing||[]).filter(r=>r.currency?.toLowerCase()==='usd'&&inDate(r.date));
  const events=[...calls.map(c=>c.started_at),...runs.map(r=>r.started_at),...ledger.map(r=>r.date)];
  let start=from||events.map(day).sort()[0],end=to||events.map(day).sort().at(-1);
  const keys=[];
  if(start&&end&&start<=end){let cur=bucket(start,grain),last=bucket(end,grain);for(let i=0;cur<=last&&i<4000;i++){keys.push(cur);const d=new Date(cur+'T00:00:00Z');if(grain==='month')d.setUTCMonth(d.getUTCMonth()+1);else d.setUTCDate(d.getUTCDate()+(grain==='week'?7:1));cur=day(d.toISOString());}}
  const grouped=(items,getDate)=>{const m=new Map();for(const item of items){const k=bucket(getDate(item),grain);if(!m.has(k))m.set(k,[]);m.get(k).push(item);}return m;};
  const cg=grouped(calls,c=>c.started_at),rg=grouped(runs,r=>r.started_at),lg=grouped(ledger,r=>r.date);
  const cache=new Map();
  return {scoped,undated:allCalls.filter(c=>!day(c.started_at)).length,calls,runs,ledger,keys,truncated:keys.length===4000,
    series:metric=>{if(!cache.has(metric))cache.set(metric,keys.map(date=>({date,...measure(metric,cg.get(date)||[],rg.get(date)||[],lg.get(date)||[],allCalls)})));return cache.get(metric);},
    summary:metric=>measure(metric,calls,runs,ledger,allCalls)};
}
