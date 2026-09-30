from __future__ import annotations

import base64
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fuckclassroom.auth import LoginSessionService
from fuckclassroom.classroom import (
    ClassroomClientError,
    Course,
    CourseDetail,
    ExportedFile,
    Lesson,
    LessonExportTarget,
    LessonResource,
    LiveLesson,
    LiveLessonInfo,
    LivePptSlide,
    SemesterSyncService,
    Term,
)
from fuckclassroom.classroom.downloads import DownloadLibrary
from fuckclassroom.core.plugins import PluginContext
from fuckclassroom.plugins.process_runtime import ProcessPluginError, ProcessPluginHost
from fuckclassroom.plugins.rpc import PLUGIN_RPC_API_VERSION


class ClassroomClientProcessProxy:
    """Main-process facade for ClassroomClient running in an isolated worker."""

    def __init__(self, host: ProcessPluginHost) -> None:
        self.host = host

    def _call(self, method: str, params: dict[str, object] | None = None, *, timeout: float = 180.0):
        try:
            return self.host.call_sync(method, params or {}, timeout=timeout)
        except ProcessPluginError as exc:
            raise ClassroomClientError(str(exc)) from exc

    def verify_session(self) -> None:
        self._call("classroom.verify_session", {})

    def list_terms(self) -> list[Term]:
        return [_term(item) for item in _list(self._call("classroom.list_terms"))]

    def refresh_terms(self) -> list[Term]:
        return [_term(item) for item in _list(self._call("classroom.refresh_terms"))]

    def list_courses(
        self,
        page: int = 1,
        page_size: int = 50,
        term_id: str | None = None,
        term: Term | None = None,
        *,
        force_refresh: bool = False,
    ) -> list[Course]:
        payload = self._call(
            "classroom.list_courses",
            {
                "page": page,
                "page_size": page_size,
                "term_id": term_id,
                "term": asdict(term) if term is not None else None,
                "force_refresh": bool(force_refresh),
            },
        )
        return [_course(item) for item in _list(payload)]

    def get_course_detail(self, course_id: str) -> CourseDetail:
        return _course_detail(self._dict(self._call("classroom.course.get", {"course_id": course_id})))

    def refresh_course_detail(self, course_id: str) -> CourseDetail:
        return _course_detail(
            self._dict(self._call("classroom.course.refresh", {"course_id": course_id}))
        )

    def list_current_live_lessons(self, courses: list[Course]) -> list[LiveLesson]:
        payload = self._call(
            "classroom.live.list",
            {"courses": [asdict(item) for item in courses]},
        )
        return [LiveLesson(**item) for item in _list(payload)]

    def get_live_lesson_info(self, course_id: str, lesson_id: str) -> LiveLessonInfo:
        payload = self._dict(
            self._call(
                "classroom.live.info",
                {"course_id": course_id, "lesson_id": lesson_id},
            )
        )
        return LiveLessonInfo(**payload)

    def list_live_ppt_slides(
        self,
        course_id: str,
        lesson_id: str,
        *,
        after_id: int = 0,
    ) -> list[LivePptSlide]:
        payload = self._call(
            "classroom.live.ppt.list",
            {"course_id": course_id, "lesson_id": lesson_id, "after_id": after_id},
        )
        return [LivePptSlide(**item) for item in _list(payload)]

    def get_live_playlist_url(self, course_id: str, lesson_id: str, quality: str) -> str:
        return str(
            self._call(
                "classroom.live.playlist",
                {"course_id": course_id, "lesson_id": lesson_id, "quality": quality},
            )
            or ""
        )

    def download_live_media(
        self,
        url: str,
        *,
        max_bytes: int = 32 * 1024 * 1024,
    ) -> tuple[bytes, str]:
        payload = self._dict(
            self._call(
                "classroom.live.media",
                {"url": url, "max_bytes": max_bytes},
                timeout=300,
            )
        )
        return base64.b64decode(str(payload.get("content_b64") or "")), str(
            payload.get("content_type") or "application/octet-stream"
        )

    def download_live_ppt_image(self, url: str) -> tuple[bytes, str]:
        payload = self._dict(
            self._call("classroom.live.ppt.image", {"url": url}, timeout=180)
        )
        return base64.b64decode(str(payload.get("content_b64") or "")), str(
            payload.get("content_type") or "application/octet-stream"
        )

    def get_qr_device_id(self) -> str:
        return str(self._call("classroom.qr.device_id") or "")

    def answer_qr_rollcall(
        self,
        rollcall_id: str,
        data: str,
        device_id: str,
    ) -> dict[str, Any]:
        return self._dict(
            self._call(
                "classroom.qr.answer",
                {
                    "rollcall_id": rollcall_id,
                    "data": data,
                    "device_id": device_id,
                },
                timeout=180,
            )
        )

    def get_lesson_export_target(
        self,
        course_id: str,
        lesson_id: str,
        export_kind: str,
    ) -> LessonExportTarget:
        payload = self._dict(
            self._call(
                "classroom.export.target",
                {
                    "course_id": course_id,
                    "lesson_id": lesson_id,
                    "export_kind": export_kind,
                },
            )
        )
        payload["path"] = Path(str(payload["path"]))
        return LessonExportTarget(**payload)

    def download_lesson_export(
        self,
        course_id: str,
        lesson_id: str,
        export_kind: str,
    ) -> ExportedFile:
        payload = self._dict(
            self._call(
                "classroom.export.download",
                {
                    "course_id": course_id,
                    "lesson_id": lesson_id,
                    "export_kind": export_kind,
                },
                timeout=10 * 60,
            )
        )
        saved_path = Path(str(payload["saved_path"]))
        return ExportedFile(
            filename=str(payload["filename"]),
            content=saved_path.read_bytes(),
            content_type=str(payload["content_type"]),
            saved_path=saved_path,
            from_cache=bool(payload.get("from_cache")),
        )

    @staticmethod
    def _dict(value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ClassroomClientError("课程 Worker 返回格式错误")
        return value


def _list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ClassroomClientError("课程 Worker 返回格式错误")
    return [item for item in value if isinstance(item, dict)]


def _term(row: dict[str, Any]) -> Term:
    return Term(**row)


def _course(row: dict[str, Any]) -> Course:
    return Course(**row)


def _resource(row: dict[str, Any]) -> LessonResource:
    return LessonResource(**row)


def _lesson(row: dict[str, Any]) -> Lesson:
    payload = dict(row)
    resources = payload.get("resources")
    payload["resources"] = [
        _resource(item) for item in resources if isinstance(item, dict)
    ] if isinstance(resources, list) else []
    return Lesson(**payload)


def _course_detail(row: dict[str, Any]) -> CourseDetail:
    course = row.get("course")
    lessons = row.get("lessons")
    if not isinstance(course, dict) or not isinstance(lessons, list):
        raise ClassroomClientError("课程 Worker 返回的课程详情格式错误")
    return CourseDetail(
        course=_course(course),
        lessons=[_lesson(item) for item in lessons if isinstance(item, dict)],
    )


def setup_services(context: PluginContext) -> None:
    config = context.config
    services = context.services
    auth_service = LoginSessionService(
        config,
        credential_store=services.get("credential_store"),
        verification_broker=services.get("verification_broker"),
        login_lock=services.get("login_lock"),
    )
    host = ProcessPluginHost(
        plugin_id="classroom-network",
        root=Path(__file__).resolve().parent,
        entry="worker.py",
        data_dir=Path(config.data_dir),
        rpc_registry=services.get("plugin_rpc"),
        rpc_api_version=PLUGIN_RPC_API_VERSION,
        rpc_permissions=("internal.classroom.session.ensure",),
    )
    classroom_client = ClassroomClientProcessProxy(host)
    semester_sync = SemesterSyncService(
        classroom_client,
        config.cache_dir / "classroom" / "semesters.json",
    )

    services.add("classroom_process_host", host)
    services.add("classroom_client", classroom_client)
    services.add("semester_sync", semester_sync)
    services.add("auth_service", auth_service)
    services.add("download_library", DownloadLibrary(config.downloads_dir))


async def startup(context: PluginContext) -> None:
    await context.services.get("classroom_process_host").start()


async def shutdown(context: PluginContext) -> None:
    await context.services.get("classroom_process_host").stop()


__all__ = [
    "ClassroomClientProcessProxy",
    "setup_services",
    "shutdown",
    "startup",
]
