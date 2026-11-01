"""OpenAI authority, local evidence reuse, fresh PCM checks and real SDK mock HTTP."""
import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import httpx

from file_io import atomic_write_json
from gemini_voice import GeminiVoiceAPI
from test_full_voiceover import FakeVoiceAPI, KEYS, make_script, wav_bytes
import test_gemini_migration
from voice_alignment import literal_evidence, text_authority, validate_check
from voice_errors import AudioAlignmentError, VoiceoverError
from voice_local_recheck import reassess_saved
from voice_recheck import recheck_saved
from voice_settings import VoiceoverSettings
from voiceover import VoiceoverRun


def read(path):return json.loads(path.read_text())
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


class NoAPI:
    def __getattr__(self,name):
        def forbidden(*args,**kwargs):raise AssertionError('API forbidden: '+name)
        return forbidden


class VoiceAuthorityTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name).resolve()
        quiet=redirect_stdout(StringIO());quiet.__enter__();self.addCleanup(quiet.__exit__,None,None,None)
        self.settings=VoiceoverSettings()

    def small_review(self):
        return {'verification_version':2,'decision':'Не уверен','issues':[],
            'fragments':[{'fragment_index':1,'first_word_id':1,'last_word_id':7}],
            'assessments':[{'fragment_index':1,'result':'unresolved','deepgram_supports_script':False,
                'openai_supports_script':False,'same_material_error_in_both':False,'changes_meaning':True,'explanation':'Ошибочный отрицательный GPT.'}]}

    def test_exact_openai_overrides_negative_gpt_and_deepgram_fourth(self):
        text='четвёртое помогает учесть ошибку часов'
        script={'fragments':[{'text':text,'visual':'Часы'}]}
        words=[{'word_id':i+1,'start':i*.2,'end':i*.2+.1} for i in range(7)]
        raw=self.small_review();original=copy.deepcopy(raw)
        review,mapped=validate_check(raw,script,words,2,confirmed=True,
            transcripts=('четвёртая помогает учесть ошибку часов','ЧЕТВЕРТОЕ, помогает учесть ошибку часов!'))
        self.assertEqual(review['decision'],'Можно использовать')
        self.assertEqual(mapped[0]['first_word_id'],1);self.assertEqual(mapped[0]['last_word_id'],7)
        self.assertEqual(mapped[0]['speech_end'],words[-1]['end']);self.assertEqual(raw,original)

    def test_real_number_negation_and_numeric_sign_are_not_normalized_away(self):
        for expected,spoken in [('Четыре спутника','Три спутника'),('Не четыре спутника','Четыре спутника'),
            ('Использует 4 сигнала','Использует 5 сигнала'),('Температура −4 градуса','Температура 4 градуса'),
            ('Число 3,14','Число 314'),('Расхождение 5%','Расхождение 5')]:
            with self.subTest(expected=expected):
                script={'fragments':[{'text':expected,'visual':'Схема'}]}
                self.assertNotEqual(literal_evidence(expected),literal_evidence(spoken))
                self.assertFalse(text_authority(script,(spoken,spoken),self.small_review())['approved'])
                value=self.small_review();value['fragments'][0]['last_word_id']=1
                review,_=validate_check(value,script,[{'start':0,'end':1}],2,confirmed=True,transcripts=(spoken,spoken))
                self.assertEqual(review['decision'],'Не уверен')

    def test_timing_and_word_range_failure_preserve_separate_text_approval(self):
        script={'fragments':[{'text':'Четвёртое','visual':'Часы'}]}
        for word in [{'start':0,'end':99},{'start':-1,'end':1},{'start':0,'end':float('nan')}]:
            with self.subTest(word=word):
                value=self.small_review();value['fragments'][0]['last_word_id']=1
                with self.assertRaises(AudioAlignmentError) as caught:
                    validate_check(value,script,[word],2,transcripts=('Четвёртая','Четвёртое'))
                self.assertTrue(caught.exception.text_verified)
        with self.assertRaises(AudioAlignmentError):
            validate_check(self.small_review(),script,[{'start':0,'end':1}],2,transcripts=('Четвёртая','Четвёртое'))

    def saved_run(self):
        script=make_script(self.root);api=FakeVoiceAPI(decisions=('Не уверен',))
        api.confirm=lambda *args:{'text':script['narration_text']}
        check=api.check
        def negative(*args,**kwargs):
            value=check(*args,**kwargs)
            for i,item in enumerate(value['fragments'],1):item.update(first_word_id=i,last_word_id=i)
            return value
        api.check=negative
        path=VoiceoverRun(self.root,self.settings,KEYS,api).run()
        manifest=read(path);state=read(self.root/'voiceover_state.json')
        raw=next(op['result'] for op in state['operations'].values() if op['stage']=='audio_check')
        manifest.update(status='needs_review',text_verified=False,review=raw,fragments=[])
        atomic_write_json(path,manifest);state['status']='needs_review';atomic_write_json(self.root/'voiceover_state.json',state)
        return manifest,state

    def forbid_network(self):
        stack=ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch('socket.socket.connect',side_effect=AssertionError('Network forbidden')))
        stack.enter_context(patch('socket.getaddrinfo',side_effect=AssertionError('DNS forbidden')))

    def test_local_reassessment_and_both_normal_resumes_make_zero_api_calls(self):
        before,old_state=self.saved_run();audio=self.root/before['audio_path'];original=audio.read_bytes()
        self.forbid_network()
        path=reassess_saved(self.root,self.settings);current=read(path)
        self.assertEqual(current['status'],'completed');self.assertTrue(current['text_verified'])
        receipt=read(self.root/current['local_reassessment']['receipt_path'])
        self.assertEqual(receipt['source_gpt_review']['decision'],'Не уверен')
        self.assertEqual(receipt['new_api_requests'],0)
        state=read(self.root/'voiceover_state.json');self.assertEqual(state['operations'],old_state['operations'])
        self.assertTrue(any(a.get('review_history') for j in state['jobs'] for a in j['attempts']))
        self.assertEqual(VoiceoverRun(self.root,self.settings,KEYS,NoAPI()).run(),path)
        self.assertEqual(recheck_saved(self.root,settings=self.settings,keys=KEYS,api=NoAPI()),path)
        self.assertEqual(reassess_saved(self.root,self.settings),path)
        self.assertEqual(audio.read_bytes(),original)
        self.assertEqual(len(list((self.root/'local_rechecks').glob('*/receipt.json'))),1)
        self.assertEqual(read(self.root/current['local_reassessment']['receipt_path'].replace('receipt.json','manifest_before.json')),before)

    def test_changed_wav_cannot_keep_local_completed_or_call_api(self):
        before,_=self.saved_run();path=reassess_saved(self.root,self.settings)
        audio=self.root/before['audio_path'];audio.write_bytes(wav_bytes(35,marker=0))
        self.forbid_network()
        with self.assertRaises(VoiceoverError):recheck_saved(self.root,settings=self.settings,keys=KEYS,api=NoAPI())
        self.assertNotEqual(read(path)['status'],'completed')

    def test_recheck_fresh_silent_and_clipped_pcm_never_completed(self):
        for content in [wav_bytes(35,marker=0),wav_bytes(35)[:44]+b'\xff\x7f'*(35*24000)]:
            # Construct an actual current WAV defect, without a stale saved failure flag.
            before,_=self.saved_run();path=self.root/'audio_manifest.json';audio=self.root/before['audio_path'];audio.write_bytes(content)
            from audio_files import inspect_wav
            before.update(inspect_wav(content));before['technical_checks']={'passed':True}
            atomic_write_json(path,before)
            with self.assertRaises(VoiceoverError):recheck_saved(self.root,settings=self.settings,keys=KEYS,api=NoAPI())
            current=read(path);self.assertEqual(current['status'],'technical_check_failed');self.assertFalse(current['technical_checks']['passed'])
            # Reset the test directory between defects; these fixtures are never reports.
            import shutil
            for child in list(self.root.iterdir()):
                shutil.rmtree(child) if child.is_dir() else child.unlink()

    def test_recheck_preserves_independent_saved_technical_failure(self):
        before,_=self.saved_run();path=self.root/'audio_manifest.json'
        before['technical_checks']={'passed':False,'reason':'Known technical defect'};atomic_write_json(path,before)
        with self.assertRaises(VoiceoverError):recheck_saved(self.root,settings=self.settings,keys=KEYS,api=NoAPI())
        self.assertEqual(read(path)['status'],'technical_check_failed')

    def test_local_reassessment_bad_timing_does_not_resynthesize(self):
        before,state=self.saved_run();path=self.root/'audio_manifest.json'
        dg=next((self.root/'voiceover').glob('*/deepgram.json'));raw=read(dg)
        raw['results']['channels'][0]['alternatives'][0]['words'][-1]['end']=99;atomic_write_json(dg,raw)
        self.forbid_network();reassess_saved(self.root,self.settings)
        current=read(path);self.assertEqual(current['status'],'alignment_error');self.assertTrue(current['text_verified'])
        with self.assertRaises(VoiceoverError):VoiceoverRun(self.root,self.settings,KEYS,NoAPI()).run()
        self.assertTrue(read(path)['text_verified'])

    def test_missing_saved_asr_is_explicit_failure_not_invented_approval(self):
        before,_=self.saved_run()
        next((self.root/'voiceover').glob('*/openai_transcript.json')).unlink();self.forbid_network()
        with self.assertRaisesRegex(VoiceoverError,'openai_transcript.json'):reassess_saved(self.root,self.settings)
        self.assertNotEqual(read(self.root/'audio_manifest.json')['status'],'completed')


