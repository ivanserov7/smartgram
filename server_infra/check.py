"""Offline release checks, run as smartgram-dev, never as root."""
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
if __name__=='__main__':
    subprocess.run([sys.executable,ROOT/'server_infra/ci.py'],cwd=ROOT,check=True)
