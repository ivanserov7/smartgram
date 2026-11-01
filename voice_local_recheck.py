"""Reassess saved ASRs/GPT and current WAV locally. This module has no API client."""
import copy
import hashlib
import json
from datetime import datetime,timezone
from pathlib import Path

from audio_files import inspect_wav
from file_io import atomic_write_json
from scene_files import read_json
from script_workflow import approved_script
from voice_alignment import TEXT_POLICY_VERSION,text_authority,validate_check,validate_words
from voice_audio_checks import current_audio_checks
from voice_errors import AudioContractError,AudioAlignmentError,AudioTechnicalError,VoiceoverError
from voice_settings import load_voiceover_settings


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def contained(directory,relative):
    path=(directory/relative).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise AudioContractError('Не найден сохранённый вход внутри папки запуска: '+str(relative))
    return path


def implementation():
    root=Path(__file__).parent
    return {name:sha(root/name) for name in ['voice_alignment.py','voice_audio_checks.py','voice_local_recheck.py']}


def source_check(state,manifest,script):
    saved=manifest.get('local_reassessment',{})
    key=saved.get('source_check_operation')
    candidates={k:op for k,op in state['operations'].items() if op.get('stage')=='audio_check' and op.get('status')=='completed'
        and op.get('request',{}).get('audio_sha256')==manifest['audio_sha256']
        and op.get('request',{}).get('script_version_id')==script['script_version_id']}
    if key in candidates:return key,candidates[key]
    exact=[(k,op) for k,op in candidates.items() if op.get('result')==manifest.get('review')]
    if len(exact)==1:return exact[0]
    if len(candidates)==1:return next(iter(candidates.items()))
    raise AudioContractError('Нет однозначной сохранённой GPT-проверки этого WAV/сценария. API не вызывались.')


def find_saved(directory,name,predicate):
    for folder in ['voiceover','audio_checks']:
        for path in sorted((directory/folder).glob('*/'+name)):
            try:
                value=read_json(path)
                if predicate(value):return path,value
            except (OSError,ValueError,TypeError,KeyError):continue
    raise AudioContractError('Недостаточно сохранённых данных: не найден подтверждённый '+name+'. API не вызывались.')


