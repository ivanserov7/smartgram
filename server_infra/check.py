"""Offline release checks, run as smartgram-dev, never as root."""
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
if __name__=='__main__':
    subprocess.run([sys.executable,'-m','unittest','test_web_adapter','test_server_import','test_voice_authority','test_visual_sequence','test_review_regressions','test_general_visual_repair','-q'],cwd=ROOT,check=True)
