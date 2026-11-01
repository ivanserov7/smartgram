"""Durable, factual stage observations. No time-based percentage or ETA."""
import json
from pathlib import Path
import time

from file_io import atomic_write_json

STAGES = json.loads((Path(__file__).parent/'server_infra/progress_stages.json').read_text())
ALIASES = {'voiceover_and_visuals':'assets', 'voiceover':'assets', 'visuals':'assets', 'finished':'delivery'}


class JobProgress:
    def __init__(self, directory, clock=time.time):
        self.path=Path(directory)/'progress.json';self.clock=clock;self.value={}
        self.update('research')

    def update(self, stage=None, status='running'):
        now=self.clock();stage=ALIASES.get(stage,stage)
        if stage is None:stage=self.value.get('stage_id','research')
        if stage not in {s['id'] for s in STAGES}:return
        if self.value.get('stage_id')!=stage:
            self.value.update(stage_id=stage,stage_started_at_ms=round(now*1000))
        index=next(i for i,s in enumerate(STAGES) if s['id']==stage)
        self.value.update(version=1,stage=STAGES[index]['label'],stage_index=index,stage_count=len(STAGES),
                          status=status,observed_at_ms=round(now*1000),
                          stage_elapsed_seconds=max(0,(now*1000-self.value['stage_started_at_ms'])/1000))
        if status!='running':self.value['stopped_at_ms']=round(now*1000)
        atomic_write_json(self.path,self.value)

    def fail(self):self.update(status='failed')