def prepare_local(directory,settings):
    directory=Path(directory).resolve();script=approved_script(directory)
    manifest=read_json(directory/'audio_manifest.json');state=read_json(directory/'voiceover_state.json')
    if manifest.get('mode')!='real' or manifest.get('script_version_id')!=script['script_version_id']:
        raise AudioContractError('WAV и текущий одобренный сценарий не относятся к одной реальной версии.')
    audio=contained(directory,manifest['audio_path']);measured=inspect_wav(audio.read_bytes())
    if measured['audio_sha256']!=manifest['audio_sha256']:raise AudioContractError('Текущий WAV изменён: SHA-256 не совпадает с сохранённым. API не вызывались.')
    op_key,op=source_check(state,manifest,script);request=op['request']
    dg_path,raw=find_saved(directory,'deepgram.json',lambda v:v.get('results',{}).get('channels',[{}])[0].get('alternatives',[{}])[0].get('transcript')==request['deepgram']['transcript']
        and v.get('metadata',{}).get('sha256') in (None,measured['audio_sha256']))
    ai_path,confirmation=find_saved(directory,'openai_transcript.json',lambda v:v==request.get('confirmation'))
    confirms=[o for o in state['operations'].values() if o.get('stage')=='audio_confirm' and o.get('status')=='completed'
        and o.get('request',{}).get('audio_sha256')==measured['audio_sha256'] and o.get('result')==confirmation]
    if not confirms:raise AudioContractError('У сохранённого OpenAI ASR нет доказанной связи с этим WAV. API не вызывались.')
    files={str(p.relative_to(directory)):sha(p) for p in [audio,dg_path,ai_path,directory/'script.json']}
    identity={'script_version_id':script['script_version_id'],'audio_sha256':measured['audio_sha256'],
        'inputs_sha256':files,'source_check_operation':op_key,'source_request_sha256':digest(request),
        'source_result_sha256':digest(op['result']),'policy_version':TEXT_POLICY_VERSION,
        'validation_settings_snapshot':settings.snapshot(),'implementation_sha256':implementation()}
    authority=text_authority(script,(request['deepgram']['transcript'],confirmation['text']),op['result'])
    updated=copy.deepcopy(manifest);updated.update(**measured,text_confirmation=authority,review_source='local_saved_evidence')
    _,technical=current_audio_checks(audio,manifest.get('technical_checks'))
    updated['technical_checks']=technical
    target=read_json(directory/'script_state.json')['settings']
    identity['target_duration_seconds']={k:target[k] for k in ['target_min_seconds','target_max_seconds']}
    status,reason='needs_review','OpenAI не подтвердил текст; сохранённого содержательного решения недостаточно. API не вызывались.'
    mapped=[];words=None;review=None
    try:
        words=validate_words(raw,measured['duration_seconds'],settings.timing_tolerance_seconds)
        if words['transcript']!=request['deepgram']['transcript'] or words['words']!=request['deepgram']['words']:
            raise AudioContractError('Слова/таймкоды Deepgram изменились относительно сохранённой GPT-привязки.')
        review,mapped=validate_check(op['result'],script,words['words'],measured['duration_seconds'],settings.timing_tolerance_seconds,
            confirmed=True,transcripts=(words['transcript'],confirmation['text']))
        text_ok=review['decision']=='Можно использовать'
        status='completed' if text_ok else 'needs_review'
        reason=None if text_ok else 'OpenAI не подтверждён содержательной проверкой: '+review['decision']
    except (AudioContractError,VoiceoverError) as error:
        text_ok=authority['approved']
        status='alignment_error' if text_ok else 'contract_error'
        reason='Текст подтверждён OpenAI, но привязка/таймкоды Deepgram непригодны. '+str(error) if text_ok else str(error)
    if not technical['passed']:
        status='technical_check_failed';reason='; '.join(technical['issues'])
    elif status=='completed' and not target['target_min_seconds']<=measured['duration_seconds']<=target['target_max_seconds']:
        status='duration_out_of_range';reason='Текст принят, но длительность WAV вне согласованного диапазона; синтез и сценарист не запускались.'
    updated.update(status=status,reason=reason,text_verified=text_ok,
        timing_status='aligned_to_deepgram_words' if mapped and text_ok else 'invalid_alignment',
        fragments=mapped if text_ok else [],provisional_fragments=mapped if not text_ok else [],
        total_duration_seconds=measured['duration_seconds'],openai_transcript=confirmation)
    if words:updated.update(words=words['words'],transcript=words['transcript'])
    if review:updated['review']=review
    # Re-evaluation is atomic with respect to source evidence: do not publish approval
    # if a source was concurrently changed after it was read.
    if any(sha(contained(directory,name))!=expected for name,expected in files.items()):
        raise AudioContractError('Исходные данные изменились во время локальной переоценки. Результат не принят.')
    return updated,state,identity,op_key


