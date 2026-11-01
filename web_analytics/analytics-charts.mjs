import {METRICS,buildTrends,finite} from './analytics-trends.mjs?v=costs3';
const esc=s=>String(s??'').replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const state={grain:'day',type:'bar',stage:'',provider:'',model:'',metrics:['effective','estimated','calls','avgEffectiveCall']};
const opt=(v,l,c)=>`<option value="${esc(v)}" ${v===c?'selected':''}>${esc(l)}</option>`;
export const formatMetric=(n,m)=>!finite(n)?'—':METRICS[m].unit==='money'?'$'+n.toLocaleString('ru-RU',{maximumFractionDigits:5}):n.toLocaleString('ru-RU',{maximumFractionDigits:METRICS[m].unit==='count'?0:2})+({seconds:' с',percent:'%'}[METRICS[m].unit]||'');
function dateLabel(s){return new Date(s+'T00:00:00Z').toLocaleDateString('ru-RU',{timeZone:'UTC',day:'2-digit',month:'short',year:'numeric'});}
function plot(points,metric,type,index){
 const valid=points.filter(p=>finite(p.value));if(!valid.length)return '<div class="trend-empty"><span>Нет данных для графика</span><p>Неизвестные значения не заменяем нулями.</p></div>';
 const width=600,height=230,left=76,right=20,top=20,bottom=38,plotW=width-left-right,plotH=height-top-bottom;
 const max=Math.max(...valid.map(p=>p.value),0.000001)*1.15,min=Math.min(0,...valid.map(p=>p.value)),span=max-min;
 const x=i=>left+plotW*(i+.5)/Math.max(points.length,1),y=v=>top+plotH*(max-v)/span;
 const bw=Math.min(70,plotW/Math.max(points.length,1)*.55),zero=y(0);
 let out=`<svg class="trend-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(METRICS[metric].label)}: ${valid.length} интервалов с данными"><title>${esc(METRICS[metric].label)}</title>`;
 for(let i=0;i<4;i++){const value=min+span*i/3,yy=y(value);out+=`<line x1="${left}" x2="${width-right}" y1="${yy}" y2="${yy}" class="trend-gridline"/><text x="${left-9}" y="${yy+4}" text-anchor="end" class="trend-axis">${esc(formatMetric(value,metric))}</text>`;}
 if(type==='line'){let segment=[];const flush=()=>{if(segment.length>1)out+=`<polyline points="${segment.join(' ')}" class="trend-line trend-color-${index}"/>`;segment=[];};points.forEach((p,i)=>{if(finite(p.value))segment.push(`${x(i)},${y(p.value)}`);else flush();});flush();}
 points.forEach((p,i)=>{if(!finite(p.value))return;const label=`${dateLabel(p.date)} · ${formatMetric(p.value,metric)} · ${p.costStatus||(/money/.test(METRICS[metric].unit)?(metric==='estimated'?'Оценка':'Факт'):'')} ${p.partial?'· частично':''} · покрытие ${p.known}/${p.total}`;
 out+=`<g tabindex="0" class="trend-point trend-color-${index}" data-trend-tip="${esc(label)}" role="img" aria-label="${esc(label)}"><title>${esc(label)}</title>`;
 out+=type==='bar'?`<rect x="${x(i)-bw/2}" y="${Math.min(y(p.value),zero)}" width="${bw}" height="${Math.max(2,Math.abs(zero-y(p.value)))}" rx="4"/>`:`<circle cx="${x(i)}" cy="${y(p.value)}" r="4.5"/>`;
 out+='</g>';});
 const indices=[...new Set([0,Math.floor((points.length-1)/2),points.length-1])];
 for(const i of indices)if(points[i])out+=`<text x="${x(i)}" y="${height-12}" text-anchor="middle" class="trend-axis">${esc(dateLabel(points[i].date))}</text>`;
 return out+'</svg>';
}
export function renderCharts(data,filter){
 const t=buildTrends(data,{...filter,...state});
 const options=Object.entries(METRICS);
 return `<section class="a-trends" aria-label="Графики аналитики"><div class="trends-heading"><div><span class="eyebrow">ДИНАМИКА ПО ДАТАМ</span><h2>Как меняются расходы и нагрузка</h2><p>Выбери метрику в каждом графике. Даты, ролик и запуск — из фильтров выше.</p></div><div class="trend-segments" role="group" aria-label="Группировка по времени">${[['day','День'],['week','Неделя'],['month','Месяц']].map(([v,l])=>`<button data-grain="${v}" aria-pressed="${state.grain===v}">${l}</button>`).join('')}</div></div>
 <div class="trend-controls"><label>Этап графиков<select id="trend-stage">${opt('','Все этапы',state.stage)}${[['research','Исследователь'],['scriptwriter','Сценарист'],['voiceover','Озвучка'],['visuals','Материалы'],['editing','Монтаж']].map(([v,l])=>opt(v,l,state.stage)).join('')}</select></label><label>Провайдер<select id="trend-provider">${opt('','Все провайдеры',state.provider)}${[...new Set(data.calls.map(c=>c.provider))].sort().map(p=>opt(p,p,state.provider)).join('')}</select></label><label>Модель<select id="trend-model">${opt('','Все модели',state.model)}${[...new Set(data.calls.map(c=>c.model).filter(Boolean))].sort().map(p=>opt(p,p,state.model)).join('')}</select></label><label>Вид графиков<select id="trend-type">${opt('bar','Столбцы',state.type)}${opt('line','Линии',state.type)}</select></label><button id="trend-reset">Сбросить графики</button></div>
 <p class="trend-context">Календарные интервалы UTC · неделя с понедельника · ${t.calls.length} вызовов с датой${t.undated?` · ${t.undated} без даты: только в журнале`:''}. ${filter.from&&filter.to&&filter.from>filter.to?'Начальная дата позже конечной.':t.keys.length===1?'В данных один интервал — для тренда нужны новые дни.':''}</p>
 <div class="trend-cards">${state.metrics.map((metric,i)=>{const m=METRICS[metric],points=t.series(metric),s=t.summary(metric),restricted=m.ledger&&t.scoped;
 return `<article class="trend-card"><div class="trend-card-head"><label for="trend-metric-${i}">Метрика ${i+1}</label><select id="trend-metric-${i}" data-metric-index="${i}">${options.map(([v,k])=>opt(v,k.label,metric)).join('')}</select></div><div class="trend-value"><strong>${restricted?'—':formatMetric(s.value,metric)}</strong><span>${s.costStatus||(m.unit==='money'?(metric==='estimated'?'Оценка':'Факт'):'')} ${s.partial?'· частично':''} · ${/avg/.test(metric)?'среднее за период':metric==='p90Duration'?'P90 за период':metric==='models'?'уникальных за период':metric==='errorRate'?'за весь период':'всего за период'}</span>${!restricted&&s.total?`<small>Данные: ${s.known} / ${s.total}</small>`:''}</div>${restricted?'<div class="trend-empty"><span>Счёт OpenAI доступен по всему проекту</span><p>Для выбранного ролика, этапа или модели выбери «Распределённые расходы».</p></div>':plot(points,metric,state.type,i)}<div class="trend-tip" aria-live="polite">${points.some(p=>finite(p.value))?'Наведи на значение или выбери его клавишей Tab':'Ожидаем данные'}</div><p class="trend-note">${esc(m.note)}</p></article>`;}).join('')}</div>
 <details class="trend-data"><summary>Точные значения по интервалам</summary><div class="table-wrap"><table class="a-table"><thead><tr><th>Начало интервала, UTC</th>${state.metrics.map(m=>`<th>${esc(METRICS[m].label)}</th>`).join('')}</tr></thead><tbody>${t.keys.length?t.keys.map((d,i)=>`<tr><td>${dateLabel(d)}</td>${state.metrics.map(m=>`<td>${formatMetric(t.series(m)[i]?.value,m)} ${t.series(m)[i]?.costStatus||(METRICS[m].unit==='money'?(m==='estimated'?'Оценка':'Факт'):'')} ${t.series(m)[i]?.partial?'· частично':''}</td>`).join('')}</tr>`).join(''):'<tr><td colspan="5">Нет данных в выбранном периоде.</td></tr>'}</tbody></table></div></details>
 <p class="trend-footer">Средние считаются из исходных сумм и количества. Пустой интервал означает отсутствие данных. Начисления OpenAI и их распределение — одна сумма в разных представлениях, складывать их нельзя.</p></section>`;
}
export function bindCharts(root,render){
 root.querySelectorAll('[data-grain]').forEach(b=>b.onclick=()=>{state.grain=b.dataset.grain;render();});
 ['stage','provider','model','type'].forEach(k=>root.querySelector('#trend-'+k).onchange=e=>{state[k]=e.target.value;render();});
 root.querySelectorAll('[data-metric-index]').forEach(s=>s.onchange=e=>{state.metrics[Number(s.dataset.metricIndex)]=e.target.value;render();});
 root.querySelector('#trend-reset').onclick=()=>{Object.assign(state,{grain:'day',type:'bar',stage:'',provider:'',model:'',metrics:['effective','estimated','calls','avgEffectiveCall']});render();};
 root.querySelectorAll('[data-trend-tip]').forEach(el=>{const set=()=>{el.closest('.trend-card').querySelector('.trend-tip').textContent=el.dataset.trendTip;};el.addEventListener('pointerenter',set);el.addEventListener('focus',set);});
}