class GeminiHTTPAccountingTests(unittest.TestCase):
    def exercise(self,responses,expected_count,success):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);requests=[];original=httpx.Client
            wav=wav_bytes(1);payload=test_gemini_migration.GeminiMigrationTests.sdk_response(None,wav)
            def handler(request):
                requests.append(request)
                response=responses[min(len(requests)-1,len(responses)-1)]
                if isinstance(response,Exception):raise response
                code,body=response
                return httpx.Response(code,json=payload if body is None else body)
            def factory(**options):return original(transport=httpx.MockTransport(handler),**options)
            with patch('gemini_voice.httpx.Client',side_effect=factory),patch('gemini_voice.time.sleep'):
                api=GeminiVoiceAPI(root,replace(VoiceoverSettings(),tts_max_retries=1),KEYS)
                if success:api.synthesize({'narration_text':'Тест','script_version_id':'fixture'},root)
                else:
                    with self.assertRaises(VoiceoverError):api.synthesize({'narration_text':'Тест','script_version_id':'fixture'},root)
            record=read(root/'gemini_usage.json')['requests'][0]
            self.assertEqual(len(requests),expected_count)
            self.assertEqual(len(record['attempts']),expected_count);self.assertEqual(record['http_request_count'],expected_count)
            self.assertEqual([a['number'] for a in record['attempts']],list(range(1,expected_count+1)))
            self.assertEqual(record['sdk_retries'],0)
            return record

    def test_installed_sdk_transient_503_has_two_http_requests_and_two_log_entries(self):
        record=self.exercise([(503,{'error':{'code':503,'message':'Unavailable','status':'UNAVAILABLE'}}),(200,None)],2,True)
        self.assertEqual([a['http_status'] for a in record['attempts']],[503,200])

    def test_installed_sdk_permanent_billing_and_daily_quota_only_one_http(self):
        for message in ['Billing disabled','Quota exhausted: limit: 0','Daily quota exhausted', 'limit reached']:
            body={'error':{'code':429,'message':message,'status':'RESOURCE_EXHAUSTED','details':[{'quotaValue':'0'}]}}
            self.exercise([(429,body),(200,None)],1,False)

    def test_installed_sdk_timeout_retries_count_every_actual_attempt(self):
        self.exercise([httpx.ReadTimeout('fixture timeout'),(200,None)],2,True)


if __name__=='__main__':unittest.main()
