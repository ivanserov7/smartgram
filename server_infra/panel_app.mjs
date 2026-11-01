// Small, single-owner panel. No npm dependencies. Node >= 18.19.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const allowed={target_seconds:[30,45,60],output_size:['720x1280','1080x1920'],audience:['general','teen'],tone:['clear','story']};
export function validateRequest(v){
 if(!v||typeof v!=='object'||Array.isArray(v))throw Error('Нужны параметры ролика.');
 const keys=['topic',...Object.keys(allowed),'subtitles','notes'];
 if(Object.keys(v).some(k=>!keys.includes(k)))throw Error('Неизвестный параметр ролика.');
 if(typeof v.topic!=='string'||!v.topic.trim()||v.topic.length>300)throw Error('Тема должна содержать от 1 до 300 символов.');
 if(typeof v.notes!=='string'||v.notes.length>2000)throw Error('Пожелания: максимум 2000 символов.');
 for(const [k,values] of Object.entries(allowed))if(!values.includes(v[k]))throw Error('Недопустимый параметр: '+k);
 if(typeof v.subtitles!=='boolean')throw Error('Укажи, нужны ли субтитры.');
 return Object.fromEntries(keys.map(k=>[k,typeof v[k]==='string'?v[k].trim():v[k]]));
}
export function passwordRecord(password){const salt=crypto.randomBytes(24).toString('hex');return {salt,hash:crypto.scryptSync(password,salt,64,{N:32768,maxmem:64*1024*1024}).toString('hex')};}
function atomic(file,value){fs.mkdirSync(path.dirname(file),{recursive:true,mode:0o700});const temp=file+'.tmp';const fd=fs.openSync(temp,'w',0o600);try{fs.writeFileSync(fd,JSON.stringify(value,null,2));fs.fsyncSync(fd);}finally{fs.closeSync(fd);}fs.renameSync(temp,file);const d=fs.openSync(path.dirname(file),'r');try{fs.fsyncSync(d);}finally{fs.closeSync(d);}}
function secureFile(base,relative){const full=fs.realpathSync(path.resolve(base,relative));if(!full.startsWith(fs.realpathSync(base)+path.sep)||!fs.statSync(full).isFile())throw Error('Недопустимый файл.');return full;}
const hash=v=>crypto.createHash('sha256').update(v).digest('hex');
const now=()=>new Date().toISOString();
const types={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.mjs':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.json':'application/json; charset=utf-8','.txt':'text/plain; charset=utf-8','.py':'text/plain; charset=utf-8','.svg':'image/svg+xml','.png':'image/png','.jpg':'image/jpeg','.webp':'image/webp'};

