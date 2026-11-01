"""Fresh PCM checks shared by normal synthesis, paid recheck and local reassessment."""
import array
import io
import math
import sys
import wave
from pathlib import Path
from audio_files import inspect_wav


def current_audio_checks(path, saved_checks=None):
    content=Path(path).read_bytes()
    measured=inspect_wav(content)
    with wave.open(io.BytesIO(content)) as audio:
        pcm=audio.readframes(audio.getnframes())
    samples=array.array('h',pcm)
    if sys.byteorder!='little':samples.byteswap()
    peak=max(abs(v) for v in samples)/32768
    rms=math.sqrt(sum(v*v for v in samples)/len(samples))/32768
    clipped=sum(abs(v)>=32767 for v in samples)
    issues=[]
    if rms==0:issues.append('WAV содержит только тишину.')
    if clipped:issues.append(f'Клиппинг PCM: {clipped} отсчётов.')
    if isinstance(saved_checks,dict) and saved_checks.get('passed') is False:
        issues.append('Сохранённая независимая техническая проверка не пройдена; решение по тексту её не отменяет.')
    checks={'passed':not issues,'sound_present':rms>0,'clipped_samples':clipped,
        'peak_dbfs':20*math.log10(peak) if peak else None,'rms_dbfs':20*math.log10(rms) if rms else None,
        'container':'WAV','audio_sha256':measured['audio_sha256'],'measurement':'fresh_current_pcm16',
        'sample_rate':measured['sample_rate'],'channels':measured['channels'],'issues':issues}
    return measured,checks
