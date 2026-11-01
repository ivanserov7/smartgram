"""Точка входа Smartgram: чтение аргументов и запуск конвейера."""

import argparse
import sys
from pathlib import Path

from pipeline import print_execution_plan, run_pipeline, resume_script_run
from research import ResearchError
from voice_errors import VoiceoverError
from voiceover import voiceover_from_file
from commons_api import VisualError
from visuals import collect_visuals
from visual_review import review_visuals
from production import continue_production
from production_state import ProductionError
from media_tools import MediaError


def parse_arguments() -> argparse.Namespace:
    """Читает тему и режим запуска из командной строки."""
    parser = argparse.ArgumentParser(
        description="Smartgram — конвейер познавательных вертикальных видео."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--resume-production", type=Path, help="Продолжить материалы и монтаж без повторной генерации исследования/сценария/озвучки")
    source.add_argument("--topic", help="Тема будущего видео")
    source.add_argument("--research-file", type=Path, help="Продолжить из research.json без повторного поиска")
    source.add_argument("--resume-script", type=Path, help="Продолжить только сценариста в папке запуска, сохраняя бюджет доработок")
    source.add_argument("--scenes-file", type=Path, help="Совместимый вход: озвучить весь связанный текущий сценарий одним WAV")
    source.add_argument("--script-file", type=Path, help="Только озвучка одобренного script.json: Gemini TTS, две ASR, GPT-проверка и привязка")
    source.add_argument("--visuals-file", type=Path, help="Подобрать изображения по scenes.json без исследования и озвучки")
    source.add_argument("--review-visuals-file", type=Path, help="Проверить картинки через OpenAI Vision по scenes.json")
    parser.add_argument("--production-stage", choices=["visuals", "editing", "all"], default="all", help="Этап для --resume-production; visuals не вызывает озвучку и монтаж")
    parser.add_argument("--demo", action="store_true", help="ТЕСТ: подставные ответы без сети и API")
    parser.add_argument("--stop-after", choices=["research", "script", "voiceover"], help="Остановиться после исследования, сценариста или полной проверенной озвучки")
    parser.add_argument("--demo-case", choices=["success", "repair", "fail", "unavailable", "insufficient"],
                        default="success", help="Сценарий демонстрации (только с --demo)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать план без вызовов API и создания видео",
    )
    arguments = parser.parse_args()
    if arguments.production_stage != "all" and not arguments.resume_production:
        parser.error("--production-stage применяется только к --resume-production.")
    if arguments.stop_after and not (arguments.topic or arguments.research_file):
        parser.error("--stop-after применяется к --topic или --research-file.")
    if arguments.topic is not None:
        arguments.topic = arguments.topic.strip()
        if not arguments.topic:
            parser.error("Тема --topic не должна быть пустой.")
    if arguments.demo_case != "success" and not arguments.demo:
        parser.error("--demo-case используется только вместе с --demo.")
    if (arguments.scenes_file or arguments.script_file or arguments.visuals_file or arguments.review_visuals_file) and arguments.demo_case != "success":
        parser.error("--demo-case не используется при отдельном запуске озвучки или подбора изображений.")
    return arguments


def main() -> int:
    """Выбирает режим работы и возвращает код завершения."""
    arguments = parse_arguments()
    if arguments.dry_run:
        if arguments.resume_production:
            print(f"Dry-run: продолжить этап {arguments.production_stage}; лимиты сохраняются. API не вызываются.")
            return 0
        if arguments.resume_script:
            print("Dry-run: продолжение только сценариста с сохранённым бюджетом. Файлы не читаются, API не вызываются.")
            return 0
        if arguments.stop_after:
            print(f"План: {'новое исследование' if arguments.topic else 'сохранённое исследование'} → остановка после {arguments.stop_after}.")
            print("Dry-run: файлы не читаются, API не вызываются.")
            return 0
        if arguments.review_visuals_file:
            print(f"План: передать картинки и текст сцен из {arguments.review_visuals_file} в OpenAI Vision.")
            print("Dry-run: файлы не читаются, API не вызывается.")
            return 0
        if arguments.visuals_file:
            print(f"План: подобрать JPEG/PNG по {arguments.visuals_file}, сохранить visual_manifest.json и атрибуцию.")
            print("Dry-run: файлы не читаются, сеть не используется, изображения не скачиваются.")
            return 0
        if arguments.scenes_file or arguments.script_file:
            print("План: текущий одобренный сценарий → один WAV Gemini TTS → Nova-2 и независимый OpenAI ASR → GPT-проверка и привязка фрагментов.")
            print("Dry-run: файлы не читаются, API не вызываются, аудио не создаётся.")
            return 0
        print_execution_plan(arguments.topic or f"Из файла {arguments.research_file}")
        if arguments.demo:
            print("Выбран тестовый режим; подставные ответы тоже не выполнялись.")
        return 0

    try:
        if arguments.resume_production:
            continue_production(arguments.resume_production, arguments.production_stage)
        elif arguments.resume_script:
            resume_script_run(arguments.resume_script, demo=arguments.demo)
        elif arguments.review_visuals_file:
            review_visuals(arguments.review_visuals_file, demo=arguments.demo)
        elif arguments.visuals_file:
            collect_visuals(arguments.visuals_file, demo=arguments.demo)
        elif arguments.scenes_file or arguments.script_file:
            voiceover_from_file(arguments.script_file or arguments.scenes_file, demo=arguments.demo)
        else:
            run_pipeline(arguments.topic, research_file=arguments.research_file,
                         demo=arguments.demo, demo_case=arguments.demo_case, stop_after=arguments.stop_after)
    except (ResearchError, VoiceoverError, VisualError, ProductionError, MediaError) as error:
        print(f"Конвейер остановлен: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("Не удалось прочитать настройки или сохранить файлы. Проверьте права и место на диске.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
