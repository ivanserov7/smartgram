"""Исследователь → сценарист (текст, visual, проверка) → озвучка и материалы."""

from pathlib import Path
import json
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed

from artifacts import load_research, save_artifact, save_json, validate_research
from research import ResearchError, research_topic, save_research
from scene_planner import scenes_from_script
from voice_errors import VoiceoverError
from voiceover import voiceover_from_file
from commons_api import VisualError
from visuals import collect_visuals
from visual_review import review_visuals
from visual_materials import run_visual_materials
from editing import run_editing
from production_state import ProductionError
from media_tools import MediaError
from progress import run_with_progress
from script_config import ScriptSettings, load_script_settings
from script_requests import ScriptClient
from script_workflow import run_scriptwriter, approved_script
from file_io import atomic_write_json, json_lock


# Границы этапов не зависят от поставщиков API: каждый обработчик
# выполняет одну задачу и передаёт результат дальше.
# Этот список описывает план, а не выполненные операции.
PIPELINE_STAGES = (
    (
        "Исследование темы",
        "GPT и веб-поиск: собрать факты и ссылки на источники.",
    ),
    (
        "Сценарий и проверка утверждений",
        "Написать fragments с text и visual; отдельным веб-запросом проверить всю версию.",
    ),
    (
        "Визуальные материалы и озвучка",
        "Один WAV Gemini TTS → Nova-2 и независимый OpenAI ASR → GPT-проверка и реальные таймкоды фрагментов. "
        "Подобрать JPEG/PNG Wikimedia Commons с атрибуцией и проверить картинки через OpenAI Vision. "
        "Подготовить оригиналы, кадрирование и три превью; автоматическая проверка композиции.",
    ),
    (
        "Монтаж",
        "FFmpeg: видеоряд по display_times, смысловые субтитры и измерение libass, H.264/AAC.",
    ),
    (
        "Проверка и сохранение",
        "Проверить результат и сохранить MP4, источники и атрибуцию материалов.",
    ),
)


def print_execution_plan(topic: str) -> None:
    """Показывает будущие действия без сети и создания файлов."""
    print(f"Тема: {topic}")
    print("Режим dry-run: только план. API не вызываются.")
    for number, (name, description) in enumerate(PIPELINE_STAGES, start=1):
        status = "РЕАЛИЗОВАНО; ТРЕБУЕТ ПРИНЯТЫХ ВХОДОВ"
        print(f"\n{number}. {name} [{status}]")
        print(f"   {description}")
    print("\nНикакие проверки не выполнены. Видео и материалы не созданы.")
    print("Обычный запуск доходит до MP4 только после принятия всех входов, технических проверок и всех партий кадров.")
    print("--research-file продолжает с сохранённого исследования без повторного поиска.")


def update_run(directory: Path, state: dict, **changes) -> None:
    """Обновляет короткий журнал: видно, на каком этапе остановилась работа."""
    path=directory/'run.json'
    with json_lock(path):
        latest=json.loads(path.read_text()) if path.exists() else dict(state)
        latest.update(changes)
        atomic_write_json(path,latest)
        state.update(latest)


def write_and_check(research: dict, directory: Path, state: dict, generate=None, demo=False, duration_feedback=None) -> tuple:
    """Ровно два смысловых шага. Старый планировщик модель не вызывает."""
    settings = ScriptSettings() if demo else load_script_settings()[1]
    saved = directory / "script_state.json"
    if saved.exists():
        try:
            settings = ScriptSettings(**json.loads(saved.read_text())["settings"])
        except (OSError, ValueError, TypeError, KeyError):
            raise ResearchError("Не удалось прочитать сохранённые настройки сценариста.") from None
    client = None
    update_run(directory, state, stage="script")
    try:
        if generate is None:
            client = ScriptClient(directory, settings)
            generate = client.generate
        script = run_scriptwriter(research, directory, generate, settings=settings, demo=demo, duration_feedback=duration_feedback)
        return script, json.loads((directory / "verification.json").read_text())
    finally:
        if client is not None:
            client.close()


def save_branch_snapshot(directory, snapshot):
    from production_state import digest,sha
    from file_io import atomic_write
    inputs=directory/'branch_snapshots'/digest(snapshot)
    inputs.mkdir(parents=True,exist_ok=True)
    if (inputs/'script.json').exists() and (inputs/'script_state.json').exists():
        if approved_script(inputs)!=snapshot:raise ProductionError('Saved branch approval changed.')
        return inputs
    names=['script.json','script_state.json']
    originals={name:(directory/name).read_bytes() for name in names}
    if approved_script(directory)!=snapshot:raise ProductionError('Script changed while taking branch snapshot.')
    for name,content in originals.items():atomic_write(inputs/name,content)
    if any((directory/name).read_bytes()!=content for name,content in originals.items()) or approved_script(inputs)!=snapshot:
        raise ProductionError('Concurrent script change; branch snapshot rejected before API.')
    atomic_write_json(inputs/'approved_script.json',snapshot)
    atomic_write_json(inputs/'source_hashes.json',{name:sha(inputs/name) for name in names})
    return inputs


