"""Review regressions: saved real ASR, crash safety and concurrent branch journals. Offline."""
import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from PIL import Image,ImageFont

from file_io import atomic_write_json
from production_state import ProductionError,ProductionServiceError,UpstreamBlocked,config,digest,read,sha
from editing_inputs import checked_audio,checked_visuals
from subtitle_binding import binding,bound_range
from subtitles import materialize,script_tokens
from visual_shots import shot_timeline
from visual_materials import VisualMaterials
from test_production import metadata,composition
from visual_geometry import prepare
from test_full_voiceover import make_script
from openai_meter import OpenAIMeter
import pipeline

FIXTURE=Path(__file__).parent/'tests/fixtures/accepted_voice_bindings.json'


def plan_from_blocks(proof):
    return {'status':'ok','reason':None,'cues':[{'cue_id':i,**{k:b[k] for k in ['first_token_id','last_token_id','first_audio_word_id','last_audio_word_id']},
        'lines':[{'first_token_id':b['first_token_id'],'last_token_id':b['last_token_id']}]} for i,b in enumerate(proof['blocks'],1)]}


class SavedVoiceBindingTests(unittest.TestCase):
    def test_three_real_asrs_script_subtitles_and_montage_clock(self):
        for record in read(FIXTURE)['entries']:
            with self.subTest(topic=record['source_directory']):
                script,a=record['script'],record['audio'];aliases=config()['editing']['subtitle_segment']['equivalences']
                proof=binding(script,a['words'],a['fragments'],aliases)
                plan=plan_from_blocks(proof)
                cues=materialize(plan,script,a['words'],a['fragments'],aliases,binding_proof=proof)
                self.assertEqual(' '.join(c['text'] for c in cues),' '.join(f['text'] for f in script['fragments']))
                self.assertEqual(cues[-1]['last_audio_word_id'],len(a['words']))
                # Multiple shots, including the differently recognized word, use the same binding.
                visuals={'fragments':[]};tokens=script_tokens(script)
                for index,fragment in enumerate(script['fragments'],1):
                    local=[t for t in tokens if t['fragment_index']==index]
                    bounds=[b for b in proof['blocks'] if b['fragment_index']==index]
                    cut=bounds[len(bounds)//2]['first_token_id']
                    split=next(t for t in local if t['token_id']==cut)['start']-local[0]['start']
                    visuals['fragments'].append({'shots':[{'shot_id':str(index)+'a','text_start':0,'text_end':split,'text':fragment['text'][:split]},
                        {'shot_id':str(index)+'b','text_start':split,'text_end':len(fragment['text']),'text':fragment['text'][split:]}]})
                timeline=shot_timeline(script,visuals,a['fragments'],a['words'],aliases,binding_proof=proof)
                from video_render import schedule
                scenes,total=schedule(timeline,a['duration_seconds'],30)
                self.assertEqual(scenes[0]['start_frame'],0);self.assertEqual(scenes[-1]['end_frame'],total)
                self.assertAlmostEqual(timeline[-1]['display_end'],a['duration_seconds'])
                source=Path(record['source_directory'])
                if source.exists():
                    current=read(source/'audio_manifest.json')
                    self.assertEqual(current['words'],a['words']);self.assertEqual(current['audio_sha256'],a['audio_sha256'])
                    checked_audio(script,current,source)

    def test_gaps_overlaps_foreign_proofs_and_unconfirmed_openai_are_blocked(self):
        record=read(FIXTURE)['entries'][2];script,a=record['script'],record['audio']
        proof=binding(script,a['words'],a['fragments']);original=plan_from_blocks(proof)
        for mutate in [lambda p:p['cues'][0].update(first_token_id=2),
            lambda p:p['cues'][1].update(first_audio_word_id=1),lambda p:p['cues'].pop(1),
            lambda p:p['cues'][0].update(last_audio_word_id=999)]:
            plan=copy.deepcopy(original);mutate(plan)
            with self.assertRaises(ProductionError):materialize(plan,script,a['words'],a['fragments'],binding_proof=proof)
        other=copy.deepcopy(script);other['script_version_id']='foreign'
        with self.assertRaises(ProductionError):materialize(original,other,a['words'],a['fragments'],binding_proof=proof)
        words=copy.deepcopy(a['words']);words[2]['start']=words[0]['start']
        with self.assertRaises(ProductionError):materialize(original,script,words,a['fragments'],binding_proof=proof)
        source=Path(record['source_directory'])
        if source.exists():
            current=read(source/'audio_manifest.json')
            for mutation in [lambda x:x.update(script_version_id='foreign'),lambda x:x.update(audio_sha256='foreign'),
                lambda x:x.update(openai_transcript={'text':''}),lambda x:x.update(openai_transcript={'text':'Реальная ошибка: не четыре, а пять спутников.'}),lambda x:x.update(text_verified=False)]:
                bad=copy.deepcopy(current);mutation(bad)
                with self.assertRaises(UpstreamBlocked):checked_audio(script,bad,source)

    def test_different_word_counts_need_atomic_verified_boundaries(self):
        script={'script_version_id':'test','fragments':[{'text':'GPS помогает точно определить место','visual':'Map'}]}
        names=['джи','пи','эс','помогает','точно','определить','место']
        words=[{'word_id':i,'word':w,'start':i*.3,'end':i*.3+.2} for i,w in enumerate(names,1)]
        fr=[{'fragment_index':1,'first_word_id':1,'last_word_id':7}]
        proof=binding(script,words,fr)
        self.assertEqual(bound_range(proof,1,1),(1,3))
        plan=plan_from_blocks(proof);cues=materialize(plan,script,words,fr,binding_proof=proof)
        self.assertEqual(cues[0]['text'],'GPS');self.assertEqual(cues[0]['end'],words[2]['end'])
        bad=copy.deepcopy(plan);bad['cues'][0]['last_audio_word_id']=1;bad['cues'][1]['first_audio_word_id']=2
        with self.assertRaises(ProductionError):materialize(bad,script,words,fr,binding_proof=proof)
        script['fragments'][0]['text']='по этому месту';names=['поэтому','месту']
        words=[{'word_id':i,'word':w,'start':i,'end':i+.5} for i,w in enumerate(names,1)]
        fr[0]['last_word_id']=2;proof=binding(script,words,fr)
        self.assertEqual(bound_range(proof,1,2),(1,1))
        with self.assertRaises(ProductionError):bound_range(proof,1,1)

    def test_standalone_punctuation_and_all_saved_texts_have_feasible_local_cues(self):
        from offline_review_check import local_plan
        from media_tools import load_media_settings,MediaError
        try:media=load_media_settings()
        except MediaError as error:self.skipTest(str(error))
        settings=config()['editing'];size=(360,640)
        font=ImageFont.truetype(str(media.font),round(settings['subtitle_layout']['font_pixels_at_1080_width']/3))
        for record in read(FIXTURE)['entries']:
            script,a=record['script'],record['audio'];aliases=settings['subtitle_segment']['equivalences']
            proof=binding(script,a['words'],a['fragments'],aliases)
            plan=local_plan(script,a['words'],proof,settings['subtitle_layout'],font,size)
            cues=materialize(plan,script,a['words'],a['fragments'],aliases,binding_proof=proof)
            self.assertEqual(' '.join(c['text'] for c in cues),' '.join(f['text'] for f in script['fragments']))
            self.assertTrue(all(.6<=c['end']-c['start']<=6 for c in cues))
            self.assertTrue(all(len(c['text'])/(c['end']-c['start'])<=22 for c in cues))

    def test_repeated_or_missing_words_do_not_gain_guessed_internal_times(self):
        script={'fragments':[{'text':'да да верно','visual':'Test'}]}
        words=[{'word_id':1,'word':'да','start':0,'end':1},{'word_id':2,'word':'верно','start':1,'end':2}]
        fr=[{'fragment_index':1,'first_word_id':1,'last_word_id':2}]
        proof=binding(script,words,fr)
        with self.assertRaises(ProductionError):bound_range(proof,1,1)
        self.assertEqual(bound_range(proof,1,3),(1,2))

    def test_saved_voice_aac_duration_uses_sample_clock_without_trimming(self):
        source=Path(read(FIXTURE)['entries'][0]['source_directory'])
        if not source.exists():self.skipTest('Real saved WAV is local; mapping fixtures remain portable.')
        from media_tools import load_media_settings,run_command
        from video_render import compose
        media=load_media_settings();a=read(source/'audio_manifest.json')
        audio=(source/a['audio_path']).resolve();before=sha(audio)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            run_command([media.ffmpeg,'-v','error','-y','-f','lavfi','-i','color=s=64x64:r=30:d=42.066666667','-an','-c:v','libx264','-pix_fmt','yuv420p',root/'silent.mp4'])
            # Isolate the actual audio/AAC clock; full subtitles are exercised separately.
            with patch('video_render.font_filter',return_value='null'):
                candidate,_=compose(root,audio,config()['editing']['compose'],media,[])
            output,_=run_command([media.ffprobe,'-v','error','-show_streams','-of','json',candidate])
            streams=json.loads(output)['streams'];actual=next(v for v in streams if v['codec_type']=='audio')
            self.assertLessEqual(abs(float(actual['duration'])-a['duration_seconds']),1024/48000)
            self.assertEqual(sha(audio),before)

    def test_pixel_reference_uses_actual_centisecond_boundary_not_25fps_rounding(self):
        from media_tools import load_media_settings,run_command,MediaError
        from subtitles import write_subtitles,font_filter
        from video_render import extract_frames
        from video_schedule_check import check_schedule
        import shutil
        try:media=load_media_settings()
        except MediaError as error:self.skipTest(str(error))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'font').mkdir();shutil.copyfile(media.font,root/'font'/media.font.name)
            settings=copy.deepcopy(config()['editing']['subtitle_layout']);settings['font_pixels_at_1080_width']=105
            cues=[{'start':0,'end':.13,'line_text':['ПЕРВЫЙ ТЕКСТ','ПЕРВЫЙ ТЕКСТ']},
                {'start':.13,'end':.4,'line_text':['Другой']}]
            write_subtitles(root,cues,settings,'Arial',(360,640))
            candidate=root/'clock.mp4'
            run_command([media.ffmpeg,'-v','error','-y','-f','lavfi','-i','color=c=blue:s=360x640:r=30:d=0.4',
                '-vf',font_filter(root/'subtitles.ass',root/'font'),'-an','-c:v','libx264','-pix_fmt','yuv420p',candidate])
            Image.new('RGB',(720,1280),'blue').save(root/'original.png')
            motion=prepare(root/'original.png',composition(zoom=0),root/'previews',(360,640))
            assets=[{'original_path':'original.png','motion':motion}]
            scenes=[{'start_frame':0,'end_frame':12,'dissolve_frames':0}]
            samples=[{'frame_number':4,'time':4/30,'frame_id':'boundary','fragment_index':1,'cue_id':2}]
            extract_frames(candidate,root/'frames',samples,media,[])
            self.assertTrue(check_schedule(root,assets,scenes,samples,(360,640),media,[])['passed'])
            def old_clock(args,**kwargs):
                import re
                args=list(args)
                if '-framerate' in args:
                    position=args.index('-framerate');del args[position:position+2]
                return run_command([re.sub(r'setpts=\d+,','setpts=PTS+0.13333333333333333/TB,',v) if isinstance(v,str) else v for v in args],**kwargs)
            with patch('video_schedule_check.run_command',side_effect=old_clock):
                self.assertFalse(check_schedule(root,assets,scenes,samples,(360,640),media,[])['passed'])
            # Exact centisecond boundary: avoid 3.6 s becoming 3599 ms internally.
            cues[0]['end']=3.6;cues[1].update(start=3.6,end=3.8)
            write_subtitles(root,cues,settings,'Arial',(360,640))
            run_command([media.ffmpeg,'-v','error','-y','-f','lavfi','-i','color=c=blue:s=360x640:r=30:d=3.8',
                '-vf',font_filter(root/'subtitles.ass',root/'font'),'-an','-c:v','libx264','-pix_fmt','yuv420p',candidate])
            exact=[{'frame_number':108,'time':3.6,'frame_id':'exact_boundary','fragment_index':1,'cue_id':2}]
            extract_frames(candidate,root/'exact_frames',exact,media,[])
            self.assertTrue(check_schedule(root,assets,[{'start_frame':0,'end_frame':114,'dissolve_frames':0}],exact,(360,640),media,[])['passed'])


