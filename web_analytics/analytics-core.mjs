export const STAGES = [
  ['research','Исследователь'],['scriptwriter','Сценарист'],['voiceover','Озвучка'],
  ['visuals','Визуальные материалы'],['editing','Монтаж и проверка']
];
export const STEPS = {source_search:'Поиск источников',SourceReviews:'Проверка источников',ExtractedFacts:'Извлечение фактов',EvidenceChecks:'Проверка доказательств',ResearchAssessment:'Оценка исследования',comparison_text_check:'Проверка тестовой озвучки',comparison_transcription:'Расшифровка тестовой озвучки',comparison_generation:'Тест генерации речи',collection:'Поиск и сбор фактов',assessment:'Оценка достаточности',write:'Текст и визуальный замысел',check:'Проверка сценария',synthesize:'Генерация речи',measure_duration:'Измерение длительности',finalize_timing:'Таймкоды для монтажа',transcribe:'Расшифровка и таймкоды',audio_check:'Проверка текста и таймкоды',audio_confirm:'Перепроверка аудио',visual_queries:'Подготовка поиска',visual_search:'Сбор кандидатов',visual_select:'Выбор изображения и показа',visual_prepare:'Подготовка кадрирования',visual_review:'Проверка готового показа',search:'Поиск материалов',vision:'Проверка изображений',render:'Рендер',save:'Сохранение',video_render:'Сборка видеоряда',subtitle_segment:'Деление субтитров',subtitle_layout:'Размещение субтитров',compose:'Звук и экспорт',final_check:'Проверка готового ролика'};
export const number = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
export function validateDataset(data) {
  if (!data || data.schema_version !== 1 || !Array.isArray(data.runs) || !Array.isArray(data.calls)) throw Error('Нужен JSON аналитики Smartgram, schema_version: 1, runs и calls.');
  if(data.runs.length > 20000 || data.calls.length > 100000) throw Error('Файл слишком большой: максимум 20 000 запусков и 100 000 вызовов.');
  const ids=new Set(), calls=new Set();
  const str=(x)=>typeof x==='string' && x.length>0 && x.length<=2000;
  for(const r of data.runs) {
    if(!str(r.id)||ids.has(r.id)||!str(r.title)||!str(r.video_id)||(r.started_at!=null&&(!str(r.started_at)||!Number.isFinite(Date.parse(r.started_at))))) throw Error('Некорректный или повторяющийся запуск.');
    if(typeof r.video_completed!=='boolean'||!str(r.status)||typeof r.is_demo!=='boolean') throw Error('У запуска должны быть status, video_completed и is_demo.');
    if(r.duration_seconds!=null&&!number(r.duration_seconds))throw Error('Неверная длительность запуска.');
    if(r.ended_at!=null&&(!Number.isFinite(Date.parse(r.ended_at))||Date.parse(r.ended_at)<Date.parse(r.started_at)))throw Error('Неверное время окончания.');
    ids.add(r.id);
  }
  for(const c of data.calls) {
    if(!str(c.id)||calls.has(c.id)||!ids.has(c.run_id)||!(c.stage==='delivery'||STAGES.some(s=>s[0]===c.stage))||!str(c.provider)||!str(c.status)||!str(c.step))throw Error('Некорректный вызов или ссылка на неизвестный запуск.');
    for(const k of ['duration_seconds','input_tokens','output_tokens','cached_tokens','cache_write_tokens','reasoning_tokens','web_requests','web_actions'])if(c[k]!=null&&!number(c[k]))throw Error('Неверное числовое поле вызова: '+k);
    if(c.attempt!=null&&(!Number.isInteger(c.attempt)||c.attempt<1))throw Error('attempt должен быть целым числом от 1.');
    if(!c.cost||!['billed','allocated','estimated','mixed','unknown'].includes(c.cost.status))throw Error('Укажи источник стоимости: billed, estimated или unknown.');
    if(c.cost.status==='unknown'&&c.cost.amount!=null)throw Error('Неизвестная стоимость должна быть null.');
    if(c.cost.status!=='unknown'&&(!(typeof c.cost.amount==='number'&&Number.isFinite(c.cost.amount))||(c.cost.status==='estimated'&&c.cost.amount<0)||c.cost.currency!=='USD'))throw Error('Нужна корректная стоимость в USD. Отрицательная оценка недопустима; кредит требует фактического основания.');
    if(['billed','allocated','mixed'].includes(c.cost.status)&&!str(c.cost.evidence))throw Error('Для списания требуется cost.evidence — ссылка или идентификатор записи провайдера.');
    if(['estimated','mixed'].includes(c.cost.status)&&!str(c.cost.pricing_version))throw Error('Для расчёта требуется cost.pricing_version.');
    calls.add(c.id);
  }
  if(data.billing!=null){
    if(!Array.isArray(data.billing)||data.billing.length>100000)throw Error('Некорректный журнал начислений.');
    const rowIds=new Set();
    for(const row of data.billing){
      if(!row.id||rowIds.has(row.id)||!Number.isFinite(Date.parse(row.date))||!['amount','allocated','unallocated'].every(k=>typeof row[k]==='number'&&Number.isFinite(row[k]))||typeof row.currency!=='string')throw Error('Некорректная или повторяющаяся строка биллинга.');
      if(Math.abs(row.amount-row.allocated-row.unallocated)>1e-9)throw Error('Начисления не сходятся с распределением.');
      rowIds.add(row.id);
    }
  }
  return data;
}
export function mergeDatasets(base,next) {
  validateDataset(base);validateDataset(next);
  const runs=new Map(base.runs.map(r=>[r.id,r]));next.runs.forEach(r=>runs.set(r.id,r));
  const calls=new Map(base.calls.map(c=>[c.id,c]));next.calls.forEach(c=>calls.set(c.id,c));
  return validateDataset({...base,...next,runs:[...runs.values()],calls:[...calls.values()]});
}
const dayMoscow=s=>s?new Intl.DateTimeFormat('sv-SE',{timeZone:'UTC',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(s)):'';
export function selectRuns(data,filter={}) {
  return data.runs.filter(r=>!r.is_demo && (!filter.query||r.title.toLowerCase().includes(filter.query.toLowerCase())) && (!filter.run||r.id===filter.run) && (!filter.video||r.video_id===filter.video) &&
    (!filter.from||dayMoscow(r.started_at)>=filter.from) && (!filter.to||(r.started_at&&dayMoscow(r.started_at)<=filter.to)));
}
export function sumKnown(values) {const known=values.filter(number); return known.length?known.reduce((a,b)=>a+b,0):null;}
export function costStats(calls) {
  const valid=n=>typeof n==='number'&&Number.isFinite(n);
  const sum=values=>values.some(valid)?values.filter(valid).reduce((a,b)=>a+b,0):null;
  const known=calls.filter(c=>valid(c.cost.amount));
  const actual=calls.map(c=>c.cost.actual_amount??(['billed','allocated'].includes(c.cost.status)?c.cost.amount:null));
  const estimates=calls.map(c=>c.cost.estimated_amount??(c.cost.status==='estimated'?c.cost.amount:null));
  return {total:sum(known.map(c=>c.cost.amount)),billed:sum(calls.filter(c=>c.cost.status==='billed').map(c=>c.cost.amount)),
    allocated:sum(actual),estimated:sum(estimates),unknown:calls.length-known.length,partial:known.filter(c=>c.cost.complete===false).length,
    known:known.length,count:calls.length};
}
export function costLabel(value) {
  if('status' in value){const label=({billed:'Факт',allocated:'Факт · распределено',estimated:'Оценка',mixed:'Факт + оценка',unknown:'Нет данных'})[value.status]||'Нет данных';return label+(value.complete===false&&value.amount!=null?' · частично':'');}
  const fact=value.allocated!=null||value.billed!=null,estimate=value.estimated!=null;
  return (fact&&estimate?'Факт + оценка':fact?'Факт':estimate?'Оценка':'Нет данных')+(value.unknown||value.partial?' · частично':'');
}
export function percentile(values,p) {const v=values.filter(number).sort((a,b)=>a-b);return v.length?v[Math.max(0,Math.ceil(p*v.length)-1)]:null;}
export function aggregate(data,runs) {
  const ids=new Set(runs.map(r=>r.id)),calls=data.calls.filter(c=>ids.has(c.run_id)).map(c=>c.stage==='delivery'?{...c,stage:'editing',original_stage:'delivery'}:c),cost=costStats(calls);
  // Completed-video economics includes all selected attempts, failures and unfinished work.
  const completed=new Set(runs.filter(r=>r.video_completed).map(r=>r.video_id)).size;
  const groups=new Map(runs.map(r=>[r.id,[]]));calls.forEach(c=>groups.get(c.run_id).push(c));
  const perRun=[...groups.values()].map(costStats).filter(s=>s.count>0&&s.unknown===0&&s.partial===0).map(s=>s.total);
  return {calls,cost,completed,runCount:runs.length,avgRun:cost.total!=null&&runs.length?cost.total/runs.length:null,
    avgVideo:cost.total!=null&&completed?cost.total/completed:null,median:percentile(perRun,.5),p90:percentile(perRun,.9),
    avgTime:runs.some(r=>number(r.duration_seconds))?runs.filter(r=>number(r.duration_seconds)).reduce((s,r)=>s+r.duration_seconds,0)/runs.filter(r=>number(r.duration_seconds)).length:null,
    errors:calls.filter(c=>c.status==='failed').length,retries:calls.filter(c=>c.attempt>1).length,
    input:sumKnown(calls.map(c=>c.input_tokens)),output:sumKnown(calls.map(c=>c.output_tokens)),cached:sumKnown(calls.map(c=>c.cached_tokens))};
}
export function csvRows(calls,runs) {
  const names=new Map(runs.map(r=>[r.id,r.title]));
  const safe=x=>{let s=String(x??'');if(/^[=+\-@\t\r]/.test(s))s="'"+s;return '"'+s.replaceAll('"','""')+'"';};
  const rows=[['run_id','title','call_id','stage','step','provider','model','status','attempt','cost_usd','cost_status','cost_label','cost_complete','actual_usd','estimated_usd','pricing_version','duration_seconds','input_tokens','output_tokens','cached_tokens','web_requests']];
  calls.forEach(c=>rows.push([c.run_id,names.get(c.run_id),c.id,c.stage,c.step,c.provider,c.model,c.status,c.attempt,c.cost.amount,c.cost.status,costLabel(c.cost),c.cost.complete,c.cost.actual_amount,c.cost.estimated_amount,c.cost.pricing_version,c.duration_seconds,c.input_tokens,c.output_tokens,c.cached_tokens,c.web_requests]));
  return '\ufeff'+rows.map(r=>r.map(safe).join(',')).join('\r\n');
}