def reassess_saved(directory,settings=None):
    directory=Path(directory).resolve()
    if settings is None:_,settings=load_voiceover_settings(require_keys=False)
    before=read_json(directory/'audio_manifest.json')
    try:updated,state,identity,op_key=prepare_local(directory,settings)
    except (VoiceoverError,OSError,ValueError,KeyError,TypeError) as error:
        # Never leave an obsolete completed flag on a WAV that failed fresh validation.
        reason=str(error) if isinstance(error,VoiceoverError) else 'Недостаточно сохранённых входов: '+type(error).__name__
        history=directory/'local_rechecks'/('blocked_'+digest({'before':before,'reason':reason})[:20]);history.mkdir(parents=True,exist_ok=True)
        if not (history/'manifest_before.json').exists():atomic_write_json(history/'manifest_before.json',before)
        failed={**before,'status':'contract_error','reason':reason,'text_verified':False,'fragments':[],'timing_status':'invalid_alignment'}
        atomic_write_json(directory/'audio_manifest.json',failed)
        state=read_json(directory/'voiceover_state.json')
        if not (history/'state_before.json').exists():atomic_write_json(history/'state_before.json',state)
        state.update(status='contract_error',reason=reason);atomic_write_json(directory/'voiceover_state.json',state)
        raise VoiceoverError(reason) from None
    key=digest(identity);history=directory/'local_rechecks'/key[:24];history.mkdir(parents=True,exist_ok=True)
    if not (history/'manifest_before.json').exists():atomic_write_json(history/'manifest_before.json',before)
    if not (history/'state_before.json').exists():atomic_write_json(history/'state_before.json',state)
    receipt_path=history/'receipt.json'
    if receipt_path.exists():
        old=read_json(receipt_path)
        if old['identity']!=identity:raise AudioContractError('Конфликт локального протокола переоценки; старый протокол сохранён.')
        created=old['created_at_utc']
    else:created=datetime.now(timezone.utc).isoformat()
    receipt={'schema_version':1,'created_at_utc':created,'identity':identity,'source_check_operation':op_key,
        'source_gpt_review':state['operations'][op_key]['result'],'text_confirmation':updated['text_confirmation'],
        'status':updated['status'],'text_verified':updated['text_verified'],'new_api_requests':0,'audio_unchanged':True}
    atomic_write_json(receipt_path,receipt)
    updated['local_reassessment']={'receipt_path':str(receipt_path.relative_to(directory)),'receipt_sha256':sha(receipt_path),
        'source_check_operation':op_key,'identity_sha256':key,'new_api_requests':0}
    state.update(status=updated['status'],reason=updated['reason'])
    state.setdefault('local_reassessments',{})[key]=updated['local_reassessment']
    for job in state.get('jobs',[]):
        for attempt in job.get('attempts',[]):
            if attempt.get('audio',{}).get('audio_sha256')==updated['audio_sha256']:
                attempt.setdefault('review_history',[])
                if not any(item.get('local_reassessment_id')==key for item in attempt['review_history']):
                    attempt['review_history'].append({'local_reassessment_id':key,'previous_status':attempt.get('status'),'previous_review':attempt.get('review')})
                attempt.update(status=updated['status'],review=updated.get('review'))
    atomic_write_json(history/'manifest_after.json',updated)
    atomic_write_json(directory/'voiceover_state.json',state)
    atomic_write_json(directory/'audio_manifest.json',updated)
    return directory/'audio_manifest.json'


def reuse_local(directory,settings):
    directory=Path(directory).resolve();path=directory/'audio_manifest.json'
    if not path.exists():return None
    manifest=read_json(path);local=manifest.get('local_reassessment')
    if not local:return None
    receipt_path=contained(directory,local['receipt_path'])
    if sha(receipt_path)!=local['receipt_sha256']:raise AudioContractError('Локальный протокол переоценки изменён. Автоматические запросы запрещены.')
    receipt=read_json(receipt_path)
    if receipt['identity']['validation_settings_snapshot']!=settings.snapshot():return None
    if receipt['identity']['script_version_id']!=approved_script(directory)['script_version_id']:return None
    # Validate the actual WAV, source hashes, original GPT evidence and all Deepgram
    # times again. Reusing a local approval never turns a new technical failure green.
    updated,state,identity,_=prepare_local(directory,settings)
    if digest(identity)!=local['identity_sha256']:raise AudioContractError('Входы локальной переоценки изменились; новый API-запрос не выполнялся.')
    if updated['status']!='completed':
        error=AudioAlignmentError(updated['reason'],text_verified=updated['text_verified']) if updated['status']=='alignment_error' else AudioTechnicalError(updated['reason']) if updated['status']=='technical_check_failed' else VoiceoverError(updated['reason'])
        raise error
    return path
