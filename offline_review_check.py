"""Saved accepted audio → proved subtitle binding → technical MP4 with TEST images.

No research, scriptwriting, image search, TTS, ASR, GPT or final visual approval.
Real release remains blocked until all real images and final review are approved.
"""
import argparse
import base64
import copy
import json
import math
import shutil
from pathlib import Path
from unittest.mock import patch

from PIL import Image,ImageDraw,ImageFont
from file_io import atomic_write_json,atomic_write
from editing_inputs import checked_audio,snapshot_inputs
from media_tools import load_media_settings,inspect_tools
from production_state import config,read,sha,ProductionError,digest
from script_workflow import approved_script
from subtitle_binding import binding
from subtitles import materialize,script_tokens,layout
from visual_geometry import prepare
from visual_shots import shot_timeline
from video_render import schedule,render_video,compose,technical_check,sample_schedule,extract_frames
from video_schedule_check import check_schedule


def local_plan(script,words,proof,settings,font,size):
    """Local line/duration search over proved atomic blocks, without ASR text veto."""
    tokens=script_tokens(script);cues=[]
    width=size[0]*settings['width_percent']/100
    def line_breaks(first,last):
        chunks=[t['text'] for t in tokens[first-1:last]]
        def fits(v):return font.getlength(' '.join(v))+14<=width
        if fits(chunks):return [{'first_token_id':first,'last_token_id':last}]
        choices=[]
        for split in range(1,len(chunks)):
            if fits(chunks[:split]) and fits(chunks[split:]):
                penalty=abs(font.getlength(' '.join(chunks[:split]))-font.getlength(' '.join(chunks[split:])))
                if chunks[split-1].casefold() in {'в','на','к','с','о','об','из','и','а','не','по','для','от','до'}:penalty+=1000
                choices.append((penalty,split))
        if not choices:return None
        cut=min(choices)[1]
        return [{'first_token_id':first,'last_token_id':first+cut-1},{'first_token_id':first+cut,'last_token_id':last}]
    for index in range(1,len(script['fragments'])+1):
        blocks=[b for b in proof['blocks'] if b['fragment_index']==index];n=len(blocks);best={n:(0,[])}
        for i in range(n-1,-1,-1):
            candidates=[]
            for j in range(i,n):
                first,last=blocks[i]['first_token_id'],blocks[j]['last_token_id']
                af,al=blocks[i]['first_audio_word_id'],blocks[j]['last_audio_word_id']
                seconds=words[al-1]['end']-words[af-1]['start']
                if seconds*1000>settings['max_duration_ms']+1e-6:break
                exact=' '.join(t['text'] for t in tokens[first-1:last]);lines=line_breaks(first,last)
                if seconds*1000<settings['min_duration_ms']-1e-6 or len(exact)/seconds>settings['max_characters_per_second'] or not lines or j+1 not in best:continue
                cue={'first_token_id':first,'last_token_id':last,'first_audio_word_id':af,'last_audio_word_id':al,'lines':lines}
                score=best[j+1][0]+1+abs(seconds-2.2)*.15-(.1 if exact.endswith(('.',',','?',':',';')) else 0)
                candidates.append((score,[cue,*best[j+1][1]]))
            if candidates:best[i]=min(candidates,key=lambda c:c[0])
        if 0 not in best:raise ProductionError('Local line constraints need alignment/layout repair for fragment '+str(index)+'. No API or TTS used.')
        cues.extend(best[0][1])
    for i,cue in enumerate(cues,1):cue['cue_id']=i
    return {'status':'ok','reason':None,'cues':cues,'method':'offline_search_over_proved_blocks'}


