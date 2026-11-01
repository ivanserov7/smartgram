"""Новый блок речи: подставные API/PCM. Тестовые WAV не являются настоящей речью."""

import base64
import copy
import io
import json
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from audio_files import inspect_wav
from fact_checker import VerificationDraft
from file_io import atomic_write_json
from script_workflow import approved_script, run_scriptwriter
from scriptwriter import ScriptDraft
from test_scriptwriter import fixture
from voice_alignment import validate_check, validate_words
from voice_api import VoiceAPI
from voice_errors import AudioContractError, VoiceoverError
from voice_settings import VoiceoverSettings, load_voiceover_settings
from voiceover import VoiceoverRun

KEYS = {"OPENAI_API_KEY": "test-openai-value", "DEEPGRAM_API_KEY": "test-deepgram-value", "GEMINI_API_KEY": "test-gemini-value"}
FRAGMENTS = [{"text": "Как работает устройство?", "visual": "Само устройство."},
             {"text": "Оно использует 4 сигнала.", "visual": "Схема четырёх сигналов."}]


def wav_bytes(duration, marker=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
        wav.writeframes(bytes([marker, 0]) * int(duration*24000))
    return output.getvalue()


def make_script(directory, revisions=0):
    directory.mkdir(exist_ok=True)
    research = fixture()
    atomic_write_json(directory / "research.json", research)
    atomic_write_json(directory / "run.json", {"mode": "real", "topic": research["topic"]})
    calls = []
    def generate(schema, instructions, data):
        calls.append(schema)
        if schema is ScriptDraft:
            return ScriptDraft(fragments=copy.deepcopy(FRAGMENTS))
        if revisions and len(calls) == 2:
            return VerificationDraft(approved=False, issues=[{"fragment_index": 0, "field": "visual", "phrase": "Само устройство.", "problem": "ТЕСТ", "correction": "Исправьте", "source_url": None}])
        return VerificationDraft(approved=True, issues=[])
    return run_scriptwriter(research, directory, generate)


class FakeVoiceAPI:
    def __init__(self, durations=(35,), decisions=("Можно использовать",)):
        self.durations, self.decisions = list(durations), list(decisions)
        self.syntheses, self.transcriptions, self.checks, self.confirmations = [], [], [], []

    def synthesize(self, script, directory):
        number = len(self.syntheses)
        self.syntheses.append(copy.deepcopy(script))
        content = wav_bytes(self.durations[min(number, len(self.durations)-1)], number+1)
        return content, inspect_wav(content)

    def transcribe(self, path, script_id, audio_hash):
        self.transcriptions.append(audio_hash)
        duration = inspect_wav(path.read_bytes())["duration_seconds"]
        return {"metadata": {"request_id": "fake-deepgram", "duration": duration}, "results": {"channels": [{"alternatives": [{
            "transcript": "Как работает устройство? Оно использует четыре сигнала.", "words": [
                {"word": "устройство", "start": 1.0, "end": 4.0, "confidence": 0.9},
                {"word": "четыре", "start": 5.0, "end": duration-1, "confidence": 0.9}]}]}]}}

    def confirm(self, path, script_id, audio_hash):
        self.confirmations.append(audio_hash)
        return {"text": "Независимая тестовая расшифровка."}

    def check(self, script, transcription, audio_hash, confirmation=None, validation_feedback=None):
        number = len(self.checks)
        self.checks.append((audio_hash, confirmation))
        decision = self.decisions[min(number, len(self.decisions)-1)]
        result = {"decision": decision, "issues": [] if decision == "Можно использовать" else [{"fragment_index": 2,
            "expected": script["fragments"][1]["text"], "observed_deepgram": transcription["transcript"],
            "observed_openai": confirmation['text'] if confirmation else None, "reason": "ТЕСТ: расхождение или сомнение"}],
            "fragments": [{"fragment_index": i, "first_word_id": i if decision != "Не уверен" else None,
                           "last_word_id": i if decision != "Не уверен" else None} for i in (1, 2)]}
        result['verification_version'] = 2
        result['assessments'] = []
        for i in (1, 2):
            confirmed = decision == 'Есть расхождения' and i == 2
            uncertain = decision == 'Не уверен'
            result['assessments'].append({'fragment_index':i,
                'result':'confirmed_material_error' if confirmed else 'unresolved' if uncertain else 'match',
                'deepgram_supports_script':not confirmed and not uncertain,
                'openai_supports_script':not confirmed and not uncertain,
                'same_material_error_in_both':confirmed,'changes_meaning':confirmed or uncertain,
                'explanation':'ТЕСТОВЫЙ структурированный результат, не реальная проверка.'})
        return result


class FullVoiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        quiet = redirect_stdout(io.StringIO());quiet.__enter__();self.addCleanup(quiet.__exit__,None,None,None)
        self.script = make_script(self.root)

    def run_voice(self, api, settings=None, rewrite=None):
        return VoiceoverRun(self.root, settings or VoiceoverSettings(), KEYS, api).run(rewrite)

    def test_whole_text_success_precise_alignment_and_free_resume(self):
        api = FakeVoiceAPI()
        path = self.run_voice(api)
        manifest = json.loads(path.read_text())
        self.assertEqual(len(api.syntheses), 1)
        self.assertEqual(api.syntheses[0]["narration_text"], ' '.join(f["text"] for f in FRAGMENTS))
        self.assertNotIn("Схема", api.syntheses[0]["narration_text"])
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["duration_seconds"], 35)
        self.assertEqual(manifest["fragments"][0]["display_start"], 0)
        self.assertEqual(manifest["fragments"][0]["display_end"], manifest["fragments"][1]["display_start"])
        self.assertEqual(manifest["fragments"][-1]["display_end"], 35)
        self.run_voice(api)
        self.assertEqual(len(api.syntheses), 1);self.assertEqual(len(api.transcriptions), 1);self.assertEqual(len(api.checks), 1)
        self.assertEqual(len(api.confirmations), 1)

    def test_boundaries_30_and_45_are_allowed(self):
        for duration in (30,45):
            with self.subTest(duration=duration):
                directory=self.root/str(duration);make_script(directory)
                path=VoiceoverRun(directory,VoiceoverSettings(),KEYS,FakeVoiceAPI((duration,))).run()
                self.assertEqual(json.loads(path.read_text())["status"],"completed")

    def test_mismatch_confirmed_regenerates_entire_audio_not_script(self):
        api=FakeVoiceAPI((20,35),("Есть расхождения","Можно использовать"))
        self.run_voice(api,rewrite=lambda _:self.fail("Wrongly changing correct script"))
        self.assertEqual(len(api.syntheses),2);self.assertEqual(len(api.confirmations),2);self.assertEqual(len(api.transcriptions),2)
        self.assertEqual(approved_script(self.root)["script_version_id"],self.script["script_version_id"])
        self.assertNotEqual(api.transcriptions[0],api.transcriptions[1])

    def test_regeneration_limit_zero_and_one_persist_on_resume(self):
        for limit in (0,1):
            directory=self.root/str(limit);make_script(directory)
            api=FakeVoiceAPI((35,), ("Есть расхождения",))
            settings=replace(VoiceoverSettings(),max_text_regenerations=limit)
            with self.assertRaisesRegex(VoiceoverError,"лимит"):
                VoiceoverRun(directory,settings,KEYS,api).run()
            count=len(api.syntheses)
            self.assertEqual(count,limit+1)
            with self.assertRaises(VoiceoverError):
                VoiceoverRun(directory,settings,KEYS,api).run()
            self.assertEqual(len(api.syntheses),count)

    def test_conflicting_transcripts_stop_and_confirmation_once(self):
        api=FakeVoiceAPI(decisions=("Не уверен",))
        for _ in range(2):
            with self.assertRaises(VoiceoverError):self.run_voice(api)
        self.assertEqual(len(api.syntheses),1);self.assertEqual(len(api.confirmations),1);self.assertEqual(len(api.checks),1)
        self.assertEqual(json.loads((self.root/'voiceover_state.json').read_text())["status"],"needs_review")

    def test_verified_duration_uses_remaining_script_budget_and_new_version(self):
        # Начальный бюджет уже частично потрачен, перенос/озвучка его не обнуляют.
        state=json.loads((self.root/'script_state.json').read_text())
        state['settings']['max_revisions']=1
        atomic_write_json(self.root/'script_state.json',state)
        feedback=[]
        def rewrite(data):
            feedback.append(data)
            def generate(schema,instructions,data):
                if schema is ScriptDraft:
                    fragments=copy.deepcopy(FRAGMENTS);fragments[0]['text']='Как устроено это устройство?'
                    return ScriptDraft(fragments=fragments)
                return VerificationDraft(approved=True,issues=[])
            run_scriptwriter(fixture(),self.root,generate,duration_feedback=data)
        self.run_voice(FakeVoiceAPI((20,35)),rewrite=rewrite)
        self.assertEqual(feedback[0]['measured_duration_seconds'],20)
        self.assertEqual(json.loads((self.root/'script_state.json').read_text())['revisions_used'],1)

    def test_voice_prompt_and_version_invalidate_cache(self):
        api=FakeVoiceAPI();self.run_voice(api)
        self.run_voice(api,replace(VoiceoverSettings(),tts_voice='cedar'))
        self.assertEqual(len(api.syntheses),2)
        prompt=self.root/'narrator.txt';prompt.write_text('Другой тестовый промпт диктора.')
        self.run_voice(api,replace(VoiceoverSettings(),tts_style='Новый тестовый стиль.'))
        self.assertEqual(len(api.syntheses),3)

    def test_network_unknown_outcome_does_not_repeat_on_resume(self):
        api=FakeVoiceAPI()
        def fail(*args):raise VoiceoverError('ТЕСТ: тайм-аут')
        api.synthesize=unittest.mock.Mock(side_effect=fail)
        for _ in range(2):
            with self.assertRaises(VoiceoverError):self.run_voice(api)
        self.assertEqual(api.synthesize.call_count,1)

    def test_contract_rejects_bad_ids_null_missing_and_gaps(self):
        words=[{'word_id':1,'start':1.,'end':2.},{'word_id':2,'start':3.,'end':4.}]
        valid={'decision':'Можно использовать','issues':[], 'fragments':[{'fragment_index':1,'first_word_id':1,'last_word_id':1},{'fragment_index':2,'first_word_id':2,'last_word_id':2}]}
        for mutation in ('unknown','null','missing','duplicate','gap'):
            bad=copy.deepcopy(valid)
            if mutation=='unknown':bad['fragments'][1]['last_word_id']=3
            elif mutation=='null':bad['fragments'][0]['first_word_id']=None
            elif mutation=='missing':bad['fragments'].pop()
            elif mutation=='duplicate':bad['fragments'][1]['fragment_index']=1
            else:bad['fragments'][1]['first_word_id']=1
            with self.subTest(mutation=mutation),self.assertRaises(AudioContractError):validate_check(bad,self.script,words,35)

    def test_deepgram_chronology_finite_times_and_rounding(self):
        raw=FakeVoiceAPI().transcribe(self.write_wav(),self.script['script_version_id'],'hash')
        for value in (-1,float('nan'),float('inf'),36):
            bad=copy.deepcopy(raw);bad['results']['channels'][0]['alternatives'][0]['words'][1]['end']=value
            with self.subTest(value=value),self.assertRaises(AudioContractError):validate_words(bad,35,.02)
        raw['results']['channels'][0]['alternatives'][0]['words'][1]['end']=35.01
        with self.assertRaises(AudioContractError):validate_words(raw,35,.02)

    def write_wav(self):
        path=self.root/'fixture.wav';path.write_bytes(wav_bytes(35));return path

    def test_environment_overrides_dotenv_and_keys_are_not_snapshotted(self):
        path=self.root/'.env';path.write_text('OPENAI_API_KEY=file-key\nDEEPGRAM_API_KEY=file-deepgram\nVOICE_TTS_VOICE=marin\n')
        with patch.dict('os.environ',{'OPENAI_API_KEY':'env-key','DEEPGRAM_API_KEY':'env-deepgram','GEMINI_API_KEY':'env-gemini','VOICE_TTS_VOICE':'cedar'}):
            keys,settings=load_voiceover_settings(path)
        self.assertEqual(keys['OPENAI_API_KEY'],'env-key');self.assertEqual(settings.tts_voice,'cedar')
        self.assertNotIn('env-key',json.dumps(settings.snapshot()))

    def test_html_has_embedded_wav_download_and_seeking(self):
        from voiceover_report import build_voiceover_report
        self.run_voice(FakeVoiceAPI())
        path=build_voiceover_report({'entries':[{'topic':'Тест <script>','directory':str(self.root)}]},self.root/'report.html')
        text=path.read_text()
        self.assertIn('data:audio/wav;base64,',text);self.assertIn('download=',text);self.assertIn('data-time=',text)
        self.assertNotIn('<script>Тест',text);self.assertNotIn('test-openai-value',text)

    def test_partial_report_includes_topics_not_started_yet(self):
        from voiceover_report import build_voiceover_report
        self.run_voice(FakeVoiceAPI())
        path=build_voiceover_report({'entries':[{'topic':'Готовая','directory':str(self.root)},{'topic':'Ещё не начата'}]},self.root/'partial.html')
        self.assertIn('Не запускалась',path.read_text())

    def test_deepgram_large_end_overrun_stops_without_regeneration(self):
        api=FakeVoiceAPI()
        original=api.transcribe
        def overrun(*args):
            raw=original(*args)
            raw['results']['channels'][0]['alternatives'][0]['words'][-1]['end']=37
            return raw
        api.transcribe=overrun
        with self.assertRaisesRegex(VoiceoverError,'таймкоды'):
            self.run_voice(api)
        self.assertEqual(len(api.syntheses),1);self.assertEqual(len(api.confirmations),1)
        self.assertEqual(json.loads((self.root/'audio_manifest.json').read_text())['status'],'contract_error')

    def test_duration_resume_keeps_already_spent_script_revision(self):
        directory=self.root/'spent';make_script(directory,revisions=1)
        api=FakeVoiceAPI((20,20))
        def rewrite(feedback):
            def generate(schema,instructions,data):
                if schema is ScriptDraft:
                    fragments=copy.deepcopy(FRAGMENTS);fragments[0]['text']='Что объясняет работа устройства?'
                    return ScriptDraft(fragments=fragments)
                return VerificationDraft(approved=True,issues=[])
            run_scriptwriter(fixture(),directory,generate,duration_feedback=feedback)
        with self.assertRaisesRegex(VoiceoverError,'бюджет'):
            VoiceoverRun(directory,VoiceoverSettings(),KEYS,api).run(rewrite)
        self.assertEqual(json.loads((directory/'script_state.json').read_text())['revisions_used'],2)
        self.assertEqual(len(api.syntheses),2)

    def test_batch_is_stable_continues_after_error_and_copies_budget(self):
        import voiceover_batch
        sources=[]
        for index in (1,2,3):
            source=self.root/f'source_{index}';script=make_script(source,revisions=1)
            sources.append({'topic':f'Topic {index}','source_directory':str(source),'source_script_version_id':script['script_version_id']})
        calls=[]
        def run(path):
            calls.append(path)
            if path.parent.name=='topic_1':raise VoiceoverError('Test failure')
        with patch.object(voiceover_batch,'select_sources',return_value=sources):
            first=voiceover_batch.run_batch(script_batch=self.root,output_root=self.root/'batches',run=run)
            second=voiceover_batch.run_batch(script_batch=self.root,output_root=self.root/'batches',run=run)
        self.assertEqual(first,second);self.assertEqual(len(calls),6)
        for index in (1,2,3):
            copied=json.loads((first/f'topic_{index}'/'script_state.json').read_text())
            self.assertEqual(copied['revisions_used'],1)
        self.assertEqual(json.loads((first/'batch.json').read_text())['entries'][2]['status'],'completed')

    def test_realtime_timeout_does_not_save_success_wav(self):
        connection=unittest.mock.MagicMock();connection.__enter__.return_value=connection
        connection.recv.side_effect=TimeoutError('timeout')
        with patch('voice_api.connect',return_value=connection) as connect:
            with self.assertRaises(VoiceoverError):
                VoiceAPI(self.root,VoiceoverSettings(),KEYS).synthesize(self.script,self.root)
        connect.assert_called_once();self.assertFalse((self.root/'audio.wav').exists())

    def test_explicit_confirmation_model_change_invalidates_asr_without_new_tts(self):
        api=FakeVoiceAPI(decisions=('Не уверен',))
        with self.assertRaises(VoiceoverError):self.run_voice(api)
        with self.assertRaises(VoiceoverError):
            self.run_voice(api,replace(VoiceoverSettings(),confirm_model='other-confirmation-model'))
        self.assertEqual(len(api.confirmations),2);self.assertEqual(len(api.syntheses),1)

    def test_existing_working_history_cannot_be_overwritten_if_origin_missing(self):
        from voiceover_batch import copy_working_script
        destination=self.root/'working';make_script(destination,revisions=1)
        before=(destination/'script_state.json').read_bytes()
        with self.assertRaisesRegex(VoiceoverError,'бюджет не сброшен'):
            copy_working_script({'source_directory':str(self.root)},destination)
        self.assertEqual((destination/'script_state.json').read_bytes(),before)


