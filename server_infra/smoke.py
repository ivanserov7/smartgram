"""One editing-only smoke on accepted media. No provider access or fake approval."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time

ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    os.environ['SMARTGRAM_FFMPEG_THREADS']='1'
    from editing_inputs import snapshot_inputs
    from production import continue_production
    from production_state import ModelClient
    from file_io import atomic_write_json
    # This is a guard at the real shared provider boundary, not an approving stub.
    def deny(*args,**kwargs):raise RuntimeError('PAID_CHECK_REQUIRED: external model call blocked during migration; no approval supplied.')
    ModelClient.__call__=deny
    run=snapshot_inputs(a.source,a.output)
    if (a.source/'editing_state.json').exists():shutil.copy2(a.source/'editing_state.json',run/'editing_state.json')
    started=time.monotonic();reason=None
    try:continue_production(run,stage='editing')
    except Exception as error:reason=str(error)
    quality=json.loads((run/'quality_report.json').read_text()) if (run/'quality_report.json').exists() else {}
    result={'editing_only':True,'existing_media_only':True,'paid_requests':0,'elapsed_seconds':time.monotonic()-started,
        'quality_passed':quality.get('ready') is True,'technical_passed':quality.get('technical',{}).get('passed') is True,
        'reason':reason,'run_directory':str(run)}
    atomic_write_json(run/'migration_smoke.json',result);print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(0 if result['quality_passed'] else 2)