def validate_topic(source,destination,index,settings,media,tools):
    snapshot_inputs(source,destination,include_audio=True)
    script=approved_script(destination);audio=read(destination/'audio_manifest.json')
    path,duration,words,fragments=checked_audio(script,audio,destination)
    proof=binding(script,words,fragments,settings['subtitle_segment'].get('equivalences',{}))
    size=(360,640);fps=settings['video_render']['fps'];journal=[]
    font=ImageFont.truetype(str(media.font),round(settings['subtitle_layout']['font_pixels_at_1080_width']*size[0]/1080))
    segmentation=local_plan(script,words,proof,settings['subtitle_layout'],font,size)
    atomic_write_json(destination/'subtitle_binding.json',proof);atomic_write_json(destination/'subtitle_segmentation.json',segmentation)
    cues=materialize(segmentation,script,words,fragments,settings['subtitle_segment'].get('equivalences',{}),binding_proof=proof)
    atomic_write_json(destination/'subtitle_cues.json',{'cues':cues,'text_source':'approved_script','timing_source':'saved_deepgram','test_images':True})
    metrics=layout(destination,cues,settings['subtitle_layout'],media,tools['font_family'],size,journal=journal)
    atomic_write_json(destination/'subtitle_layout.json',metrics)
    if not metrics['passed']:raise ProductionError('Local subtitle layout failed: '+str(metrics['issues']))
    assets=[];visuals={'fragments':[]}
    for number,fragment in enumerate(script['fragments'],1):
        original=destination/f'TEST_frame_{number}.png';image=Image.new('RGB',(720,1280),['#12335a','#1e553f','#562d4c'][index-1])
        draw=ImageDraw.Draw(image);big=ImageFont.truetype(str(media.font),42)
        draw.text((55,90),'ТЕХНИЧЕСКИЙ ТЕСТ',font=big,fill='white')
        draw.text((55,160),'Тестовое изображение',font=ImageFont.truetype(str(media.font),32),fill='white')
        draw.text((55,235),f'Тема {index} · фрагмент {number}',font=big,fill='white')
        draw.rectangle((80,420,640,900),outline='white',width=8);draw.line((80,420,640,900),fill='white',width=5)
        image.save(original)
        motion=prepare(original,{'mode':'crop','initial_crop':[0,0,1,1],'focus_point':[.5,.5],'zoom_percent':5,'must_keep':[]},destination/f'TEST_motion_{number}',size)
        for preview in motion['previews']:preview['path']=str(Path(preview['path']).relative_to(destination))
        assets.append({'original_path':original.name,'motion':motion})
        visuals['fragments'].append({'status':'test_only','shot_id':f'test_{number}'})
    timeline=shot_timeline(script,visuals,fragments,words,settings['subtitle_segment'].get('equivalences',{}),binding_proof=proof)
    scenes,total=schedule(timeline,duration,fps,settings['video_render']['transition'],settings['video_render']['transition_ms'])
    atomic_write_json(destination/'test_timeline.json',{'scenes':scenes,'total_frames':total,'duration_seconds':duration,'test_images':True})
    render_video(destination,assets,scenes,total,size,settings['video_render'],media,journal)
    candidate,_=compose(destination,path,settings['compose'],media,journal)
    hashes={str(path.relative_to(destination)):audio['audio_sha256'],**{a['original_path']:sha(destination/a['original_path']) for a in assets},
        **{name:sha(destination/name) for name in ['script.json','script_state.json','audio_manifest.json','subtitles.ass','subtitle_binding.json']}}
    technical=technical_check(candidate,path,duration,size,fps,total,media,hashes,destination,journal)
    atomic_write_json(destination/'technical_checks.json',technical)
    if not technical['passed']:raise ProductionError('Technical MP4 checks failed: '+str(technical['errors']))
    samples=sample_schedule(cues,scenes,total,fps)
    extract_frames(candidate,destination/'test_frames',samples,media,journal)
    timing=check_schedule(destination,assets,scenes,samples,size,media,journal,fps=fps)
    atomic_write_json(destination/'schedule_checks.json',timing)
    atomic_write_json(destination/'test_frame_samples.json',{'samples':samples})
    if not timing['passed']:raise ProductionError('Actual burned subtitle/scene schedule failed.')
    final=destination/'TEST_ONLY.mp4';candidate.replace(final)
    missing=[]
    for location in sorted(Path('outputs').glob('visuals_only_*/'+source.name+'/visual_materials.json')):
        value=read(location)
        if value.get('script_version_id')==script['script_version_id']:
            missing.append({'path':str(location),'status':value.get('status'),'fragment_statuses':[f['status'] for f in value['fragments']]})
    result={'topic':read(destination/'research.json').get('topic'),
        'audio_status':'completed','script_version_id':script['script_version_id'],'audio_sha256':audio['audio_sha256'],
        'duration_seconds':duration,'subtitle_cues':len(cues),'binding_blocks':len(proof['blocks']),
        'subtitle_layout_passed':True,'technical_mp4_passed':True,'actual_schedule_passed':True,
        'original_wav_byte_identical':sha(path)==sha(source/audio['audio_path'])==audio['audio_sha256'],
        'test_images':True,'final_visual_review':'not_run_no_API','real_release_ready':False,
        'real_visual_materials':missing,'reason':'Нет полного принятого набора реальных изображений. MP4 — технический тест с явно тестовыми кадрами.',
        'mp4_path':str(final),'mp4_sha256':sha(final),'source_directory':str(source),
        'source_script_sha256':sha(source/'script.json'),'source_script_state_sha256':sha(source/'script_state.json'),
        'settings_snapshot':settings,'technical':technical,'schedule':timing,'new_paid_requests':0}
    atomic_write_json(destination/'technical_validation.json',result)
    atomic_write_json(destination/'local_compute.json',{'operations':journal,'new_api_requests':0})
    return result