export function createPanel(config){
 const data=path.resolve(config.data_dir),stateFile=path.join(data,'state.json'),jobsDir=path.join(data,'jobs');
 fs.mkdirSync(jobsDir,{recursive:true,mode:0o700});
 // Fail closed for unreadable/corrupt state; never silently discard existing jobs.
 let state=fs.existsSync(stateFile)?JSON.parse(fs.readFileSync(stateFile,'utf8')):{jobs:[],sessions:{},draft:null};
 if(!Array.isArray(state.jobs)||!state.sessions)throw Error('Invalid panel state');
 const save=()=>atomic(stateFile,state), attempts=new Map();
 let active=null,stopping=false;
 for(const j of state.jobs)if(j.status==='running'){j.status='interrupted';j.error='Сервис перезапущен во время обработки. Повторный запуск требует твоего действия.';j.ended_at=now();}
 save();
 // SMARTGRAM_DRAIN_PROTOCOL_V1: deployment marker gates admission AND queue selection.
 function maintenance(){return fs.existsSync(path.join(data,'maintenance.json'));}
 function runner(){
  try{
   const c=config.runner_command;
   if(maintenance()||!Array.isArray(c)||c.length<2||!c.every(x=>typeof x==='string')||!path.isAbsolute(c[0])||!fs.existsSync(c[0])||config.runner_contract!==1||!config.runner_readiness_file)return false;
   const proof=JSON.parse(fs.readFileSync(config.runner_readiness_file,'utf8'));
   return proof.ready===true&&proof.contract_version===1&&proof.release===fs.realpathSync(config.runner_cwd)&&proof.adapter_sha256===hash(fs.readFileSync(c[1]));
  }catch{return false;}
 }
 function publicJob(j){let stage='';try{const progress=JSON.parse(fs.readFileSync(path.join(jobsDir,j.id,'progress.json'),'utf8'));if(typeof progress.stage==='string')stage=progress.stage.slice(0,150);}catch{}
 return {id:j.id,request:j.request,status:j.status,created_at:j.created_at,started_at:j.started_at,ended_at:j.ended_at,error:j.error,stage:j.status==='running'?stage:''};}
 function session(req){const raw=(req.headers.cookie||'').split(';').map(x=>x.trim()).find(x=>x.startsWith('smartgram_session='))?.slice(18);if(!raw||!/^[-\w]{40,100}$/.test(raw))return null;const key=hash(raw),s=state.sessions[key];return s&&s.expires>Date.now()?key:null;}
 function json(res,status,value,headers={}){res.writeHead(status,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store',...headers});res.end(JSON.stringify(value));}
 async function body(req){if(!String(req.headers['content-type']||'').startsWith('application/json'))throw Object.assign(Error('Ожидается JSON.'),{status:415});let size=0,parts=[];for await(const part of req){size+=part.length;if(size>16384)throw Object.assign(Error('Слишком большой запрос.'),{status:413});parts.push(part);}try{return JSON.parse(Buffer.concat(parts).toString('utf8'));}catch{throw Object.assign(Error('Некорректный JSON.'),{status:400});}}
 function file(req,res,filename,mime,download=false){const stat=fs.statSync(filename);let start=0,end=stat.size-1,status=200;const range=req.headers.range;if(range){const m=/^bytes=(\d*)-(\d*)$/.exec(range);if(!m||(!m[1]&&!m[2])){res.writeHead(416,{'Content-Range':`bytes */${stat.size}`});res.end();return;}if(!m[1])start=Math.max(0,stat.size-Number(m[2]));else{start=Number(m[1]);if(m[2])end=Math.min(Number(m[2]),end);}if(start>end||start>=stat.size||!Number.isSafeInteger(start)||!Number.isSafeInteger(end)){res.writeHead(416,{'Content-Range':`bytes */${stat.size}`});res.end();return;}status=206;}
 const headers={'Content-Type':mime,'Content-Length':end-start+1,'Accept-Ranges':'bytes','Cache-Control':'private, no-store'};if(status===206)headers['Content-Range']=`bytes ${start}-${end}/${stat.size}`;if(download)headers['Content-Disposition']='attachment; filename="smartgram-video.mp4"';res.writeHead(status,headers);if(req.method==='HEAD'){res.end();return;}const stream=fs.createReadStream(filename,{start,end});stream.on('error',()=>res.destroy());res.on('close',()=>stream.destroy());stream.pipe(res);}
 async function handle(req,res){
 res.setHeader('X-Content-Type-Options','nosniff');res.setHeader('X-Frame-Options','DENY');res.setHeader('Referrer-Policy','same-origin');
 try{
 const url=new URL(req.url,'http://localhost'),p=decodeURIComponent(url.pathname);
 if(['POST','PUT','DELETE','PATCH'].includes(req.method)&& (req.headers.origin!==config.origin||req.headers['x-smartgram-request']!=='1'))return json(res,403,{error:'Запрос с другого сайта запрещён.'});
 if(p==='/healthz'&&req.method==='GET')return json(res,200,{ok:true});
 if(p==='/login'&&req.method==='GET')return file(req,res,path.join(root,'server/login.html'),'text/html; charset=utf-8');
 if(p==='/api/login'&&req.method==='POST'){
  const key=String(req.headers['x-forwarded-for']||req.socket.remoteAddress).split(',')[0].trim();
  const time=Date.now();for(const [ip,a] of attempts)if(a.until<time)attempts.delete(ip);
  const a=attempts.get(key)||{count:0,until:time+600000};if(a.count>=8||attempts.size>10000)return json(res,429,{error:'Слишком много попыток. Повтори через 10 минут.'});a.count++;attempts.set(key,a);
  const b=await body(req);if(typeof b.password!=='string'||b.password.length>256||typeof b.username!=='string')return json(res,400,{error:'Проверь логин и пароль.'});
  const actual=await new Promise((resolve,reject)=>crypto.scrypt(b.password,config.password.salt,64,{N:32768,maxmem:64*1024*1024},(e,v)=>e?reject(e):resolve(v)));
  if(b.username!==config.username||!crypto.timingSafeEqual(actual,Buffer.from(config.password.hash,'hex')))return json(res,401,{error:'Неверный логин или пароль.'});
  attempts.delete(key);for(const [k,v] of Object.entries(state.sessions))if(v.expires<time)delete state.sessions[k];
  if(Object.keys(state.sessions).length>30){const oldest=Object.keys(state.sessions).sort((a,b)=>state.sessions[a].expires-state.sessions[b].expires)[0];delete state.sessions[oldest];}
  const token=crypto.randomBytes(32).toString('base64url');state.sessions[hash(token)]={expires:time+43200000};save();
  return json(res,200,{ok:true},{'Set-Cookie':`smartgram_session=${token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=43200`});
 }
 const sid=session(req);
 if(!sid){if(p.startsWith('/api/'))return json(res,401,{error:'Войди в аккаунт.'});res.writeHead(303,{Location:'/login'});res.end();return;}
 if(p==='/api/logout'&&req.method==='POST'){delete state.sessions[sid];save();return json(res,200,{ok:true},{'Set-Cookie':'smartgram_session=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0'});}
 if(p==='/api/capabilities'&&req.method==='GET')return json(res,200,{application:'smartgram-studio',generation_ready:runner(),maintenance:maintenance(),supported_subtitles:[true],generation_reason:maintenance()?'Идёт обновление. Очередь сохранена.':runner()?null:'Адаптер не прошёл проверку готовности.',concurrency:1,contract_version:1});
 if(p==='/api/draft'&&req.method==='GET')return json(res,200,{draft:state.draft});
 if(p==='/api/draft'&&req.method==='PUT'){state.draft=validateRequest((await body(req)).draft);save();return json(res,200,{ok:true});}
 if(p==='/api/jobs'&&req.method==='GET')return json(res,200,{jobs:state.jobs.slice().reverse().slice(0,200).map(publicJob)});
 if(p==='/api/jobs'&&req.method==='POST'){
  const b=await body(req),request=validateRequest(b.request);if(typeof b.idempotency_key!=='string'||!/^[-\w]{16,80}$/.test(b.idempotency_key))return json(res,400,{error:'Нет идентификатора запроса.'});
  const old=state.jobs.find(j=>j.idempotency_key===b.idempotency_key);if(old){if(JSON.stringify(old.request)!==JSON.stringify(request))return json(res,409,{error:'Идентификатор уже использован для другого задания.'});return json(res,200,{job:publicJob(old)});}
  if(maintenance())return json(res,503,{error:'Идёт обновление. Дождись завершения; очередь сохранена.'});
  if(!request.subtitles)return json(res,422,{error:'Режим без субтитров пока не подключён к полной проверке монтажа. Включите субтитры.'});
  if(!runner())return json(res,409,{error:'Адаптер не прошёл проверку готовности. Задание можно сохранить как черновик.'});
  if(state.jobs.filter(j=>['queued','running'].includes(j.status)).length>=10)return json(res,429,{error:'В очереди уже 10 роликов. Дождись завершения.'});
  if(state.jobs.length>=1000)return json(res,409,{error:'Достигнут лимит истории. Требуется архивирование на сервере.'});
  const free=fs.statfsSync(data);if(free.bavail*free.bsize<2*1024**3)return json(res,507,{error:'На сервере осталось менее 2 ГБ. Освободи место перед запуском.'});
  const j={id:crypto.randomUUID(),idempotency_key:b.idempotency_key,request,status:'queued',created_at:now()};state.jobs.push(j);save();json(res,202,{job:publicJob(j)});setImmediate(tick);return;
 }
 const cancel=/^\/api\/jobs\/([a-f0-9-]{36})\/cancel$/.exec(p);
 if(cancel&&req.method==='POST'){const j=state.jobs.find(j=>j.id===cancel[1]);if(!j)return json(res,404,{error:'Задание не найдено.'});if(j.status!=='queued')return json(res,409,{error:'Можно отменить только задание в очереди.'});j.status='cancelled';j.ended_at=now();save();return json(res,200,{ok:true});}
 const video=/^\/api\/jobs\/([a-f0-9-]{36})\/video$/.exec(p);
 if(video&&['GET','HEAD'].includes(req.method)){const j=state.jobs.find(j=>j.id===video[1]);if(!j||j.status!=='completed')return json(res,404,{error:'Видео ещё не готово.'});return file(req,res,secureFile(path.join(jobsDir,j.id),'video.mp4'),'video/mp4',url.searchParams.get('download')==='1');}
 if(p.startsWith('/api/'))return json(res,404,{error:'Метод не найден.'});
 if(!['GET','HEAD'].includes(req.method))return json(res,405,{error:'Метод не разрешён.'});
 const relative=p==='/'?'index.html':p.slice(1);if(relative.split('/').some(x=>x.startsWith('.')))return json(res,404,{error:'Файл не найден.'});
 const ext=path.extname(relative);if(!types[ext])return json(res,404,{error:'Файл не найден.'});
 try{return file(req,res,secureFile(path.join(root,'dist'),relative),types[ext]);}catch{return json(res,404,{error:'Файл не найден.'});}
 }catch(e){if(!res.headersSent)json(res,e.status||400,{error:e.status?e.message:'Не удалось обработать запрос. Проверь данные.'});else res.destroy();}
 }
 function finish(job,status,error){if(!['running'].includes(job.status))return;job.status=status;job.error=error;job.ended_at=now();save();}
 function tick(){
 if(stopping||active||!runner())return;const job=state.jobs.find(j=>j.status==='queued');if(!job)return;
 const dir=path.join(jobsDir,job.id);fs.mkdirSync(dir,{recursive:true,mode:0o700});atomic(path.join(dir,'request.json'),{contract_version:1,id:job.id,...job.request});job.status='running';job.started_at=now();save();
 const command=config.runner_command,env={...process.env};delete env.SMARTGRAM_PANEL_CONFIG;
 const child=spawn(command[0],[...command.slice(1),dir],{cwd:config.runner_cwd||dir,env,stdio:['ignore','ignore','ignore'],shell:false,detached:true});active=child;
 const stopGroup=()=>{try{process.kill(-child.pid,'SIGTERM');}catch{}};
 const timeout=setTimeout(()=>{finish(job,'failed','Превышен лимит обработки: 30 минут.');stopGroup();setTimeout(()=>{try{process.kill(-child.pid,'SIGKILL');}catch{}},5000).unref();},1800000);
 child.once('error',()=>{clearTimeout(timeout);finish(job,'failed','Не удалось запустить конвейер. Проверь подключение на сервере.');active=null;});
 child.once('close',async code=>{clearTimeout(timeout);try{
   if(stopping){finish(job,'interrupted','Работа прервана остановкой сервиса. Автоматический платный повтор отключён.');return;}
   if(code!==0)throw Error('Конвейер завершился с ошибкой. Технические подробности доступны администратору.');
   const result=JSON.parse(fs.readFileSync(secureFile(dir,'result.json'),'utf8'));if(result.status!=='completed'||result.quality_passed!==true)throw Error('Конвейер не подтвердил готовность и проверку ролика.');
   const filename=secureFile(dir,'video.mp4');if(fs.statSync(filename).size<32)throw Error('Готовый видеофайл отсутствует или пуст.');
   // An adapter must supply the accepted final MP4, not a scene or preview.
   await new Promise((resolve,reject)=>{let out='';const probe=spawn('/usr/bin/ffprobe',['-v','error','-show_entries','stream=codec_type,width,height:format=duration','-of','json',filename],{stdio:['ignore','pipe','ignore']});const timer=setTimeout(()=>{probe.kill('SIGKILL');reject(Error('Проверка видео превысила время ожидания.'));},15000);probe.stdout.on('data',d=>{if(out.length<64000)out+=d;});probe.on('error',()=>{clearTimeout(timer);reject(Error('FFprobe недоступен на сервере.'));});probe.on('close',code=>{clearTimeout(timer);try{const p=JSON.parse(out),[w,h]=job.request.output_size.split('x').map(Number),duration=Number(p.format?.duration);if(code!==0||!p.streams.some(s=>s.codec_type==='video'&&s.width===w&&s.height===h)||!p.streams.some(s=>s.codec_type==='audio')||!Number.isFinite(duration)||duration<1)throw Error();resolve();}catch{reject(Error('Готовый файл не прошёл проверку формата, изображения и звука.'));}});});
   finish(job,'completed',null);
 }catch(e){finish(job,'failed',e.message);}finally{active=null;setImmediate(tick);}});
 }
 const server=http.createServer({maxHeaderSize:16384},handle);server.requestTimeout=20000;server.headersTimeout=15000;server.keepAliveTimeout=5000;
 const timer=setInterval(tick,1500);timer.unref();
 async function close(){stopping=true;clearInterval(timer);if(active){try{process.kill(-active.pid,'SIGTERM');}catch{}}await new Promise(resolve=>server.close(resolve));}
 return {server,tick,close};
}
// ESM resolves symlinks in import.meta.url; systemd starts through current/.
if(process.argv[1]&&fs.realpathSync(process.argv[1])===fileURLToPath(import.meta.url)){
 process.umask(0o077);
 const config=JSON.parse(fs.readFileSync(process.env.SMARTGRAM_PANEL_CONFIG||'/etc/smartgram-panel/config.json','utf8'));
 if(!config.origin?.startsWith('https://')||!config.username||!config.password?.salt||!/^[a-f0-9]{128}$/.test(config.password?.hash||''))throw Error('Invalid panel configuration');
 const panel=createPanel(config);panel.server.listen(8787,'127.0.0.1',()=>console.log('Smartgram panel listening on 127.0.0.1:8787'));
 for(const signal of ['SIGTERM','SIGINT'])process.on(signal,()=>{panel.close().finally(()=>process.exit(0));setTimeout(()=>process.exit(0),6000).unref();});
}