class RepairCrashTests(unittest.TestCase):
    def fixture(self,root):
        root=Path(root);source=root/'original.png';Image.new('RGB',(1400,2400),'navy').save(source)
        script={'script_version_id':'saved','fragments':[{'text':'Земля','visual':'Earth'}]}
        settings=config()['visuals'];motion=prepare(source,composition(),root/'previews')
        for p in motion['previews']:p['path']=str(Path(p['path']).relative_to(root))
        meta={**metadata(),'candidate_id':'earth','original_path':'original.png','file_sha256':sha(source)}
        result={'fragment_index':1,'fragment_sha256':digest(script['fragments'][0]),'status':'approved',
            'review':{'decision':'approved'},'asset':{'candidate_id':'earth','original_path':'original.png','original_sha256':sha(source),
            'source':meta,'motion':motion,'changes':'original accepted crop'}}
        runner=VisualMaterials(root,settings,model=lambda *args:(_ for _ in ()).throw(ProductionServiceError('Mock HTTP 503')))
        key=digest({'fragment':script['fragments'][0],'index':1})
        runner.journal.state['jobs'][key]={'key':key,'fragment':script['fragments'][0],'fragment_index':1,'rounds':[],
            'corrections':{},'rejections':[],'seen_ids':['earth'],'seen_hashes':[sha(source)],'status':'approved','result':result,'approved_settings_id':digest(settings)}
        runner.journal.save()
        visuals={'mode':'real','status':'approved','script_version_id':'saved','settings_snapshot':settings,'fragments':[copy.deepcopy(result)]}
        atomic_write_json(root/'visual_materials.json',visuals)
        return script,runner,visuals,key

    def test_failed_api_and_resume_never_restore_old_approval_or_reset_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);script,runner,visuals,key=self.fixture(root);old=copy.deepcopy(visuals)
            with self.assertRaises(ProductionServiceError):runner.repair_approved(script,visuals,{1},[{'problem':'crop'}])
            state=read(root/'visual_materials_state.json');job=state['jobs'][key]
            self.assertEqual(job['corrections']['earth'],1);self.assertEqual(job['status'],'repair_pending')
            self.assertEqual(job['approval_history'][0]['previous_result'],old['fragments'][0])
            with self.assertRaises(UpstreamBlocked):checked_visuals(script,old,root)
            resumed=VisualMaterials(root,runner.settings,model=lambda *args:self.fail('No automatic paid retry'))
            with self.assertRaisesRegex(ProductionError,'Unfinished'):resumed.fragment(script['fragments'][0],1)
            with self.assertRaises(ProductionServiceError):resumed.repair_approved(script,read(root/'visual_materials.json'),{1},[])
            self.assertEqual(resumed.journal.state['jobs'][key]['corrections']['earth'],1)
            self.assertEqual(sha(root/'original.png'),old['fragments'][0]['asset']['original_sha256'])

    def test_interruption_between_state_and_manifest_writes_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);script,runner,visuals,key=self.fixture(root);old=copy.deepcopy(visuals)
            with patch('visual_materials.atomic_write_json',side_effect=KeyboardInterrupt('Crash before public manifest write')):
                with self.assertRaises(KeyboardInterrupt):runner.repair_approved(script,visuals,{1},[])
            self.assertEqual(read(root/'visual_materials.json')['status'],'approved') # public file lags
            self.assertEqual(read(root/'visual_materials_state.json')['jobs'][key]['status'],'repair_pending')
            with self.assertRaises(UpstreamBlocked):checked_visuals(script,old,root)
            from editing_inputs import import_manifest
            destination=root/'import';destination.mkdir()
            with patch('editing_inputs.approved_script',return_value=script):
                with self.assertRaisesRegex(UpstreamBlocked,'revoked'):
                    import_manifest(root/'visual_materials.json',destination,'visuals')
            resumed=VisualMaterials(root,runner.settings,model=lambda *args:self.fail('No API'))
            with self.assertRaises(ProductionError):resumed.fragment(script['fragments'][0],1)
            self.assertEqual(resumed.journal.state['jobs'][key]['corrections'],{})

    def test_shot_workflow_failure_also_keeps_old_approval_revoked(self):
        from shot_materials import ShotMaterials
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);script,runner,visuals,key=self.fixture(root)
            old=copy.deepcopy(visuals['fragments'][0]);shot=dict(old,shot_id='shot1',text=script['fragments'][0]['text'],
                text_start=0,text_end=len(script['fragments'][0]['text']),visual_task='Earth',queries=['Earth'],must_show=[],must_not_show=[],preferences=[])
            job=runner.journal.state['jobs'][key];job.update(workflow_version=2,shots=[shot],approved={'shot1':shot})
            job.pop('result');runner.journal.save()
            visuals['schema_version']=2;visuals['fragments']=[{'fragment_index':1,'fragment_sha256':digest(script['fragments'][0]),'status':'approved','shots':[shot]}]
            atomic_write_json(root/'visual_materials.json',visuals);before=copy.deepcopy(visuals)
            model=lambda *args:(_ for _ in ()).throw(ProductionServiceError('Mock failure'))
            shots=ShotMaterials(root,runner.settings,model=model)
            with self.assertRaises(ProductionError):shots.repair_approved(script,visuals,{1},[{'problem':'crop'}])
            with self.assertRaises(UpstreamBlocked):checked_visuals(script,before,root)
            saved=read(root/'visual_materials_state.json')['jobs'][key]
            self.assertFalse(saved['approved']);self.assertTrue(saved['approval_history'])
            current=read(root/'visual_materials.json');self.assertNotEqual(current['status'],'approved')
            resumed=ShotMaterials(root,runner.settings,model=lambda *args:self.fail('No paid retries'))
            self.assertNotEqual(read(resumed.run(script))['status'],'approved')

    def test_only_successful_fix_and_review_can_reapprove(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);script,runner,visuals,key=self.fixture(root);before=copy.deepcopy(visuals)
            def success(step,settings,schema,data,images):
                if step=='visual_select':return {'fragment_index':1,'status':'selected','primary_candidate_id':'earth',
                    'backup_candidate_id':None,'reason':'Test correction','rejected_candidates':[],'composition':composition(zoom=5)}
                return {'fragment_index':1,'decision':'approved','issues':[],'corrections':None}
            runner.model=success
            current=runner.repair_approved(script,visuals,{1},[{'problem':'crop'}])
            checked_visuals(script,current,root)
            with self.assertRaises(UpstreamBlocked):checked_visuals(script,before,root)
            self.assertNotEqual(current['fragments'][0]['asset']['motion'],before['fragments'][0]['asset']['motion'])
            self.assertEqual(runner.journal.state['jobs'][key]['corrections']['earth'],1)


