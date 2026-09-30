from __future__ import annotations

import asyncio

from fuckclassroom.core.plugins import PluginContext


async def startup(context: PluginContext) -> None:
    services = context.services
    semester_sync = services.get("semester_sync")
    auth_service = services.get("auth_service")

    async def sync_semesters() -> None:
        try:
            snapshot = await asyncio.to_thread(semester_sync.sync, force=True)
        except Exception as exc:  # noqa: BLE001 - startup work must not stop the app.
            context.logger.warning("启动时学期同步失败：%s", exc)
            return
        if snapshot.error:
            context.logger.warning("启动时学期同步失败，已使用缓存：%s", snapshot.error)
        else:
            context.logger.info("启动时已同步 %s 个学期", len(snapshot.terms))

    async def verify_session() -> None:
        try:
            result = await asyncio.to_thread(
                auth_service.verify_saved_session,
                auto_relogin=True,
            )
        except Exception as exc:  # noqa: BLE001
            context.logger.warning("启动时课程录播会话检测失败：%s", exc)
            return
        context.logger.info("启动时课程录播会话状态：%s", result.message)

    context.create_task(sync_semesters())
    context.create_task(verify_session())


__all__ = ["startup"]
