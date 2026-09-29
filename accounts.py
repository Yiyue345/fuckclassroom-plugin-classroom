from __future__ import annotations

from typing import Any

from fuckclassroom.core.plugins import ServiceContainer


def build_account_context(
    services: ServiceContainer,
    _request: Any,
) -> dict[str, object]:
    auth_service = services.get("auth_service")
    return {
        "session": auth_service.get_session_status(),
        "login_run": auth_service.get_login_run_status(),
    }


__all__ = ["build_account_context"]
