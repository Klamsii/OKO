"""Describe what was checked without treating the rule catalog as a model.

The jury verdict is intentionally compact. Private UI metadata gives it a scope;
it does not infer a trained model's coverage from its executable command.
"""
from __future__ import annotations


def describe_analysis(detectors, *, command_configured=False, provenance="configuration"):
    names = list(dict.fromkeys(detectors or ()))
    if names == ["datathon"]:
        return {
            "mode": "datathon", "label": "OKO · Qwen3-VL · анализ кассовых операций",
            "method": "self_hosted_qwen3_vl", "detectors": names,
            "checked_rule_ids": [], "rule_coverage_known": False, "full_audit_available": False,
            "requires_llm": True, "provenance": provenance,
            "verdict_labels": {"violation": "Обнаружены признаки возможных нарушений",
                               "no_violation": "Нарушение не обнаружено"},
            "limitations": [
                "Оригинальное видео анализирует собственный сервер команды с Qwen3-VL. Коммерческие облачные AI API не используются.",
                "Проверяются видимые действия с деньгами, хранение денег и уход сотрудника; полнота распознавания не гарантирована.",
                "Возможные нарушения требуют просмотра человеком. Неопределённость, влияющая на вердикт, сохраняется в отчёте.",
                "Проверка отдельных кадров не подтверждает отсутствие событий между ними. Неизвестные границы эпизода не заменяются точными таймкодами.",
                "Ссылки на нормы передаются из модели; непроверенная применимость нормы отмечается отдельно. Лица не идентифицируются.",
            ],
        }
    camera = "obstruction" in names
    external = "team" in names
    known = camera and not external and set(names) == {"obstruction"}
    completed = provenance == "completed_run"
    if known:
        mode, label, method = "camera_visibility", "Проверка видимости камеры", "local_cv_heuristic"
        labels = {"violation": "Есть признаки перекрытия камеры",
                  "no_violation": "Признаков перекрытия камеры не найдено"}
        limitations = [
            "Проверяется только V13: признаки перекрытия или сильного затемнения камеры.",
            "Алгоритм использует яркость, контраст и детали кадра; LLM для этой проверки не нужна.",
            "Кассовые операции и остальные типы каталога автоматически не проверяются.",
            "Отсутствие эпизодов V13 не подтверждает отсутствие других нарушений.",
        ]
    else:
        mode = "external_model" if external else "unknown"
        label = "Проверка с внешней моделью" if external else "Область проверки не указана"
        method = "external_model_and_local_cv" if external and camera else "external_model" if external else "unknown"
        labels = {"violation": "Есть эпизоды для проверки",
                  "no_violation": "Подключённые детекторы не вернули эпизодов" if external else "В отчёте нет найденных эпизодов"}
        limitations = [
            "Наличие типа в каталоге не означает, что он автоматически проверен.",
            "Отсутствие найденных эпизодов не подтверждает отсутствие всех нарушений.",
        ]
        if external:
            limitations.append("Область распознавания внешней модели не заявлена; команда запуска не подтверждает её качество или покрытие.")
        else:
            limitations.append("В сохранённом отчёте нет достоверных сведений о работавших детекторах.")
    return {
        "mode": mode, "label": label, "method": method,
        "detectors": names, "checked_rule_ids": ["V13"] if camera else [],
        "rule_coverage_known": known, "full_audit_available": False,
        "requires_llm": False if known else None,
        "team_model": {"enabled": external, "command_configured": bool(command_configured),
                       "available": True if external and completed else None if external else False,
                       "status": "completed" if external and completed else "configured_unverified" if external and command_configured else "command_missing" if external else "disabled"},
        "provenance": provenance, "limitations": limitations, "verdict_labels": labels,
    }


def scope_for_run(run):
    """Use persisted execution metadata before current server configuration."""
    result = run.get("result") or {}
    stored = result.get("analysis_scope")
    if isinstance(stored, dict):
        return stored
    if isinstance(result.get("detectors"), list):
        return describe_analysis(result["detectors"], command_configured="team" in result["detectors"],
                                 provenance="completed_run")
    snapshot = run.get("config_snapshot") or {}
    stored = snapshot.get("analysis_scope")
    if isinstance(stored, dict):
        return stored
    detectors = snapshot.get("detectors")
    return describe_analysis(detectors if isinstance(detectors, list) else [],
                             provenance="saved_configuration" if detectors else "unknown_legacy_report")
