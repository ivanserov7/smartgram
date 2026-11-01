import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from web_adapter import validate_request, prepare_job, execute


def request(**changes):
    return dict({'contract_version':1,'id':str(uuid.uuid4()),'topic':'Почему закат красный?',
        'target_seconds':45,'output_size':'1080x1920','audience':'general','tone':'clear','subtitles':True,'notes':''},**changes)


class AdapterTests(unittest.TestCase):
    def test_all_supported_form_values_are_applied_per_job(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{},clear=False):
            for seconds in [30,45,60]:
                for size in ['720x1280','1080x1920']:
                    r=request(target_seconds=seconds,output_size=size,audience='teen',tone='story',notes='$(touch unsafe); пожелания')
                    root=Path(temp)/r['id'];root.mkdir();env=prepare_job(root,r)
                    config=json.loads((root/'runtime_config.json').read_text());brief=json.loads((root/'job_brief.json').read_text())
                    self.assertEqual(list(config['visuals']['preparation']['output'].values()),list(map(int,size.split('x'))))
                    self.assertEqual(brief,{'language':'ru','audience':'teen','tone':'story','additional_wishes':r['notes']})
                    self.assertEqual(int(env['SCRIPT_TARGET_MIN_SECONDS']),seconds-3)
                    self.assertEqual(json.loads((root/'applied_request.json').read_text()),r)

    def test_unsupported_subtitle_mode_is_explicitly_rejected_before_pipeline(self):
        with self.assertRaisesRegex(ValueError,'без субтитров'):validate_request(request(subtitles=False))

    def test_invalid_fields_and_extra_fields_rejected(self):
        for changes in [{'target_seconds':True},{'target_seconds':20},{'tone':'other'},{'audience':'other'},
                        {'output_size':'1920x1080'},{'subtitles':'yes'},{'id':'../escape'},{'topic':''},{'contract_version':True},{'extra':'ignored'}]:
            with self.assertRaises((ValueError,TypeError,AttributeError)):validate_request(request(**changes))

    def test_adapter_uses_pipeline_once_and_only_accepts_hash_checked_ready_video(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{},clear=False):
            r=request();job=Path(temp)/r['id'];job.mkdir();(job/'request.json').write_text(json.dumps(r));seen=[]
            def pipeline(**kwargs):
                seen.append(kwargs);run=kwargs['output_root']/'saved';run.mkdir(parents=True)
                payload=b'test fixture, not real generated video';(run/'video.mp4').write_bytes(payload)
                (run/'run.json').write_text('{}');(run/'quality_report.json').write_text(json.dumps({'ready':True,'technical':{'passed':True}}))
                (run/'render_manifest.json').write_text(json.dumps({'mode':'real','status':'ready','size':[1080,1920],'output_hashes':{'video.mp4':hashlib.sha256(payload).hexdigest()}}));return run
            execute(job,pipeline=pipeline)
            result=json.loads((job/'result.json').read_text());self.assertTrue(result['quality_passed']);self.assertEqual(len(seen),1)
            self.assertEqual(seen[0]['topic'],r['topic']);self.assertFalse((job/'video.mp4').is_symlink())
            with self.assertRaisesRegex(ValueError,'уже запускалось'):execute(job,pipeline=pipeline)
            self.assertEqual(len(seen),1)

    def test_pipeline_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{},clear=False):
            r=request();job=Path(temp)/r['id'];job.mkdir();(job/'request.json').write_text(json.dumps(r));seen=[]
            def failed(**kwargs):seen.append(1);raise RuntimeError('provider unavailable')
            with self.assertRaises(RuntimeError):execute(job,pipeline=failed)
            with self.assertRaises(ValueError):execute(job,pipeline=failed)
            self.assertEqual(len(seen),1);self.assertFalse((job/'video.mp4').exists())

    def test_brief_reaches_existing_scriptwriter_as_data(self):
        import scriptwriter
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'brief.json';brief={'language':'ru','audience':'teen','tone':'story','additional_wishes':'literal $()'};path.write_text(json.dumps(brief))
            seen=[]
            def generate(schema,instructions,data):seen.append(data);return {}
            with patch.dict(os.environ,{'SMARTGRAM_JOB_BRIEF_FILE':str(path)}),patch.object(scriptwriter,'require_usable_research'),patch.object(scriptwriter,'validate_script',return_value={}),patch.object(scriptwriter,'read_script_prompt',return_value='saved prompt'):
                scriptwriter.write_script({'topic':'test'},generate=generate)
            self.assertEqual(seen[0]['video_request'],brief)


if __name__=='__main__':unittest.main()