def retain_branch_result(directory,name,path):
    from production_state import sha
    from file_io import atomic_write
    path=Path(path);content=path.read_bytes()
    history=directory/'branch_results'/name
    history.mkdir(parents=True,exist_ok=True)
    import hashlib
    saved=history/(hashlib.sha256(content).hexdigest()+'.json')
    if not saved.exists():atomic_write(saved,content)
    if saved.read_bytes()!=content:raise ProductionError('Branch result history changed.')
    return str(saved)


def run_independent_branches(directory, state):
    """Both workers get one approved version; only the coordinator publishes branch status."""
    directory=Path(directory)
    snapshot=copy.deepcopy(approved_script(directory))
    version=snapshot['script_version_id']
    # Preserve the exact approved input even if voice duration feedback revises it.
    from production_state import digest, read
    save_branch_snapshot(directory,snapshot)
    branches={name:{'status':'running','input_script_version_id':version} for name in ('voiceover','visuals')}
    update_run(directory,state,stage='voiceover_and_visuals',branches=copy.deepcopy(branches))
    errors={};results={}
    with ThreadPoolExecutor(max_workers=2,thread_name_prefix='smartgram') as pool:
        futures={pool.submit(run_with_progress,'Озвучка и проверка речи',voiceover_from_file,directory/'script.json',expected_script_version_id=version):'voiceover',
            pool.submit(run_with_progress,'Подбор и проверка видеоряда',run_visual_materials,directory,script_snapshot=snapshot):'visuals'}
        for future in as_completed(futures):
            name=futures[future]
            try:
                path=future.result();results[name]=path
                result=read(path)
                branches[name]['history_path']=retain_branch_result(directory,name,path)
                branches[name].update(status=result.get('status','completed'),result_path=str(path),output_script_version_id=result.get('script_version_id'))
            except Exception as error:
                errors[name]=error
                failed_path=directory/('audio_manifest.json' if name=='voiceover' else 'visual_materials.json')
                if failed_path.exists():branches[name]['history_path']=retain_branch_result(directory,name,failed_path)
                branches[name].update(status='failed',reason=str(error),error_type=type(error).__name__)
            update_run(directory,state,branches=copy.deepcopy(branches))
    # Reconcile visuals even if their sibling failed after creating a revised script.
    # Unchanged fragment jobs are reusable; changed text/visual gets a new content key.
    current=approved_script(directory)
    if current['script_version_id']!=version:
        branches['visuals'].setdefault('history',[]).append(copy.deepcopy(branches['visuals']))
        branches['visuals'].update(status='revalidating',input_script_version_id=current['script_version_id'])
        update_run(directory,state,branches=copy.deepcopy(branches))
        try:
            save_branch_snapshot(directory,current)
            results['visuals']=run_visual_materials(directory,script_snapshot=copy.deepcopy(current))
            value=read(results['visuals'])
            branches['visuals']['history_path']=retain_branch_result(directory,'visuals',results['visuals'])
            branches['visuals'].update(status=value['status'],output_script_version_id=value['script_version_id'])
            errors.pop('visuals',None)
        except Exception as error:
            errors['visuals']=error;branches['visuals'].update(status='failed',reason=str(error))
        update_run(directory,state,branches=copy.deepcopy(branches))
    if errors:
        raise ProductionError('Independent branches retained: '+'; '.join(name+': '+str(error) for name,error in errors.items()))
    for name,path in results.items():
        if read(path).get('script_version_id')!=current['script_version_id']:
            raise ProductionError(name+' belongs to a different script version; editing blocked.')
    return results


