"""Resolve imported Mac references without rewriting historical manifests."""
import argparse
import hashlib
import json
from pathlib import Path


def resolve_reference(value, mapping, expected_sha256=None):
    original=Path(value)
    if not original.is_absolute():raise ValueError('Expected an absolute historical reference.')
    for old,new in mapping.items():
        try:relative=original.relative_to(old)
        except ValueError:continue
        base=Path(new).resolve();path=(base/relative).resolve()
        if not path.is_relative_to(base) or not path.is_file():raise ValueError('Imported reference missing or outside imported root.')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if expected_sha256 and digest!=expected_sha256:raise ValueError('Imported reference hash mismatch.')
        return {'original_path':str(original),'path':str(path),'sha256':digest}
    raise ValueError('No explicit migration mapping for reference.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('reference');p.add_argument('--sha256');p.add_argument('--mapping',type=Path,default=Path('/etc/smartgram/import-paths.json'));a=p.parse_args()
    print(json.dumps(resolve_reference(a.reference,json.loads(a.mapping.read_text()),a.sha256),ensure_ascii=False))
