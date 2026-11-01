"""One durable panel job -> the existing Smartgram pipeline. No separate queue."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from file_io import atomic_write_json

ROOT = Path(__file__).resolve().parent
FIELDS = {'contract_version','id','topic','target_seconds','output_size','audience','tone','subtitles','notes'}
UNSUPPORTED = 'Режим без субтитров пока не подключён к полной проверке монтажа. Включите субтитры.'


def validate_request(value):
    if not isinstance(value,dict) or set(value)!=FIELDS: raise ValueError('Неверные поля задания.')
    if value['contract_version'] != 1 or isinstance(value['contract_version'],bool): raise ValueError('Неверная версия контракта.')
    if str(uuid.UUID(value['id'])) != value['id']: raise ValueError('Неверный ID задания.')
    for field,limit,required in [('topic',300,True),('notes',2000,False)]:
        if not isinstance(value[field],str) or len(value[field])>limit or (required and not value[field].strip()):raise ValueError('Неверное поле '+field)
    for field,options in [('target_seconds',[30,45,60]),('output_size',['720x1280','1080x1920']),('audience',['general','teen']),('tone',['clear','story'])]:
        if value[field] not in options or isinstance(value[field],bool):raise ValueError('Неверное поле '+field)
    if type(value['subtitles']) is not bool:raise ValueError('Неверное поле subtitles')
    if not value['subtitles']:raise ValueError(UNSUPPORTED)
    return dict(value)


def prepare_job(directory, request):
    """Parameters affect only this child process and its immutable job snapshot."""
    from production_state import config
    request = validate_request(request)
    width,height=map(int,request['output_size'].split('x'))
    settings=config()
    settings['visuals']['preparation']['output']={'width':width,'height':height}
    settings['visuals']['preparation']['full_frame_working_size_min']={'width':round(width*1.1),'height':round(height*1.1)}
    settings['editing']['compose']['burn_subtitles']=request['subtitles']
    atomic_write_json(directory/'runtime_config.json',settings)
    brief={'language':'ru','audience':request['audience'],'tone':request['tone'],'additional_wishes':request['notes']}
    atomic_write_json(directory/'job_brief.json',brief)
    atomic_write_json(directory/'applied_request.json',request)
    return {'SMARTGRAM_RUN_CONFIG':str(directory/'runtime_config.json'),
            'SMARTGRAM_JOB_BRIEF_FILE':str(directory/'job_brief.json'),
            'SCRIPT_TARGET_MIN_SECONDS':str(request['target_seconds']-3),
            'SCRIPT_TARGET_MAX_SECONDS':str(request['target_seconds']+3),
            'SMARTGRAM_FFMPEG_THREADS':'1'}


def readiness():
    """Local configuration/dependency check, never a paid capability probe."""
    from research_config import load_research_settings
    from script_config import load_script_settings
    from voice_settings import load_voiceover_settings
    from production_state import config
    from media_tools import load_media_settings, inspect_tools
    from pipeline import run_pipeline
    load_research_settings();load_script_settings();load_voiceover_settings()
    media=load_media_settings();tools=inspect_tools(media,None)
    if tools['font_family'] != config()['editing']['subtitle_layout']['font']:raise ValueError('Шрифт измерения и монтажа не совпадает.')
    return {'ready':True,'release':str(ROOT),'adapter_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'font_family':tools['font_family'],'font_sha256':hashlib.sha256(media.font.read_bytes()).hexdigest(),
            'supported_subtitles':[True],'unsupported_reason':UNSUPPORTED,'contract_version':1,
            'checked_at_utc':datetime.now(timezone.utc).isoformat()}


def execute(directory, pipeline=None):
    directory=Path(directory).resolve();request=validate_request(json.loads((directory/'request.json').read_text()))
    if directory.name!=request['id']:raise ValueError('ID задания не соответствует каталогу.')
    # A retry of a started/failed job cannot silently spend money again.
    started=time.monotonic();stamp=datetime.now(timezone.utc).isoformat()
    try:
        with (directory/'adapter_started.json').open('x') as marker:
            json.dump({'started_at_utc':stamp,'request_id':request['id']},marker);marker.flush();os.fsync(marker.fileno())
    except FileExistsError:raise ValueError('Задание уже запускалось. Автоматический повтор отключён.') from None
    os.environ.update(prepare_job(directory,request))
    atomic_write_json(directory/'progress.json',{'stage':'Исследование темы'})
    stop=threading.Event()
    labels={'research':'Исследование темы','script':'Сценарий и проверка','voiceover_and_visuals':'Озвучка и изображения','voiceover':'Озвучка','visuals':'Изображения','editing':'Монтаж и проверка','finished':'Проверка готового ролика'}
    def monitor():
        while not stop.wait(.5):
            for p in (directory/'runs').glob('*/run.json'):
                try:
                    state=json.loads(p.read_text());stage=labels.get(state.get('stage'),'Обработка ролика')
                    atomic_write_json(directory/'progress.json',{'stage':stage})
                except (OSError,ValueError):pass
    thread=threading.Thread(target=monitor,daemon=True);thread.start()
    try:
        if pipeline is None:
            from pipeline import run_pipeline
            pipeline=run_pipeline
        run=Path(pipeline(topic=request['topic'],output_root=directory/'runs'))
        if not run.resolve().is_relative_to((directory/'runs').resolve()):raise ValueError('Результат вне каталога задания.')
        manifest=json.loads((run/'render_manifest.json').read_text());quality=json.loads((run/'quality_report.json').read_text())
        if manifest.get('mode')!='real' or manifest.get('test_only') or manifest.get('status')!='ready' or quality.get('ready') is not True or quality.get('technical',{}).get('passed') is not True:
            raise ValueError('Монтаж не получил полное одобрение качества.')
        video=run/'video.mp4';expected=manifest.get('output_hashes',{}).get('video.mp4')
        if not expected or hashlib.sha256(video.read_bytes()).hexdigest()!=expected:raise ValueError('Хеш принятого MP4 не совпадает.')
        if manifest.get('size')!=list(map(int,request['output_size'].split('x'))):raise ValueError('Размер MP4 не соответствует заданию.')
        shutil.copyfile(video,directory/'video.mp4')
        if hashlib.sha256((directory/'video.mp4').read_bytes()).hexdigest()!=expected:raise ValueError('Ошибка копирования MP4.')
        now=datetime.now(timezone.utc).isoformat();duration=time.monotonic()-started
        state_path=run/'run.json';state=json.loads(state_path.read_text());state.update(started_at_utc=stamp,ended_at_utc=now,completed_at_utc=now,duration_seconds=duration)
        atomic_write_json(state_path,state)
        atomic_write_json(directory/'result.json',{'status':'completed','quality_passed':True,'video_sha256':expected,'run_directory':str(run),'duration_seconds':duration})
    finally:
        stop.set();thread.join(timeout=2)
    atomic_write_json(directory/'progress.json',{'stage':'Готово'})


def terminate(signum, frame):
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    if os.getpgrp()==os.getpid():os.killpg(os.getpgrp(),signal.SIGTERM)
    raise SystemExit(128+signum)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('job',type=Path,nargs='?');parser.add_argument('--check',action='store_true');args=parser.parse_args()
    signal.signal(signal.SIGTERM,terminate)
    try:
        if args.check:print(json.dumps(readiness(),ensure_ascii=False));return 0
        if args.job is None:parser.error('Нужен каталог задания.')
        from billing_api import redact,local_secret_values,ENV_PATH
        from dotenv import dotenv_values
        secrets=local_secret_values(dotenv_values(ENV_PATH,interpolate=False),include_environment=True)
        class CleanLog:
            def __init__(self,stream):self.stream=stream;self.buffer=''
            def write(self,value):
                self.buffer+=value
                if '\n' in self.buffer:
                    completed,self.buffer=self.buffer.rsplit('\n',1)
                    self.stream.write(redact(completed+'\n',secrets));self.stream.flush()
                return len(value)
            def flush(self):
                if self.buffer:self.stream.write(redact(self.buffer,secrets));self.buffer=''
                self.stream.flush()
        with (args.job/'diagnostic.log').open('a') as log:
            clean=CleanLog(log)
            try:
                with contextlib.redirect_stdout(clean),contextlib.redirect_stderr(clean):execute(args.job)
            finally:clean.flush()
        return 0
    except Exception as error:
        from billing_api import redact,local_secret_values,ENV_PATH
        from dotenv import dotenv_values
        reason=redact(str(error),local_secret_values(dotenv_values(ENV_PATH,interpolate=False),include_environment=True))
        if args.job:
            # Library stdout may include URLs but no key is allowed in stored diagnostics.
            path=args.job/'diagnostic.log'
            if path.exists():path.write_text(redact(path.read_text(),local_secret_values(dotenv_values(ENV_PATH,interpolate=False),include_environment=True)))
            atomic_write_json(args.job/'progress.json',{'stage':'Остановлено','error':reason})
            atomic_write_json(args.job/'result.json',{'status':'failed','quality_passed':False,'reason':reason})
        print(json.dumps({'ready':False,'reason':reason},ensure_ascii=False),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