def run_pipeline(topic=None, research_file=None, demo=False, demo_case="success", output_root=None, stop_after=None) -> Path:
    """Запускает реализованные этапы, сохраняя результаты и явные причины остановки."""
    generate = None
    if demo:
        from demo_data import DemoResponses, demo_research
        print("ТЕСТОВЫЙ РЕЖИМ: без сети и API. Настоящая проверка фактов не выполняется.")
        responses = DemoResponses(demo_case)
        generate = responses.generate
    if research_file:
        research = load_research(Path(research_file), demo=demo)
    elif demo:
        research = demo_research(topic, demo_case)
    else:
        print("Исследование темы через OpenAI и веб-поиск…")
        research = run_with_progress('Исследование',research_topic,topic,output_root=output_root) if output_root else run_with_progress('Исследование',research_topic,topic)
    validate_research(research, demo=demo)
    mode = "demo" if demo else "real"
    research = {**research, "mode": mode}
    directory = Path(research["run_directory"]) if not research_file and research.get("run_directory") else save_research(research, output_root or Path(__file__).parent / "outputs")
    print(f"Папка запуска: {directory}")
    if research_file:
        atomic_write_json(directory / "research_origin.json", {
            "source_path": str(Path(research_file).resolve()), "created_at_utc": research.get("created_at_utc"),
            "source_run_directory": research.get("run_directory"),
        })
    state = {"mode": mode, "topic": research["topic"], "status": "running", "stage": "research",
             "resumed_from": str(Path(research_file).resolve()) if research_file else None}
    update_run(directory, state)
    try:
        if research.get("schema_version") == 4:
            if research["execution"]["state"] != "completed":
                raise ResearchError("Сбой выполнения исследования: " + str(research["execution"].get("error")))
            if research["enough"] is not True:
                raise ResearchError("Не хватает: " + "; ".join(research["missing_questions"]))
            if stop_after == "research":
                update_run(directory, state, status="research_completed", enough=True)
                print("Хватает. Исследование сохранено; сценарий и следующие этапы не запускались.")
                return directory
            script_research = research
        else:
            script_research = research
        if research.get("status") == "insufficient_data":
            raise ResearchError("Исследователь вернул insufficient_data. " + "; ".join(research["blocking_issues"]))
        if research.get("status") == "needs_manual_review":
            raise ResearchError("Исследование требует ручной проверки: " + "; ".join(research["manual_review_reasons"]))
        if stop_after == "research":
            update_run(directory, state, status="research_completed", stage="research")
            print("Исследование сохранено. Сценарий и последующие API не запускались.")
            return directory
        if not demo and research.get("schema_version") not in {3, 4}:
            raise ResearchError("Старое исследование не содержит проверенных снимков и цитат. Выполните новое исследование --stop-after research.")
        for warning in script_research.get("warnings", []):
            print(f"Предупреждение исследования: {warning}")
        script, report = run_with_progress('Сценарий и проверка',write_and_check,script_research, directory, state, generate, demo)
        save_artifact(directory, "scenes", scenes_from_script(script, demo=demo), mode)
        if stop_after == "script":
            update_run(directory, state, status="script_completed", stage="verification")
            print("Проверенный сценарий с visual сохранён. Озвучка и материалы не запускались.")
            return directory
        branch_results=None
        if not demo and stop_after != 'voiceover':
            branch_results=run_independent_branches(directory,state)
        else:
            update_run(directory,state,stage='voiceover')
            voiceover_from_file(directory/'script.json',demo=demo)
        # Озвучка сама проверяет текст до duration feedback и использует общий бюджет сценариста.
        from script_workflow import approved_script
        save_artifact(directory, "scenes", scenes_from_script(approved_script(directory, demo), demo), mode)
        if stop_after == "voiceover":
            update_run(directory, state, status="voiceover_completed", stage="voiceover")
            print("Полная озвучка и привязки сохранены. Картинки и монтаж не запускались.")
            return directory
        if demo:
            update_run(directory, state, status="demo_completed", stage="voiceover")
            print("ТЕСТ: материалы и монтаж не запускались; настоящего ролика нет.")
            return directory
        update_run(directory, state, stage="visuals")
        visual_path = branch_results["visuals"]
        visual_status = json.loads(visual_path.read_text(encoding="utf-8"))["status"]
        update_run(directory, state, stage="editing", visual_status=visual_status)
        edit_path = run_editing(directory)
        edit_status = json.loads(edit_path.read_text(encoding="utf-8"))["status"]
        update_run(directory, state, stage="finished", status=edit_status)
    except (ResearchError, VoiceoverError, VisualError, ProductionError, MediaError) as error:
        update_run(directory, state, status="stopped", reason=str(error))
        stopped = type(error)(f"{error} Результаты: {directory}")
        stopped.directory = directory
        raise stopped from None
    except OSError:
        # Если диск не позволяет записать даже журнал, основной обработчик сообщит об ошибке.
        update_run(directory, state, status="error", reason="Ошибка сохранения промежуточных файлов.")
        raise
    print(f"Вход подбора материалов сохранён: {directory / 'scenes.json'}")
    print("ТЕСТОВЫЙ проход завершён; настоящей проверки не было." if demo else "Проверка моделью по текстам источников завершена.")
    print("Результаты монтажа и автоматической проверки сохранены в render_manifest.json и quality_report.json.")
    return directory


def resume_script_run(directory: Path, demo=False) -> Path:
    """Продолжает только сценариста в той же папке, сохраняя общий бюджет."""
    research = load_research(directory / "research.json", demo=demo)
    state_path = directory / "run.json"
    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {"mode": "demo" if demo else "real"}
        if not isinstance(state, dict) or state.get("mode") != ("demo" if demo else "real"):
            raise ValueError
    except (OSError, ValueError):
        raise ResearchError("Не удалось прочитать run.json нужного режима. Исходный запуск не изменён.") from None
    generate = None
    if demo:
        from demo_data import DemoResponses
        generate = DemoResponses().generate
    try:
        script, _ = write_and_check(research, directory, state, generate=generate, demo=demo)
        save_artifact(directory, "scenes", scenes_from_script(script, demo), state["mode"])
        update_run(directory, state, stage="verification", status="script_completed")
        return directory
    except ResearchError as error:
        update_run(directory, state, status="stopped", reason=str(error))
        raise
