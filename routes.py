from __future__ import annotations

from dataclasses import replace
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from fuckclassroom.classroom import ClassroomClientError, CourseDetail, rewrite_live_playlist


def _run_for_result(operation, result_url: str) -> str:
    operation()
    return result_url


def _reconcile_live_status(detail: CourseDetail, live_ids: set[str]) -> CourseDetail:
    lessons = []
    for lesson in detail.lessons:
        if lesson.id in live_ids:
            lessons.append(replace(lesson, is_live=True, status_label="直播中"))
            continue
        if not lesson.is_live:
            lessons.append(lesson)
            continue
        has_recording = any(
            resource.kind == "video" and resource.is_downloadable
            for resource in lesson.resources
        )
        status_label = lesson.status_label
        if status_label == "直播中":
            status_label = "录播已生成" if has_recording else ""
        lessons.append(replace(lesson, is_live=False, status_label=status_label))
    return CourseDetail(course=detail.course, lessons=lessons)


def register_routes(
    router: APIRouter,
    *,
    config,
    templates,
    classroom_client,
    semester_sync,
    auth_service,
    download_library,
    task_manager,
    task_started_response,
) -> None:
    app_config = config

    def enrich_live_status(detail: CourseDetail) -> CourseDetail:
        try:
            live_ids = {
                item.lesson_id
                for item in classroom_client.list_current_live_lessons([detail.course])
            }
        except ClassroomClientError:
            return detail
        return _reconcile_live_status(detail, live_ids)

    @router.get("/classroom", response_class=HTMLResponse)
    def classroom_home(request: Request) -> HTMLResponse:
        session = auth_service.get_session_status()
        courses = []
        course_error = None
        live_lessons = []
        live_error = None
        if session.is_logged_in:
            try:
                courses = classroom_client.list_courses(
                    page_size=100,
                    term_id=app_config.default_term_id or None,
                )
            except ClassroomClientError as exc:
                course_error = str(exc)
            if courses:
                try:
                    live_lessons = classroom_client.list_current_live_lessons(courses)
                except ClassroomClientError as exc:
                    live_error = str(exc)
        files = download_library.list_files()
        recent_tasks = task_manager.list_recent(4)
        return templates.TemplateResponse(
            request,
            "classroom/login.html",
            {
                "session": session,
                "courses": courses,
                "course_error": course_error,
                "live_lessons": live_lessons,
                "live_error": live_error,
                "files": files,
                "recent_tasks": recent_tasks,
                "running_task_count": task_manager.count_by_status("running"),
                "completed_task_count": task_manager.count_by_status("succeeded"),
            },
        )

    @router.post("/auth/start")
    def start_login(request: Request) -> Response:
        task = task_manager.start(
            "课程录播登录",
            lambda progress: _run_for_result(
                lambda: auth_service.login_for_web(progress),
                "/accounts#classroom-session",
            ),
        )
        return task_started_response(request, task, "/accounts#classroom-session")

    @router.post("/auth/verify")
    def verify_login(request: Request) -> Response:
        task = task_manager.start(
            "验证课程录播会话",
            lambda progress: _run_for_result(
                lambda: auth_service.verify_for_web(progress),
                "/accounts#classroom-session",
            ),
        )
        return task_started_response(request, task, "/accounts#classroom-session")

    @router.post("/auth/logout")
    def logout() -> RedirectResponse:
        auth_service.clear_session()
        return RedirectResponse("/accounts#classroom-session", status_code=303)

    @router.get("/courses", response_class=HTMLResponse)
    def courses(request: Request, term_id: str | None = None) -> HTMLResponse:
        error = None
        term_error = None
        courses = []
        terms = []
        selected_term_id = term_id if term_id is not None else ""
        selected_term = None
        try:
            snapshot = semester_sync.sync(force=True)
            terms = list(snapshot.terms)
            if term_id is None:
                selected_term_id = snapshot.current_term_id or app_config.default_term_id
            selected_term = next((item for item in terms if item.id == selected_term_id), None)
            if snapshot.error:
                term_error = f"学期同步失败，已使用缓存：{snapshot.error}"
        except ClassroomClientError as exc:
            term_error = f"学期列表加载失败：{exc}"

        try:
            courses = classroom_client.list_courses(term=selected_term, force_refresh=True)
        except ClassroomClientError as exc:
            error = str(exc)

        return templates.TemplateResponse(
            request,
            "classroom/courses.html",
            {
                "session": auth_service.get_session_status(),
                "courses": courses,
                "terms": terms,
                "selected_term_id": selected_term_id,
                "term_error": term_error,
                "error": error,
            },
        )

    @router.get("/courses/{course_id}", response_class=HTMLResponse)
    def course_detail(request: Request, course_id: str) -> HTMLResponse:
        error = None
        detail = None
        try:
            detail = enrich_live_status(classroom_client.refresh_course_detail(course_id))
        except ClassroomClientError as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request,
            "classroom/course_detail.html",
            {
                "session": auth_service.get_session_status(),
                "detail": detail,
                "course_id": course_id,
                "refreshed": request.query_params.get("refreshed") == "1",
                "refresh_error": request.query_params.get("refresh_error"),
                "error": error,
            },
        )

    @router.post("/courses/{course_id}/refresh")
    def refresh_course_detail(course_id: str) -> RedirectResponse:
        return RedirectResponse(f"/courses/{course_id}?refreshed=1", status_code=303)

    @router.get("/courses/{course_id}/lessons/{lesson_id}/download/{export_kind}")
    def download_lesson_export(course_id: str, lesson_id: str, export_kind: str) -> Response:
        try:
            exported = classroom_client.download_lesson_export(course_id, lesson_id, export_kind)
        except ClassroomClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        quoted_name = quote(exported.filename)
        return Response(
            content=exported.content,
            media_type=exported.content_type,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quoted_name}",
                "Cache-Control": "no-store",
            },
        )

    @router.get("/downloads", response_class=HTMLResponse)
    def downloads(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "classroom/downloads.html",
            {
                "session": auth_service.get_session_status(),
                "files": download_library.list_files(),
                "deleted": request.query_params.get("deleted") == "1",
                "delete_error": request.query_params.get("delete_error"),
            },
        )

    @router.get("/downloads/file/{relative_path:path}")
    def download_local_file(relative_path: str) -> FileResponse:
        try:
            path = download_library.resolve(relative_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="文件不存在") from exc
        return FileResponse(path, filename=path.name)

    @router.post("/downloads/local/{action}")
    async def local_download_action(action: str, request: Request) -> JSONResponse:
        if action not in {"open", "reveal"}:
            return JSONResponse({"detail": "不支持的文件操作"}, status_code=400)
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        relative_path = (parsed.get("relative_path") or [""])[-1]
        action_label = "打开文件" if action == "open" else "在文件夹中显示"
        try:
            path = (
                download_library.open_file(relative_path)
                if action == "open"
                else download_library.reveal_file(relative_path)
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except FileNotFoundError:
            return JSONResponse({"detail": "文件不存在"}, status_code=404)
        except OSError as exc:
            return JSONResponse({"detail": f"{action_label}失败：{exc}"}, status_code=409)
        message = (
            f"已使用系统默认应用打开 {path.name}"
            if action == "open"
            else f"已在文件夹中定位 {path.name}"
        )
        return JSONResponse({"action": action, "message": message})

    @router.post("/downloads/delete")
    async def delete_download(request: Request) -> RedirectResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        relative_path = (parsed.get("relative_path") or [""])[-1]
        try:
            download_library.delete(relative_path)
        except (FileNotFoundError, ValueError, OSError) as exc:
            return RedirectResponse(f"/downloads?delete_error={quote(str(exc))}", status_code=303)
        return RedirectResponse("/downloads?deleted=1", status_code=303)

    @router.post("/downloads/delete-many")
    async def delete_downloads(request: Request) -> JSONResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        relative_paths = [value for value in parsed.get("relative_path", []) if value]
        if not relative_paths:
            return JSONResponse({"detail": "请先选择要删除的文件"}, status_code=400)
        if len(relative_paths) > 500:
            return JSONResponse({"detail": "单次最多删除 500 个文件"}, status_code=400)
        try:
            deleted_count = download_library.delete_many(relative_paths)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except FileNotFoundError:
            return JSONResponse({"detail": "部分文件已不存在，请刷新后重试"}, status_code=404)
        except OSError as exc:
            return JSONResponse({"detail": f"批量删除失败：{exc}"}, status_code=409)
        return JSONResponse(
            {
                "deleted": deleted_count,
                "message": f"已删除 {deleted_count} 个文件",
            }
        )

    @router.get("/api/courses")
    def api_courses(term_id: str | None = None) -> dict[str, object]:
        courses = classroom_client.list_courses(term_id=term_id or None)
        return {"courses": [course.__dict__ for course in courses]}

    @router.get("/api/courses/{course_id}/lessons/{lesson_id}/live")
    def api_live_lesson(course_id: str, lesson_id: str) -> dict[str, object]:
        try:
            info = classroom_client.get_live_lesson_info(course_id, lesson_id)
        except ClassroomClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        qualities = [
            {
                "id": quality,
                "label": label,
                "playlist_url": (
                    f"/api/courses/{quote(course_id, safe='')}/lessons/"
                    f"{quote(lesson_id, safe='')}/live/playlist?quality={quality}"
                ),
            }
            for quality, label in (
                ("original", "原画"),
                ("high", "高清"),
                ("standard", "流畅"),
            )
            if quality in info.playlist_urls
        ]
        return {
            "course_id": info.course_id,
            "lesson_id": info.lesson_id,
            "course_title": info.course_title,
            "lesson_title": info.lesson_title,
            "lecturer": info.lecturer,
            "room_name": info.room_name,
            "start_at": info.start_at,
            "end_at": info.end_at,
            "is_live": info.is_live,
            "ppt_running": info.ppt_running,
            "official_url": info.official_url,
            "qualities": qualities,
            "playlist_url": qualities[0]["playlist_url"] if qualities else "",
        }

    @router.get("/api/courses/{course_id}/lessons/{lesson_id}/live/playlist")
    def api_live_playlist(course_id: str, lesson_id: str, quality: str = "original") -> Response:
        try:
            source_url = classroom_client.get_live_playlist_url(course_id, lesson_id, quality)
            content, _ = classroom_client.download_live_media(source_url, max_bytes=2 * 1024 * 1024)
            playlist = rewrite_live_playlist(content.decode("utf-8", errors="replace"), source_url)
        except ClassroomClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            content=playlist,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/api/courses/{course_id}/lessons/{lesson_id}/ppt/slides")
    @router.get("/api/courses/{course_id}/lessons/{lesson_id}/live/ppt")
    def api_live_ppt(course_id: str, lesson_id: str, after_id: int = 0) -> dict[str, object]:
        try:
            slides = classroom_client.list_live_ppt_slides(
                course_id,
                lesson_id,
                after_id=max(0, after_id),
            )
        except ClassroomClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "slides": [
                {
                    "id": slide.id,
                    "image_url": f"/api/live/ppt-image?url={quote(slide.image_url, safe='')}",
                    "thumbnail_url": (
                        f"/api/live/ppt-image?url={quote(slide.thumbnail_url, safe='')}"
                        if slide.thumbnail_url
                        else ""
                    ),
                    "created_at": slide.created_at,
                    "offset_seconds": slide.offset_seconds,
                }
                for slide in slides
            ],
            "total": len(slides),
            "last_id": max((slide.id for slide in slides), default=max(0, after_id)),
        }

    @router.get("/api/live/media")
    def api_live_media(url: str) -> Response:
        try:
            content, content_type = classroom_client.download_live_media(url)
            if "mpegurl" in content_type.lower() or url.split("?", 1)[0].lower().endswith(".m3u8"):
                content = rewrite_live_playlist(
                    content.decode("utf-8", errors="replace"),
                    url,
                ).encode("utf-8")
                content_type = "application/vnd.apple.mpegurl"
        except ClassroomClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            content=content,
            media_type=content_type.split(";", 1)[0],
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/api/live/ppt-image")
    def api_live_ppt_image(url: str) -> Response:
        try:
            content, content_type = classroom_client.download_live_ppt_image(url)
        except ClassroomClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            content=content,
            media_type=content_type.split(";", 1)[0],
            headers={"Cache-Control": "private, max-age=10"},
        )

    @router.get("/api/courses/{course_id}")
    def api_course_detail(course_id: str) -> dict[str, object]:
        detail = enrich_live_status(classroom_client.get_course_detail(course_id))
        return {
            "course": detail.course.__dict__,
            "lessons": [
                {
                    **lesson.__dict__,
                    "resources": [resource.__dict__ for resource in lesson.resources],
                }
                for lesson in detail.lessons
            ],
        }


def build_router(context) -> APIRouter:
    from fuckclassroom.web.responses import task_started_response

    services = context.services
    router = APIRouter()
    register_routes(
        router,
        config=context.config,
        templates=services.get("templates"),
        classroom_client=services.get("classroom_client"),
        semester_sync=services.get("semester_sync"),
        auth_service=services.get("auth_service"),
        download_library=services.get("download_library"),
        task_manager=services.get("task_manager"),
        task_started_response=task_started_response,
    )
    return router


__all__ = ["build_router", "register_routes"]
