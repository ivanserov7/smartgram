import test from 'node:test';import assert from 'node:assert/strict';
import {jobProgress,progressStages} from './panel_app.mjs';
import {model,markup,tick,freeze} from './panel_dist/studio-progress.mjs';
const raw={stage_id:'assets',stage_started_at_ms:100000,stage_elapsed_seconds:9,status:'running'};
test('stage timer comes from saved start, survives polling, and failed timer freezes',()=>{
 assert.equal(jobProgress({status:'running'},raw,120000).stage_elapsed_seconds,20);
 assert.equal(jobProgress({status:'running'},raw,125000).stage_elapsed_seconds,25);
 assert.equal(jobProgress({status:'failed'},raw,999000).stage_elapsed_seconds,9);
});
test('running final stage cannot show 100%; only accepted completed job can',()=>{
 const p=jobProgress({status:'running'},{...raw,stage_id:'delivery',status:'completed'},120000);
 assert.equal(model({status:'running',progress:p}).percent,80);
 assert.equal(model({status:'completed',progress:null}).percent,100);
});
test('unknown legacy stages show no invented percentage, queued job does not pretend to render',()=>{
 assert.equal(jobProgress({status:'running'},{stage:'arbitrary'},120000),null);
 assert.equal(model({status:'failed'}).percent,null);assert.equal(model({status:'queued'}).percent,0);
});
test('clock is smooth locally, freezes when disconnected and markup escapes labels',()=>{
 const el={dataset:{stageElapsed:'12',stageObserved:'1000',stageRunning:'true'},textContent:''};
 const note={textContent:''};const root={querySelectorAll:()=>[el],querySelector:()=>note};
 tick(root,4500);assert.equal(el.textContent,'15 с');freeze(root);assert.equal(el.dataset.stageRunning,'false');assert.match(note.textContent,/Связь/);
 const progress=jobProgress({status:'running'},raw,120000);progress.stage='<script>';
 const html=markup({status:'running',progress},0);assert.ok(html.includes('&lt;script&gt;'));assert.ok(!html.includes('<script>'));
 assert.equal(progressStages.length,5);
});
