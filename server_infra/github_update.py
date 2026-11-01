#!/usr/bin/python3
"""Forced SSH command for Actions; fast-forward a clean dev tree, check, drain/deploy."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

DEV=Path('/opt/smartgram/dev')
DATA=Path('/var/lib/smartgram-dev')


def requested_revision(command):
    match=re.fullmatch(r'publish ([a-f0-9]{40})',command)
    if not match:raise ValueError('Only publish <40-character commit> is allowed.')
    return match[1]


def update_checkout(directory, revision):
    def git(*args):return subprocess.check_output(['git','-C',str(directory),*args],stderr=subprocess.STDOUT).decode().strip()
    if git('status','--porcelain'):raise ValueError('Uncommitted server changes preserved; automatic deployment refused.')
    if git('branch','--show-current')!='main':raise ValueError('Server dev branch is not main; preserved without switching.')
    git('fetch','--no-tags','origin','refs/heads/main:refs/remotes/origin/main')
    if git('rev-parse','refs/remotes/origin/main')!=revision:raise ValueError('This run is stale: main has a different commit.')
    if subprocess.run(['git','-C',str(directory),'merge-base','--is-ancestor','HEAD',revision],capture_output=True).returncode:
        raise ValueError('Server and GitHub histories diverged; no reset or overwrite performed.')
    git('merge','--ff-only','--no-edit',revision)
    return git('rev-parse','HEAD')


def main():
    revision=requested_revision(os.environ.get('SSH_ORIGINAL_COMMAND',''))
    DATA.mkdir(exist_ok=True)
    with (DATA/'github-update.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        os.nice(10)
        update_checkout(DEV,revision)
        log=DATA/('github-check-'+revision+'.log')
        with log.open('w') as stream:
            log.chmod(0o600)
            subprocess.run(['sudo','-n','/usr/local/sbin/smartgram','check'],stdout=stream,stderr=subprocess.STDOUT,check=True)
        print(json.dumps({'checked':revision}),flush=True)
        subprocess.run(['sudo','-n','/usr/local/sbin/smartgram','deploy',revision],check=True)
        (DATA/'github-last-deploy.json').write_text(json.dumps({'revision':revision,'completed_at':time.time(),'passed':True}))


if __name__=='__main__':
    try:main()
    except Exception as error:
        # Avoid dumping fetched URLs, credentials or the test log into Actions output.
        print(str(error) if isinstance(error,ValueError) else 'Deployment failed; inspect private server diagnostics.',file=sys.stderr)
        raise SystemExit(1)
