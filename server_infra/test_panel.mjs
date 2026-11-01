import test from 'node:test';import assert from 'node:assert/strict';
import fs from 'node:fs';import os from 'node:os';import path from 'node:path';import crypto from 'node:crypto';
import {createPanel,passwordRecord} from './panel_app.mjs';
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function setup(){
 const temp=fs.mkdtempSync(path.join(os.tmpdir(),'smartgram-drain-test-')),data=path.join(temp,'data');
 const runner=path.join(temp,'runner.mjs');fs.writeFileSync(runner,'setTimeout(()=>process.exit(1),350);');
 const proof=path.join(temp,'ready.json');fs.writeFileSync(proof,JSON.stringify({ready:true,contract_version:1,release:fs.realpathSync(temp),adapter_sha256:crypto.createHash('sha256').update(fs.readFileSync(runner)).digest('hex')}));
 const config={origin:'https://smartgram.pro',username:'test-owner',password:passwordRecord('test-password-only'),data_dir:data,runner_command:[process.execPath,runner],runner_cwd:temp,runner_contract:1,runner_readiness_file:proof};
 const panel=createPanel(config);await new Promise(r=>panel.server.listen(0,'127.0.0.1',r));const origin='http://127.0.0.1:'+panel.server.address().port;
 const headers={'Content-Type':'application/json',Origin:config.origin,'X-Smartgram-Request':'1'};
 const login=await fetch(origin+'/api/login',{method:'POST',headers,body:JSON.stringify({username:config.username,password:'test-password-only'})});headers.Cookie=login.headers.get('set-cookie').split(';')[0];
 const send=(url,body)=>fetch(origin+url,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined});
 const request={topic:'Offline test',target_seconds:30,output_size:'720x1280',audience:'general',tone:'clear',subtitles:true,notes:''};
 return {temp,data,runner,proof,panel,send,request,close:()=>panel.close()};
}
test('existing runner file cannot enable generation without a matching readiness proof',async()=>{
 const t=await setup();try{fs.unlinkSync(t.proof);assert.equal((await(await t.send('/api/capabilities')).json()).generation_ready,false);assert.equal((await t.send('/api/jobs',{request:t.request,idempotency_key:crypto.randomUUID()})).status,409);}finally{await t.close();}
});
test('changed adapter invalidates the readiness proof',async()=>{
 const t=await setup();try{assert.equal((await(await t.send('/api/capabilities')).json()).generation_ready,true);fs.appendFileSync(t.runner,'\n//changed');assert.equal((await(await t.send('/api/capabilities')).json()).generation_ready,false);}finally{await t.close();}
});
test('unsupported form value is rejected before creating a paid job',async()=>{
 const t=await setup();try{const r=await t.send('/api/jobs',{request:{...t.request,subtitles:false},idempotency_key:crypto.randomUUID()});assert.equal(r.status,422);assert.match((await r.json()).error,/субтитров/);assert.equal((await(await t.send('/api/jobs')).json()).jobs.length,0);}finally{await t.close();}
});
test('maintenance blocks new admission while preserving queued jobs and allowing the active job to finish',async()=>{
 const t=await setup();try{
  assert.equal((await t.send('/api/jobs',{request:t.request,idempotency_key:crypto.randomUUID()})).status,202);
  assert.equal((await t.send('/api/jobs',{request:t.request,idempotency_key:crypto.randomUUID()})).status,202);
  fs.writeFileSync(path.join(t.data,'maintenance.json'),'{}');
  assert.equal((await t.send('/api/jobs',{request:t.request,idempotency_key:crypto.randomUUID()})).status,503);
  await wait(700);const jobs=(await(await t.send('/api/jobs')).json()).jobs;
  assert.equal(jobs.filter(j=>j.status==='queued').length,1);assert.equal(jobs.filter(j=>j.status==='failed').length,1);
  assert.equal((await(await t.send('/api/capabilities')).json()).maintenance,true);
 }finally{await t.close();}
});
