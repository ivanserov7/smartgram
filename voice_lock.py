"""One owner for a run's speech state, independent of the visual worker."""

from contextlib import contextmanager
from pathlib import Path

from file_io import LockBusyError, exclusive_lock
from voice_errors import VoiceoverError


@contextmanager
def voice_lock(directory):
    """Reject overlapping synthesis/rechecks before requests or state changes."""
    try:
        with exclusive_lock(
            Path(directory) / ".voiceover.lock",
            "Озвучка или проверка речи уже выполняется в этой папке. Новые API-запросы не отправлены.",
        ):
            yield
    except LockBusyError as error:
        raise VoiceoverError(str(error)) from None
