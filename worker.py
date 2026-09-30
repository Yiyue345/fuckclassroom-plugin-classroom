from __future__ import annotations

import base64
from dataclasses import asdict

from .client import ClassroomClient, Course, Term
from fuckclassroom.core.config import AppConfig


_client: ClassroomClient | None = None


def _get_client(context) -> ClassroomClient:
    global _client
    if _client is None:
        _client = ClassroomClient(AppConfig(data_dir=context.data_dir))

        def recover_session():
            result = context.rpc.call("internal.classroom.session.ensure")
            return bool(isinstance(result, dict) and result.get("is_logged_in"))

        _client.set_session_recoverer(recover_session)
    return _client


def handle_call(method, params, context, progress):
    client = _get_client(context)

    if method == "classroom.verify_session":
        client.verify_session()
        return {"ok": True}
    if method == "classroom.list_terms":
        return [asdict(item) for item in client.list_terms()]
    if method == "classroom.refresh_terms":
        return [asdict(item) for item in client.refresh_terms()]
    if method == "classroom.list_courses":
        raw_term = params.get("term")
        term = Term(**raw_term) if isinstance(raw_term, dict) else None
        return [
            asdict(item)
            for item in client.list_courses(
                page=int(params.get("page") or 1),
                page_size=int(params.get("page_size") or 50),
                term_id=str(params.get("term_id") or "") or None,
                term=term,
                force_refresh=bool(params.get("force_refresh", False)),
            )
        ]
    if method == "classroom.course.get":
        return asdict(client.get_course_detail(str(params.get("course_id") or "")))
    if method == "classroom.course.refresh":
        return asdict(client.refresh_course_detail(str(params.get("course_id") or "")))
    if method == "classroom.live.list":
        rows = params.get("courses")
        courses = [Course(**item) for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []
        return [asdict(item) for item in client.list_current_live_lessons(courses)]
    if method == "classroom.live.info":
        return asdict(
            client.get_live_lesson_info(
                str(params.get("course_id") or ""),
                str(params.get("lesson_id") or ""),
            )
        )
    if method == "classroom.live.ppt.list":
        return [
            asdict(item)
            for item in client.list_live_ppt_slides(
                str(params.get("course_id") or ""),
                str(params.get("lesson_id") or ""),
                after_id=int(params.get("after_id") or 0),
            )
        ]
    if method == "classroom.live.playlist":
        return client.get_live_playlist_url(
            str(params.get("course_id") or ""),
            str(params.get("lesson_id") or ""),
            str(params.get("quality") or "original"),
        )
    if method == "classroom.live.media":
        content, content_type = client.download_live_media(
            str(params.get("url") or ""),
            max_bytes=int(params.get("max_bytes") or 32 * 1024 * 1024),
        )
        return {
            "content_b64": base64.b64encode(content).decode("ascii"),
            "content_type": content_type,
        }
    if method == "classroom.live.ppt.image":
        content, content_type = client.download_live_ppt_image(str(params.get("url") or ""))
        return {
            "content_b64": base64.b64encode(content).decode("ascii"),
            "content_type": content_type,
        }
    if method == "classroom.qr.device_id":
        return client.get_qr_device_id()
    if method == "classroom.qr.answer":
        return client.answer_qr_rollcall(
            str(params.get("rollcall_id") or ""),
            str(params.get("data") or ""),
            str(params.get("device_id") or ""),
        )
    if method == "classroom.export.target":
        result = asdict(
            client.get_lesson_export_target(
                str(params.get("course_id") or ""),
                str(params.get("lesson_id") or ""),
                str(params.get("export_kind") or ""),
            )
        )
        result["path"] = str(result["path"])
        return result
    if method == "classroom.export.download":
        result = client.download_lesson_export(
            str(params.get("course_id") or ""),
            str(params.get("lesson_id") or ""),
            str(params.get("export_kind") or ""),
        )
        return {
            "filename": result.filename,
            "content_type": result.content_type,
            "saved_path": str(result.saved_path),
            "from_cache": result.from_cache,
        }

    raise ValueError(f"未知 Classroom Worker 方法：{method}")
