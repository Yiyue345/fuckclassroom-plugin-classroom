from __future__ import annotations

from typing import Any

from fuckclassroom.core.plugins import ServiceContainer


def build_course_settings_context(
    services: ServiceContainer,
    _request: Any,
) -> dict[str, object]:
    semester_sync = services.get("semester_sync")
    terms = []
    notices = []
    try:
        snapshot = semester_sync.sync()
        terms = list(snapshot.terms)
        if snapshot.error:
            notices.append(
                {
                    "kind": "error",
                    "icon": "circle-alert",
                    "message": f"学期同步失败，已使用缓存：{snapshot.error}",
                }
            )
    except Exception as exc:  # noqa: BLE001 - settings UI should degrade to an empty list.
        notices.append(
            {
                "kind": "error",
                "icon": "circle-alert",
                "message": str(exc),
            }
        )
    return {
        "terms": terms,
        "notices": tuple(notices),
    }


__all__ = ["build_course_settings_context"]