def report(output,results):
    from html import escape
    from build_review_archive import collect_project_files
    source_hashes={str(p.relative_to(Path.cwd())):sha(p) for p in collect_project_files()}
    summary={'schema_version':1,'main_project':str(Path.cwd()),'review_archive':'Smartgram_review_20261101T130216Z.zip',
        'source_files_sha256':source_hashes,'code_id':digest(source_hashes),'new_api_requests':0,'request_count_scope':'offline_validation_only','new_paid_requests':0,'network_forbidden':True,
        'all_real_releases_ready':False,'topics':results,
        'offline_tests':read(output/'offline_tests.json') if (output/'offline_tests.json').exists() else None}
    atomic_write_json(output/'review_validation.json',summary)
    sections=[]
    for entry in results:
        # Standalone proof uses actual decoded frames. MP4 files remain local;
        # arbitrary compressed video is not exempted from the secret scanner.
        samples=read(Path(entry['mp4_path']).parent/'test_frame_samples.json')['samples']
        posters=[]
        for sample in [samples[0],samples[len(samples)//2],samples[-1]]:
            data=base64.b64encode(Path(sample['path']).read_bytes()).decode()
            posters.append('<figure><img alt="Извлечённый кадр технического MP4" src="data:image/png;base64,'+data+'"><figcaption>Кадр '
                +str(sample['frame_number'])+' · '+format(sample['time'],'.3f')+' с</figcaption></figure>')
        sections.append('<section><h2>'+escape(entry['topic'])+'</h2><p>Озвучка принята; субтитры и фактический монтаж проверены. Реальный ролик ещё не готов: нет полного принятого набора изображений.</p>'
            '<p><strong>Ниже кадры технического MP4 с тестовыми изображениями, не готового ролика.</strong> Исходный WAV сохранён побайтно. '
            +escape(str(entry['duration_seconds']))+' с; 360×640, 30 fps.</p><div class="posters">'+''.join(posters)+'</div><p>Технический MP4 сохранён локально: '+escape(entry['mp4_path'])+'</p>'
            '<details><summary>Проверки и происхождение</summary><pre>'+escape(json.dumps(entry,ensure_ascii=False,indent=2))+'</pre></details></section>')
    page='<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Smartgram · исправления ревью</title><style>body{max-width:1000px;margin:auto;padding:24px;font:16px/1.5 system-ui;background:#f3f5f8}section{background:white;padding:22px;margin:20px 0}.posters{display:flex;flex-wrap:wrap;gap:8px}.posters figure{margin:0}.posters img{max-width:240px;width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>Smartgram · исправления ревью</h1><p>Текущий основной проект; старый архив использован только для сверки замечаний. Последние изменения озвучки, поиска и аналитики сохранены.</p><p>Платных запросов: 0. Офлайн-проверки выполнялись без API. Исследование, сценарист, TTS, ASR, поиск материалов и платная финальная проверка не запускались. При штатной передаче HTML аналитика отдельно читает Costs/Usage по AGENTS.md.</p><p>Исправлены привязка сценария к словам Deepgram для субтитров и кадров, отзыв одобрения до исправления картинки и параллельные независимые ветки со снимком сценария и защищённым общим журналом.</p><p>OpenAI подтверждает текст; Deepgram задаёт ID и времена. Неоднозначные внутренние границы не угадываются; исправляется только привязка.</p><p>Версия исходников: '+escape(summary['code_id'])+'</p><p>Офлайн-тестов пройдено: '+str((summary.get('offline_tests') or {}).get('tests_passed','см. offline_tests.json'))+'. Проверка ограничений, сети и DNS включена.</p>'+''.join(sections)+'<details><summary>SHA-256 файлов кода этой итерации</summary><pre>'+escape(json.dumps(source_hashes,ensure_ascii=False,indent=2))+'</pre></details></html>'
    atomic_write(output/'report.html',page.encode())
    return output/'report.html'


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    with patch('socket.socket.connect',side_effect=AssertionError('No network in review validation')),patch('socket.getaddrinfo',side_effect=AssertionError('No DNS in review validation')):
        media=load_media_settings();tools=inspect_tools(media,[]);settings=config()['editing'];results=[]
        for index in (1,2,3):
            source=Path('outputs/gemini_migration_20261101')/f'topic_{index}'
            print('Technical saved-audio/subtitle/montage test:',source,flush=True)
            results.append(validate_topic(source,output/f'topic_{index}',index,settings,media,tools))
            atomic_write_json(output/'progress.json',{'completed':len(results),'new_api_requests':0})
        print(report(output,results),flush=True)


if __name__=='__main__':main()