class ConcurrentBranchTests(unittest.TestCase):
    def prepare(self,root):
        make_script(root);state={'mode':'real','status':'running'};pipeline.update_run(root,state);return state

    def test_both_start_before_either_finishes_and_error_preserves_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state=self.prepare(root);barrier=threading.Barrier(2)
            version=read(root/'script.json')['script_version_id']
            def voice(path,**kwargs):
                self.assertEqual(kwargs['expected_script_version_id'],version);barrier.wait(timeout=5)
                p=root/'audio_manifest.json';atomic_write_json(p,{'status':'completed','script_version_id':version});return p
            def visuals(path,**kwargs):
                self.assertEqual(kwargs['script_snapshot']['script_version_id'],version);barrier.wait(timeout=5)
                raise ProductionServiceError('Mock service failure')
            with patch('pipeline.voiceover_from_file',side_effect=voice),patch('pipeline.run_visual_materials',side_effect=visuals):
                with self.assertRaises(ProductionError):pipeline.run_independent_branches(root,state)
            self.assertEqual(read(root/'audio_manifest.json')['status'],'completed')
            branches=read(root/'run.json')['branches'];self.assertEqual(branches['voiceover']['status'],'completed');self.assertEqual(branches['visuals']['status'],'failed')

    def test_voice_failure_does_not_cancel_successful_visual_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state=self.prepare(root);barrier=threading.Barrier(2)
            version=read(root/'script.json')['script_version_id']
            def voice(*args,**kwargs):
                barrier.wait(timeout=5);raise ProductionError('Voice failure')
            def visuals(*args,**kwargs):
                barrier.wait(timeout=5)
                p=root/'visual_materials.json';atomic_write_json(p,{'status':'approved','script_version_id':version});return p
            with patch('pipeline.voiceover_from_file',side_effect=voice),patch('pipeline.run_visual_materials',side_effect=visuals):
                with self.assertRaises(ProductionError):pipeline.run_independent_branches(root,state)
            self.assertEqual(read(root/'visual_materials.json')['status'],'approved')
            self.assertEqual(read(root/'run.json')['branches']['visuals']['status'],'approved')

    def test_new_voice_script_version_revalidates_visual_snapshot_before_editing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state=self.prepare(root);original=read(root/'script.json');changed=copy.deepcopy(original)
            changed['fragments'][0]['text']='Новый текст'
            from scriptwriter import script_identity,narration_text
            changed['script_version_id']=script_identity({'fragments':changed['fragments']});changed['narration_text']=narration_text({'fragments':changed['fragments']})
            selected=[]
            def voice(*args,**kwargs):
                approved=read(root/'script_state.json');approved['versions'][-1].update(script={'fragments':changed['fragments']},script_version_id=changed['script_version_id'])
                atomic_write_json(root/'script_state.json',approved);atomic_write_json(root/'script.json',changed)
                p=root/'audio_manifest.json';atomic_write_json(p,{'status':'completed','script_version_id':changed['script_version_id']});return p
            def visuals(*args,**kwargs):
                snap=kwargs['script_snapshot'];selected.append(copy.deepcopy(snap))
                p=root/'visual_materials.json';atomic_write_json(p,{'status':'approved','script_version_id':snap['script_version_id']});return p
            with patch('pipeline.voiceover_from_file',side_effect=voice),patch('pipeline.run_visual_materials',side_effect=visuals):
                results=pipeline.run_independent_branches(root,state)
            self.assertEqual([s['script_version_id'] for s in selected],[original['script_version_id'],changed['script_version_id']])
            self.assertEqual(read(results['visuals'])['script_version_id'],changed['script_version_id'])

    def test_concurrent_openai_records_and_run_updates_do_not_lose_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);barrier=threading.Barrier(12)
            def writer(i):
                meter=OpenAIMeter(root,'voiceover' if i%2 else 'visuals','test-key')
                barrier.wait(timeout=5)
                for j in range(5):meter.telemetry['marker']=[i,j];meter.save()
                pipeline.update_run(root,{},**{str(i):True})
                return meter.telemetry['call_id']
            with ThreadPoolExecutor(max_workers=12) as pool:ids=list(pool.map(writer,range(12)))
            records=read(root/'openai_usage.json')['requests']
            self.assertEqual({r['telemetry']['call_id'] for r in records},set(ids));self.assertEqual(len(records),12)
            self.assertTrue(all(read(root/'run.json')[str(i)] for i in range(12)))


if __name__=='__main__':unittest.main()
