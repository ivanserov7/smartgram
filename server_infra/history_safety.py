"""Reject runtime/secrets/media paths anywhere in the published Git history."""
from pathlib import PurePosixPath
import subprocess

FORBIDDEN_PARTS={'.env','.venv','venv','.ssh','.codex','outputs','output','node_modules','__pycache__'}
FORBIDDEN_SUFFIXES={'.pem','.key','.log','.mp4','.wav','.mp3','.zip','.bundle','.png','.jpg','.jpeg','.webp'}


def forbidden(name):
    p=PurePosixPath(name)
    return bool(set(p.parts)&FORBIDDEN_PARTS or p.name=='auth.json' or p.suffix.lower() in FORBIDDEN_SUFFIXES
                or p.name.startswith('.env.') and p.name!='.env.example')


if __name__=='__main__':
    revisions=subprocess.check_output(['git','rev-list','--all']).decode().splitlines()
    paths=set()
    for rev in revisions:
        paths.update(subprocess.check_output(['git','ls-tree','-rz','--name-only',rev]).decode().strip('\0').split('\0'))
    bad=sorted(p for p in paths if forbidden(p))
    if bad:raise SystemExit('Forbidden paths in history: '+', '.join(bad))
    print(f'History path check passed: {len(revisions)} revisions, {len(paths)} paths.')