class VoiceProviderTests(unittest.TestCase):
    def test_realtime_chunks_done_and_no_transport_retry(self):
        settings=VoiceoverSettings()
        chunk=base64.b64encode(b'\x01\x00'*100).decode()
        for status in ('completed','incomplete','failed','cancelled'):
            with tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary)
                connection=unittest.mock.MagicMock()
                connection.__enter__.return_value=connection
                connection.recv.side_effect=[json.dumps({'type':'session.updated','session':{'model':settings.tts_model}}),
                    json.dumps({'type':'response.output_audio.delta','delta':chunk}),json.dumps({'type':'response.output_audio.delta','delta':chunk}),
                    json.dumps({'type':'response.done','response':{'status':status,'id':'resp_voice','usage':{'output_tokens':10}}})]
                with patch('voice_api.connect',return_value=connection) as connect:
                    api=VoiceAPI(directory,settings,KEYS)
                    if status=='completed':
                        content,_=api.synthesize({'script_version_id':'test','narration_text':'Только текст.'},directory)
                        self.assertEqual(inspect_wav(content)['frame_count'],200)
                    else:
                        with self.assertRaises(VoiceoverError):api.synthesize({'script_version_id':'test','narration_text':'Только текст.'},directory)
                        self.assertEqual((directory/'partial.pcm').read_bytes(),b'\x01\x00'*200)
                    connect.assert_called_once()
                self.assertNotIn(chunk,(directory/'realtime_events.json').read_text())
                self.assertNotIn(KEYS['OPENAI_API_KEY'],(directory/'openai_usage.json').read_text())

    def test_deepgram_posts_saved_wav_without_script_and_separate_telemetry(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=directory/'audio.wav';content=wav_bytes(1);path.write_bytes(content);sent=[]
            def handler(request):
                sent.append(request)
                return httpx.Response(200,json={'metadata':{'request_id':'dg_1'},'results':{}})
            client=httpx.Client(transport=httpx.MockTransport(handler))
            with patch('voice_api.httpx.Client',return_value=client):
                VoiceAPI(directory,VoiceoverSettings(),KEYS).transcribe(path,'script',inspect_wav(content)['audio_sha256'])
            self.assertEqual(sent[0].content,content);self.assertEqual(sent[0].url.params['language'],'ru')
            self.assertEqual(sent[0].url.params['smart_format'],'false');self.assertNotIn('prompt',str(sent[0].url))
            self.assertTrue((directory/'deepgram_usage.json').exists());self.assertFalse((directory/'openai_usage.json').exists())

    def test_openai_confirmation_languages_no_prompt_one_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=directory/'audio.wav';path.write_bytes(wav_bytes(1));sent=[]
            def handler(request):
                sent.append(request);return httpx.Response(200,json={'text':'Независимый текст.'})
            client=httpx.Client(transport=httpx.MockTransport(handler))
            with patch('voice_api.httpx.Client',return_value=client):
                VoiceAPI(directory,VoiceoverSettings(),KEYS).confirm(path,'script',inspect_wav(path.read_bytes())['audio_sha256'])
            self.assertEqual(len(sent),1);self.assertIn(b'name="languages[]"',sent[0].content)
            self.assertNotIn(b'name="prompt"',sent[0].content)


if __name__=='__main__':unittest.main()
