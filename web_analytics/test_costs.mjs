import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeBilling, buildTrends} from './analytics-trends.mjs';
import {validateDataset, costStats, costLabel, csvRows, aggregate} from './analytics-core.mjs';

function raw(effective){return {schema_version:1,source:'OpenAI Costs API',calls:[{
 call_id:'c',run_id:'r',video_id:'v',video_title:'Тест',stage:'visuals',model:'gpt-6-luna',provider:'openai',
 started_at_utc:'2026-11-01T12:00:00Z',status:'completed',units:{input_tokens:100,output_tokens:50},effective_cost:effective}],
 runs:[],videos:[],allocation_details:[],completeness:{warnings:[]},sync:{}};}
function effective(status='estimated',complete=true){return {status,amount:{usd:'0.3'},actual:status==='mixed'?{usd:'0.1'}:{},estimated:status==='actual'?{}:{usd:status==='mixed'?'0.2':'0.3'},complete,pricing_version:status==='actual'?null:'test-rate',note:'usage × tariff',assumptions:[],missing:[],components:[]};}
test('estimate is visible with source label and actual token counters',()=>{
 const d=validateDataset(normalizeBilling(raw(effective())));
 assert.equal(d.calls[0].cost.amount,.3);assert.equal(d.calls[0].input_tokens,100);
 assert.equal(costLabel(d.calls[0].cost),'Оценка');assert.equal(d.calls[0].stage,'visuals');
 assert.equal(buildTrends(d).summary('effective').costStatus,'Оценка');
 assert.equal(buildTrends(d).summary('avgEffectiveCall').value,.3);
 assert.equal(buildTrends(d).summary('avgCallCost').value,null);
});
test('partial factual category plus estimate is counted once and labelled everywhere',()=>{
 const d=validateDataset(normalizeBilling(raw(effective('mixed')))),s=costStats(d.calls);
 assert.equal(s.total,.3);assert.equal(s.allocated,.1);assert.equal(s.estimated,.2);
 assert.equal(costLabel(s),'Факт + оценка');assert.equal(buildTrends(d).summary('allocated').value,.1);
 assert.equal(buildTrends(d).summary('estimated').value,.2);
 const csv=csvRows(d.calls,d.runs);assert.match(csv,/cost_label/);assert.match(csv,/Факт \+ оценка/);assert.match(csv,/test-rate/);
});
test('fact replaces previous estimate after next snapshot, including zero',()=>{
 const value=effective('actual');value.amount={usd:'0'};value.actual={usd:'0'};
 const d=validateDataset(normalizeBilling(raw(value)));
 assert.equal(d.calls[0].cost.amount,0);assert.equal(costStats(d.calls).estimated,null);
 assert.equal(costLabel(d.calls[0].cost),'Факт · распределено');
});
test('partial estimate is a partial total, never a complete average or median',()=>{
 const d=validateDataset(normalizeBilling(raw(effective('estimated',false))));
 assert.equal(costLabel(costStats(d.calls)),'Оценка · частично');
 assert.equal(buildTrends(d).summary('effective').partial,true);
 assert.equal(buildTrends(d).summary('avgEffectiveCall').value,null);
 assert.equal(aggregate(d,d.runs).median,null);
});
test('unknown data stays unknown and undated calls are not moved to today',()=>{
 const r=raw({status:'unknown',amount:null,actual:{},estimated:{},complete:false,assumptions:[],missing:['no usage'],components:[]});
 r.calls[0].started_at_utc=null;
 const d=validateDataset(normalizeBilling(r));assert.equal(costStats(d.calls).total,null);
 assert.equal(costLabel(d.calls[0].cost),'Нет данных');assert.equal(buildTrends(d).undated,1);
});
