"""Редкий локальный статус долгого этапа; никаких запросов к API ради прогресса."""

from contextlib import contextmanager
from threading import Event, Thread
import time


@contextmanager
def stage_progress(label, interval=30):
    started = time.monotonic()
    stop = Event()
    print(f"{label}: начало", flush=True)

    def waiting():
        while not stop.wait(interval):
            print(
                f"{label}: выполняется, прошло {time.monotonic()-started:.0f} с",
                flush=True,
            )

    thread = Thread(target=waiting, daemon=True)
    thread.start()
    try:
        yield
    except BaseException:
        print(f"{label}: остановка через {time.monotonic()-started:.1f} с", flush=True)
        raise
    else:
        print(f"{label}: завершено за {time.monotonic()-started:.1f} с", flush=True)
    finally:
        stop.set()
        thread.join(timeout=1)


def run_with_progress(label, function, *args, **kwargs):
    with stage_progress(label):
        return function(*args, **kwargs)
