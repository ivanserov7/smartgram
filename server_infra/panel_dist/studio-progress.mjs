const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function model(job){
 const p=job.progress,known=!!p&&Number.isInteger(p.stage_index)&&p.stage_index>=0&&p.stage_index<p.stage_count;
 if(job.status==='completed')return {percent:100,title:'Ролик готов',detail:'Проверки пройдены · можно смотреть и скачивать',done:true};
 if(job.status==='queued')return {percent:0,title:'В очереди',detail:'Обработка начнётся после текущего ролика. Время ожидания заранее неизвестно.'};
 const stopped=['failed','interrupted','cancelled'].includes(job.status);
 const prefix=stopped?'Остановлено':'Сейчас';
 return {percent:known?Math.floor(100*p.stage_index/p.stage_count):null,
  title:known?`${prefix}: ${p.stage}`:stopped?'Обработка остановлена':'Запускаем обработку',
  detail:known?`${p.stage_index} из ${p.stage_count} этапов завершено. Это доля этапов, не оставшееся время.`:'Данных о текущем этапе пока нет.',
  elapsed:known?p.stage_elapsed_seconds:null,running:!stopped&&known&&p.running,
  stages:known?p.stages:[],index:known?p.stage_index:null,stopped};
}
export function markup(job,observed=performance.now(),compact=false){
 const v=model(job);
 const timer=v.elapsed==null?'':`<span class="stage-timer" data-stage-elapsed="${Number(v.elapsed)||0}" data-stage-observed="${observed}" data-stage-running="${!!v.running}">${Math.floor(v.elapsed)} с</span> <span>на этапе</span>`;
 const steps=!compact&&v.stages?.length?`<ol class="stage-steps">${v.stages.map((s,i)=>`<li class="${i<v.index?'done':i===v.index?'current':''}" ${i===v.index?'aria-current="step"':''}><span aria-hidden="true">${i<v.index?'✓':i+1}</span>${esc(s.label)}</li>`).join('')}</ol>`:'';
 const meter=v.percent==null?'':`<div class="stage-meter" role="progressbar" aria-label="Завершённые этапы" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${v.percent}" aria-valuetext="${esc(v.detail)}"><span style="width:${v.percent}%"></span></div>`;
 return `<div class="stage-progress ${v.stopped?'stopped':''}"><div class="stage-heading"><strong>${esc(v.title)}</strong>${v.percent==null?'':`<b>${v.percent}%</b>`}</div>${meter}<div class="stage-clock">${timer}</div>${steps}<p class="stage-note">${esc(v.detail)}</p></div>`;
}
export function tick(root=document,now=performance.now()){
 root.querySelectorAll('.stage-timer[data-stage-running="true"]').forEach(el=>{
  el.textContent=Math.floor(Number(el.dataset.stageElapsed)+Math.max(0,now-Number(el.dataset.stageObserved))/1000)+' с';
 });
}
export function freeze(root=document){
 tick(root);root.querySelectorAll('.stage-timer').forEach(el=>el.dataset.stageRunning='false');
 const note=root.querySelector('#studio-progress .stage-note');if(note)note.textContent='Связь с сервером потеряна. Показаны последние полученные данные.';
}
