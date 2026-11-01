from pathlib import Path
import subprocess
import tempfile
import unittest

from server_infra.github_update import requested_revision,update_checkout
from server_infra.history_safety import forbidden


class GitHubUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.remote=self.root/'remote';self.dev=self.root/'dev'
        self.git(self.root,'init','--bare','--initial-branch=main',str(self.remote))
        self.git(self.root,'clone',str(self.remote),str(self.dev))
        self.git(self.dev,'config','user.email','offline@example.invalid');self.git(self.dev,'config','user.name','Offline Test')
        self.commit('first');self.git(self.dev,'push','origin','main');self.initial=self.git(self.dev,'rev-parse','HEAD')
    def git(self,path,*args):return subprocess.check_output(['git','-C',str(path),*args],stderr=subprocess.DEVNULL).decode().strip()
    def commit(self,text):
        (self.dev/'code.txt').write_text(text);self.git(self.dev,'add','code.txt');self.git(self.dev,'commit','-m',text)
        return self.git(self.dev,'rev-parse','HEAD')
    def ahead(self):
        rev=self.commit('second');self.git(self.dev,'push','origin','main');self.git(self.dev,'reset','--hard',self.initial);return rev
    def test_clean_checkout_advances_to_exact_remote_revision(self):
        rev=self.ahead();self.assertEqual(update_checkout(self.dev,rev),rev)
    def test_dirty_checkout_is_preserved(self):
        rev=self.ahead();(self.dev/'code.txt').write_text('uncommitted server change')
        with self.assertRaisesRegex(ValueError,'Uncommitted'):update_checkout(self.dev,rev)
        self.assertEqual((self.dev/'code.txt').read_text(),'uncommitted server change');self.assertEqual(self.git(self.dev,'rev-parse','HEAD'),self.initial)
    def test_diverged_checkout_and_stale_actions_run_cannot_overwrite(self):
        rev=self.ahead();local=self.commit('local work')
        with self.assertRaisesRegex(ValueError,'diverged'):update_checkout(self.dev,rev)
        self.assertEqual(self.git(self.dev,'rev-parse','HEAD'),local)
        with self.assertRaisesRegex(ValueError,'stale'):update_checkout(self.dev,self.initial)
    def test_forced_command_rejects_shell_and_noncommit_inputs(self):
        self.assertEqual(requested_revision('publish '+'a'*40),'a'*40)
        for command in ['publish main','status','publish '+'a'*40+'; id','publish '+'a'*40+'\n','bash','']:
            with self.assertRaises(ValueError):requested_revision(command)
    def test_runtime_and_secret_paths_are_forbidden_but_examples_allowed(self):
        for name in ['.env','sub/.env.production','.codex/auth.json','auth.json','private.key','outputs/run.json','logs/request.log','video.mp4']:
            self.assertTrue(forbidden(name),name)
        for name in ['.env.example','.github/workflows/smartgram.yml','test_voice_authority.py']:
            self.assertFalse(forbidden(name),name)
