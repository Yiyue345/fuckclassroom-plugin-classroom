from __future__ import annotations

import json
import re
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from fuckclassroom.core.atomic import atomic_write_text
from fuckclassroom.core.cache import JsonCache
from fuckclassroom.core.config import AppConfig
from fuckclassroom.core.plugins import PluginServiceError


BASE_URL = "https://classroom.guet.edu.cn"
CHANGKE_BASE_URL = "https://courses.guet.edu.cn"
COURSE_LIST_PATH = "/personal/courseapi/vlabpassportapi/v1/account-profile/course"
COURSE_DETAIL_PATH = "/personal/courseapi/vlabpassportapi/v1/projects/{course_id}"
TERM_LIST_PATH = "/personal/courseapi/vlabpassportapi/v1/course/term-list"
PPT_EXPORT_PATH = "/personal/courseapi/vlabpassportapi/v1/account-profile/rcourse/export/download-sub-ppt"
TRANS_EXPORT_PATH = "/personal/courseapi/vlabpassportapi/v1/account-profile/rcourse/export/download-sub-trans"
LIVE_LIST_PATH = "/courseapi/v2/course-live/search-live-course-list"
LIVE_INFO_PATH = "/courseapi/v3/portal-home-setting/get-sub-info"
LIVE_PPT_PATH = "/pptnote/v1/schedule/search-ppt"
_PPT_CACHE_META_SCHEMA = 3
_PPT_PROVISIONAL_STATUSES = {"running", "pending", "processing", "queued", "waiting"}
_PPT_ARCHIVE_RECHECK_SECONDS = 10 * 60
_CHANGKE_SESSION_LOCK = threading.Lock()
_QR_DEVICE_LOCK = threading.Lock()


class ClassroomClientError(PluginServiceError):
    pass


@dataclass(frozen=True)
class Term:
    id: str
    name: str
    year: str
    season: str
    begin_date: str
    end_date: str
    current: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Course:
    id: str
    title: str
    teacher: str
    type: str
    model: str
    start_at: str
    end_at: str
    lesson_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LessonResource:
    kind: str
    title: str
    url: str | None = None
    file_type: str | None = None
    token: str | None = None
    status: str | None = None

    @property
    def is_downloadable(self) -> bool:
        return bool(self.url)


@dataclass(frozen=True)
class Lesson:
    id: str
    title: str
    status_label: str
    start_at: str
    end_at: str
    duration_seconds: int | None
    lecturer: str
    resources: list[LessonResource]
    is_live: bool = False
    room_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CourseDetail:
    course: Course
    lessons: list[Lesson]


@dataclass(frozen=True)
class ExportedFile:
    filename: str
    content: bytes
    content_type: str
    saved_path: Path
    from_cache: bool = False


@dataclass(frozen=True)
class LessonExportTarget:
    kind: str
    filename: str
    path: Path
    content_type: str
    exists: bool


@dataclass(frozen=True)
class LiveLesson:
    course_id: str
    course_title: str
    lesson_id: str
    lesson_title: str
    lecturer: str
    room_name: str
    start_at: str
    end_at: str


@dataclass(frozen=True)
class LiveLessonInfo:
    course_id: str
    lesson_id: str
    course_title: str
    lesson_title: str
    lecturer: str
    room_name: str
    start_at: str
    end_at: str
    is_live: bool
    playlist_urls: dict[str, str]
    ppt_running: bool
    official_url: str


@dataclass(frozen=True)
class LivePptSlide:
    id: int
    image_url: str
    thumbnail_url: str
    created_at: str
    offset_seconds: int


class ClassroomClient:
    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or AppConfig()
        self.cache = JsonCache(self.config.cache_dir / "classroom")
        self._session_recoverer: Callable[[], Any] | None = None

    def set_session_recoverer(self, recoverer: Callable[[], Any]) -> None:
        """Attach the shared GUET login recovery used by Changke SSO."""
        self._session_recoverer = recoverer

    def verify_session(self) -> None:
        self._get_json(COURSE_LIST_PATH, {"nowpage": "1", "per-page": "1"})

    def list_terms(self) -> list[Term]:
        payload = self._get_json_cached("terms", TERM_LIST_PATH)
        return self._terms_from_payload(payload)

    def refresh_terms(self) -> list[Term]:
        payload = self._get_json(TERM_LIST_PATH)
        self.cache.set("terms", payload)
        return self._terms_from_payload(payload)

    def _terms_from_payload(self, payload: dict[str, Any]) -> list[Term]:
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        rows = params.get("list", [])
        if not isinstance(rows, list):
            return []
        return [self._term_from_row(row) for row in rows if isinstance(row, dict)]

    def list_courses(
        self,
        page: int = 1,
        page_size: int = 50,
        term_id: str | None = None,
        term: Term | None = None,
        *,
        force_refresh: bool = False,
    ) -> list[Course]:
        cache_name = f"courses_page_{page}_size_{page_size}"
        params = {"nowpage": str(page), "per-page": str(page_size)}
        if force_refresh:
            payload = self._get_json(COURSE_LIST_PATH, params)
            self.cache.set(cache_name, payload)
        else:
            payload = self._get_json_cached(cache_name, COURSE_LIST_PATH, params)
        result = self._result(payload)
        rows = result.get("data", [])
        selected_term = term
        if selected_term is None and term_id:
            selected_term = next((item for item in self.list_terms() if item.id == term_id), None)
        if selected_term is not None:
            rows = [row for row in rows if self._course_matches_term(row, selected_term)]
        return [self._course_from_row(row) for row in rows]

    def get_course_detail(self, course_id: str) -> CourseDetail:
        payload = self._get_json_cached(
            f"course_detail_{course_id}",
            COURSE_DETAIL_PATH.format(course_id=course_id),
        )
        return self._course_detail_from_payload(payload)

    def refresh_course_detail(self, course_id: str) -> CourseDetail:
        payload = self._get_json(COURSE_DETAIL_PATH.format(course_id=course_id))
        self.cache.set(f"course_detail_{course_id}", payload)
        return self._course_detail_from_payload(payload)

    def list_current_live_lessons(self, courses: list[Course]) -> list[LiveLesson]:
        if not courses:
            return []
        tenant_code = next(
            (
                str(course.raw.get("TenantCode") or course.raw.get("tenant_code") or "")
                for course in courses
                if course.raw
            ),
            "",
        ) or "21"
        payload = self._get_json(
            LIVE_LIST_PATH,
            {
                "sub_live_status": "1",
                "tenant": tenant_code,
                "page": "1",
                "per_page": "500",
            },
        )
        enrolled = {course.id: course for course in courses}
        rows = payload.get("list") if isinstance(payload.get("list"), list) else []
        live_lessons: list[LiveLesson] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            course_id = str(row.get("course_id") or row.get("id") or "")
            lesson_id = str(row.get("sub_id") or "")
            course = enrolled.get(course_id)
            if (
                course is None
                or not lesson_id
                or str(row.get("sub_status") or row.get("status") or "") != "1"
            ):
                continue
            live_lessons.append(
                LiveLesson(
                    course_id=course_id,
                    course_title=str(row.get("course_title") or row.get("title") or course.title),
                    lesson_id=lesson_id,
                    lesson_title=str(row.get("sub_title") or "正在直播"),
                    lecturer=str(row.get("lecturer_name") or course.teacher),
                    room_name=str(row.get("room_name") or ""),
                    start_at=_format_time(row.get("start_at")),
                    end_at=_format_time(row.get("end_at")),
                )
            )
        return live_lessons

    def get_live_lesson_info(self, course_id: str, lesson_id: str) -> LiveLessonInfo:
        payload = self._get_json(
            LIVE_INFO_PATH,
            {"course_id": course_id, "sub_id": lesson_id},
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if str(data.get("course_id") or "") != str(course_id) or str(data.get("sub_id") or "") != str(lesson_id):
            raise ClassroomClientError("直播课次信息不匹配")
        live_url = data.get("live_url") if isinstance(data.get("live_url"), dict) else {}
        output = live_url.get("output") if isinstance(live_url.get("output"), dict) else {}
        playlist_urls = {
            label: str(output.get(field) or "")
            for label, field in (("original", "m3u8"), ("high", "m3u8_lhd"), ("standard", "m3u8_lsd"))
            if output.get(field)
        }
        content = data.get("content") if isinstance(data.get("content"), dict) else {}
        api_pass = content.get("api_pass") if isinstance(content.get("api_pass"), dict) else {}
        is_live = str(data.get("sub_status") or "") == "1"
        if is_live and not playlist_urls:
            raise ClassroomClientError(str(data.get("play_msg") or "直播流暂未就绪"))
        return LiveLessonInfo(
            course_id=str(course_id),
            lesson_id=str(lesson_id),
            course_title=str(data.get("course_title") or ""),
            lesson_title=str(data.get("sub_title") or ""),
            lecturer=str(data.get("lecturer_name") or ""),
            room_name=str(data.get("room_name") or ""),
            start_at=_format_time(data.get("start_at")),
            end_at=_format_time(data.get("end_at")),
            is_live=is_live,
            playlist_urls=playlist_urls,
            ppt_running=str(api_pass.get("ppt_status") or "") == "running",
            official_url=self._absolute_url(
                "/livingroom",
                {"course_id": str(course_id), "sub_id": str(lesson_id), "tenant_code": str(data.get("tenant_code") or "21")},
            ),
        )

    def list_live_ppt_slides(
        self,
        course_id: str,
        lesson_id: str,
        *,
        after_id: int = 0,
    ) -> list[LivePptSlide]:
        slides: list[LivePptSlide] = []
        seen_ids: set[int] = set()
        page = 1
        per_page = 100
        while page <= 50:
            payload = self._get_json(
                LIVE_PPT_PATH,
                {
                    "course_id": course_id,
                    "sub_id": lesson_id,
                    "page": str(page),
                    "per_page": str(per_page),
                },
            )
            rows = payload.get("list") if isinstance(payload.get("list"), list) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                slide_id = _to_int(row.get("id")) or 0
                if slide_id <= after_id or slide_id in seen_ids:
                    continue
                raw_content = row.get("content")
                try:
                    content = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
                except json.JSONDecodeError:
                    continue
                if not isinstance(content, dict):
                    continue
                image_url = str(content.get("pptimgurl") or "")
                if not _is_allowed_classroom_asset(image_url, "/play/"):
                    continue
                thumbnail_url = str(content.get("pptthumb") or "")
                if thumbnail_url and not _is_allowed_classroom_asset(thumbnail_url, "/play/"):
                    thumbnail_url = ""
                seen_ids.add(slide_id)
                slides.append(
                    LivePptSlide(
                        id=slide_id,
                        image_url=image_url,
                        thumbnail_url=thumbnail_url,
                        created_at=str(content.get("created") or row.get("create_time") or ""),
                        offset_seconds=_to_int(row.get("created_sec")) or 0,
                    )
                )
            total = _to_int(payload.get("total"))
            if not rows or len(rows) < per_page or (total is not None and page * per_page >= total):
                break
            page += 1
        return sorted(slides, key=lambda slide: (slide.id, slide.offset_seconds))

    def get_live_playlist_url(self, course_id: str, lesson_id: str, quality: str) -> str:
        info = self.get_live_lesson_info(course_id, lesson_id)
        if not info.is_live:
            raise ClassroomClientError("该课次当前不在直播中")
        preferred = quality if quality in {"original", "high", "standard"} else "original"
        url = info.playlist_urls.get(preferred) or info.playlist_urls.get("original")
        if not url or not _is_allowed_live_media_url(url):
            raise ClassroomClientError("直播播放地址不可用")
        return url

    def download_live_media(self, url: str, *, max_bytes: int = 32 * 1024 * 1024) -> tuple[bytes, str]:
        if not _is_allowed_live_media_url(url):
            raise ClassroomClientError("不允许代理该媒体地址")
        return self._download_live_asset(url, max_bytes=max_bytes)

    def download_live_ppt_image(self, url: str) -> tuple[bytes, str]:
        if not _is_allowed_classroom_asset(url, "/play/"):
            raise ClassroomClientError("不允许代理该课件图片地址")
        return self._download_live_asset(url, max_bytes=12 * 1024 * 1024)

    def get_qr_device_id(self) -> str:
        """Return a stable, locally generated device id for the signed-in account."""
        path = self.config.data_dir / "account" / "qr_device.json"
        with _QR_DEVICE_LOCK:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                value = str(payload.get("device_id") or "")
                return str(uuid.UUID(value))
            except (OSError, ValueError, json.JSONDecodeError, AttributeError):
                pass

            value = str(uuid.uuid4())
            atomic_write_text(
                path,
                json.dumps({"schema": 1, "device_id": value}, ensure_ascii=False, indent=2)
                + "\n",
            )
            return value

    def answer_qr_rollcall(
        self,
        rollcall_id: str,
        data: str,
        device_id: str,
    ) -> dict[str, Any]:
        """Submit a scanned QR token through the official Changke endpoint."""
        rollcall_id = str(rollcall_id).strip()
        data = str(data).strip()
        if not re.fullmatch(r"\d+", rollcall_id) or not data:
            raise ClassroomClientError("二维码签到参数不完整")

        body = json.dumps(
            {"data": data, "deviceId": str(device_id)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        url = f"{CHANGKE_BASE_URL}/api/rollcall/{quote(rollcall_id, safe='')}/answer_qr_rollcall"

        for attempt in range(2):
            session_id = self._ensure_changke_session(force=attempt > 0)
            headers = _request_headers("application/json")
            headers.update(
                {
                    "Content-Type": "application/json",
                    "Origin": CHANGKE_BASE_URL,
                    "Referer": f"{CHANGKE_BASE_URL}/",
                    "x-session-id": session_id,
                }
            )
            request = Request(url, data=body, headers=headers, method="PUT")
            opener = build_opener(HTTPCookieProcessor(self._cookie_jar()))
            try:
                with opener.open(request, timeout=30) as response:
                    status = int(getattr(response, "status", 200))
                    raw = response.read().decode("utf-8", errors="replace")
            except HTTPError as exc:
                status = exc.code
                raw = exc.read().decode("utf-8", errors="replace")
            except URLError as exc:
                raise ClassroomClientError(f"畅课签到请求失败：{exc.reason}") from exc

            if status in (401, 403) and attempt == 0:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                if attempt == 0:
                    continue
                if status in (401, 403):
                    raise ClassroomClientError("畅课登录会话已失效，请重新登录") from exc
                raise ClassroomClientError("畅课签到接口返回异常") from exc
            if not isinstance(payload, dict):
                raise ClassroomClientError("畅课签到接口返回异常")
            payload["_http_status"] = status
            return payload

        raise ClassroomClientError("畅课登录会话已失效，请重新登录")

    def _ensure_changke_session(self, *, force: bool = False) -> str:
        if not force:
            saved = self._stored_changke_session()
            if saved:
                return saved
        with _CHANGKE_SESSION_LOCK:
            if not force:
                saved = self._stored_changke_session()
                if saved:
                    return saved
            recovery_error = ""
            for recovery_attempt in range(2):
                if not self.config.storage_state_path.is_file():
                    if recovery_attempt == 0 and self._recover_guet_session():
                        continue
                    raise ClassroomClientError(
                        recovery_error or "尚未保存统一桂电登录会话，请先在账户与认证中保存账号"
                    )
                try:
                    storage_state = json.loads(
                        self.config.storage_state_path.read_text(encoding="utf-8")
                    )
                    if force:
                        storage_state["cookies"] = [
                            item
                            for item in storage_state.get("cookies", [])
                            if not (
                                item.get("name") in {"session", "role_token"}
                                and str(item.get("domain") or "").lstrip(".")
                                == "courses.guet.edu.cn"
                            )
                        ]

                    from playwright.sync_api import sync_playwright

                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(headless=True)
                        try:
                            context = browser.new_context(storage_state=storage_state)
                            page = context.new_page()
                            page.goto(
                                f"{CHANGKE_BASE_URL}/",
                                wait_until="domcontentloaded",
                                timeout=60_000,
                            )
                            page.wait_for_timeout(1500)
                            updated_state = context.storage_state()
                        finally:
                            browser.close()
                except Exception as exc:  # noqa: BLE001 - one user-facing auth boundary.
                    recovery_error = f"无法建立畅课签到会话：{exc}"
                    if recovery_attempt == 0 and self._recover_guet_session():
                        continue
                    raise ClassroomClientError(recovery_error) from exc

                session_id = _changke_session_from_state(updated_state)
                if session_id:
                    atomic_write_text(
                        self.config.storage_state_path,
                        json.dumps(updated_state, ensure_ascii=False, indent=2) + "\n",
                    )
                    return session_id
                if recovery_attempt == 0 and self._recover_guet_session():
                    continue
                break
            raise ClassroomClientError(
                recovery_error or "畅课单点登录失败，请在账户与认证中重新登录"
            )

    def _recover_guet_session(self) -> bool:
        if self._session_recoverer is None:
            return False
        try:
            status = self._session_recoverer()
        except Exception:  # noqa: BLE001 - the caller reports one stable auth error.
            return False
        return bool(getattr(status, "is_logged_in", status))

    def _stored_changke_session(self) -> str:
        try:
            storage_state = json.loads(
                self.config.storage_state_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return ""
        return _changke_session_from_state(storage_state)

    def _download_live_asset(self, url: str, *, max_bytes: int) -> tuple[bytes, str]:
        request = Request(url, headers=_request_headers("*/*"))
        opener = build_opener(HTTPCookieProcessor(self._cookie_jar()))
        try:
            with opener.open(request, timeout=30) as response:
                content = response.read(max_bytes + 1)
                if len(content) > max_bytes:
                    raise ClassroomClientError("直播媒体分片过大")
                return content, response.headers.get("content-type", "application/octet-stream")
        except HTTPError as exc:
            raise ClassroomClientError(_http_error_message(exc)) from exc
        except URLError as exc:
            raise ClassroomClientError(f"直播媒体请求失败：{exc.reason}") from exc

    def _course_detail_from_payload(self, payload: dict[str, Any]) -> CourseDetail:
        row = self._result(payload)
        course = self._course_from_detail(row)
        lessons = [self._lesson_from_row(item, course.id) for item in row.get("subject", [])]
        return CourseDetail(course=course, lessons=lessons)

    def build_ppt_export_url(self, lesson_id: str) -> str:
        return self._absolute_url(PPT_EXPORT_PATH, {"sub_id": lesson_id})

    def build_trans_export_url(self, lesson_id: str) -> str:
        return self._absolute_url(TRANS_EXPORT_PATH, {"sub_id": lesson_id})

    def get_lesson_export_target(self, course_id: str, lesson_id: str, export_kind: str) -> LessonExportTarget:
        detail = self.get_course_detail(course_id)
        lesson = next((item for item in detail.lessons if item.id == lesson_id), None)
        if lesson is None:
            raise ClassroomClientError("未找到指定课次")
        label, extension, content_type = _export_kind_info(export_kind)
        filename = f"{_safe_filename(detail.course.title)}_{_safe_filename(lesson.title)}_{label}{extension}"
        path = (
            self.config.downloads_dir
            / _safe_filename(detail.course.title)
            / _safe_filename(lesson.title)
            / filename
        )
        exists = path.exists()
        if export_kind == "ppt" and exists:
            exists = self._ppt_cache_is_reusable(course_id, lesson_id, path)
        return LessonExportTarget(
            kind=export_kind,
            filename=filename,
            path=path,
            content_type=content_type,
            exists=exists,
        )

    def download_lesson_export(self, course_id: str, lesson_id: str, export_kind: str) -> ExportedFile:
        target = self.get_lesson_export_target(course_id, lesson_id, export_kind)
        if target.exists:
            return ExportedFile(
                filename=target.filename,
                content=target.path.read_bytes(),
                content_type=target.content_type,
                saved_path=target.path,
                from_cache=True,
            )
        if export_kind == "ppt":
            path = PPT_EXPORT_PATH
        elif export_kind == "transcript":
            path = TRANS_EXPORT_PATH
        else:
            raise ClassroomClientError("不支持的导出类型")

        content, response_type = self._get_bytes(
            path,
            {"course_id": course_id, "sub_id": lesson_id},
            no_cache=export_kind == "ppt",
        )
        if content.lstrip().startswith(b"{"):
            try:
                payload = json.loads(content.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                raise ClassroomClientError("平台导出响应解析失败") from exc
            message = payload.get("message") or payload.get("msg") or "平台导出失败"
            raise ClassroomClientError(str(message))

        target.path.parent.mkdir(parents=True, exist_ok=True)
        target.path.write_bytes(content)
        if export_kind == "ppt":
            self._mark_ppt_cache_state(course_id, lesson_id, target.path)
        return ExportedFile(
            filename=target.filename,
            content=content,
            content_type=(
                response_type
                if response_type
                and "json" not in response_type
                and "text/html" not in response_type
                else target.content_type
            ),
            saved_path=target.path,
        )

    def _ppt_cache_is_reusable(self, course_id: str, lesson_id: str, path: Path) -> bool:
        meta = self._read_ppt_cache_meta(path)
        meta_matches = (
            meta is not None
            and meta.get("schema") == _PPT_CACHE_META_SCHEMA
            and str(meta.get("course_id") or "") == str(course_id)
            and str(meta.get("lesson_id") or "") == str(lesson_id)
        )

        # A recording-ready cache was verified against the uncached live list after
        # the lesson had a recording. That state is stable enough to reuse directly.
        if (
            meta_matches
            and meta.get("provisional") is False
            and str(meta.get("ppt_status") or "") == "recording-ready"
        ):
            return True
        if (
            meta_matches
            and str(meta.get("ppt_status") or "") == "archive-newer"
            and self._ppt_cache_meta_is_recent(path)
        ):
            return True

        try:
            provisional, status = self._current_ppt_cache_state(course_id, lesson_id)
        except ClassroomClientError:
            # Network/session failure must not throw away an otherwise usable local file.
            # Keep the cache unfinalized so a later access retries the state check.
            return True

        if provisional:
            self._write_ppt_cache_meta(
                path,
                course_id=course_id,
                lesson_id=lesson_id,
                provisional=True,
                status=status,
            )
            return True

        if (
            meta_matches
            and meta.get("provisional") is False
            and str(meta.get("ppt_status") or "") == status
        ):
            return True

        # Legacy metadata, old schemas, provisional caches, and a changed final
        # state all force one fresh export. This migrates PPTs that older versions
        # incorrectly marked final while they still only contained live-session pages.
        return False

    def _mark_ppt_cache_state(self, course_id: str, lesson_id: str, path: Path) -> None:
        try:
            provisional, status = self._current_ppt_cache_state(course_id, lesson_id)
        except ClassroomClientError:
            provisional, status = True, "unknown"
        if not provisional and status == "recording-ready":
            try:
                archive_count = len(self.list_live_ppt_slides(course_id, lesson_id))
                export_count = _pptx_slide_count(path)
            except (ClassroomClientError, OSError, zipfile.BadZipFile):
                pass
            else:
                if archive_count > export_count:
                    provisional, status = True, "archive-newer"
        try:
            self._write_ppt_cache_meta(
                path,
                course_id=course_id,
                lesson_id=lesson_id,
                provisional=provisional,
                status=status,
            )
        except OSError:
            # The PPT itself was downloaded successfully. Metadata failure should not
            # make the export unusable; next access will simply validate it again.
            pass

    def _current_ppt_cache_state(self, course_id: str, lesson_id: str) -> tuple[bool, str]:
        detail = self.refresh_course_detail(course_id)
        lesson = next((item for item in detail.lessons if item.id == lesson_id), None)
        if lesson is None:
            raise ClassroomClientError("未找到指定课次")

        # api_pass.ppt_status may stay "running" after a lesson has already become
        # a recording. The uncached live-list is the authoritative live signal.
        try:
            live_ids = {
                item.lesson_id
                for item in self.list_current_live_lessons([detail.course])
            }
        except ClassroomClientError:
            live_ids = None
        if live_ids is not None:
            if lesson_id in live_ids:
                return True, "live"
            has_recording = any(
                item.kind == "video" and item.is_downloadable
                for item in lesson.resources
            )
            if has_recording:
                return False, "recording-ready"

        resource = next((item for item in lesson.resources if item.kind == "ppt"), None)
        if resource is None:
            # If the platform temporarily stops advertising the export, do not
            # invalidate a local PPT that may be the only usable copy.
            return True, "unavailable"
        status = str(resource.status or "").strip().lower()
        if status:
            return status in _PPT_PROVISIONAL_STATUSES, status
        return lesson.is_live, "running" if lesson.is_live else "ready"

    @staticmethod
    def _ppt_cache_meta_path(path: Path) -> Path:
        return path.with_suffix(path.suffix + ".meta.json")

    def _read_ppt_cache_meta(self, path: Path) -> dict[str, Any] | None:
        meta_path = self._ppt_cache_meta_path(path)
        if not meta_path.is_file():
            return None
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _ppt_cache_meta_is_recent(self, path: Path) -> bool:
        try:
            age = time.time() - self._ppt_cache_meta_path(path).stat().st_mtime
        except OSError:
            return False
        return 0 <= age < _PPT_ARCHIVE_RECHECK_SECONDS

    def _write_ppt_cache_meta(
        self,
        path: Path,
        *,
        course_id: str,
        lesson_id: str,
        provisional: bool,
        status: str,
    ) -> None:
        meta_path = self._ppt_cache_meta_path(path)
        payload = {
            "schema": _PPT_CACHE_META_SCHEMA,
            "course_id": str(course_id),
            "lesson_id": str(lesson_id),
            "provisional": bool(provisional),
            "ppt_status": status,
        }
        atomic_write_text(
            meta_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = self._absolute_url(path, params)
        request = Request(url, headers=_request_headers("application/json"))
        opener = build_opener(HTTPCookieProcessor(self._cookie_jar()))
        try:
            with opener.open(request, timeout=30) as response:
                content_type = response.headers.get("content-type", "")
                body = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            message = _http_error_message(exc)
            raise ClassroomClientError(message) from exc
        except URLError as exc:
            raise ClassroomClientError(f"平台接口请求失败：{exc.reason}") from exc

        if "json" not in content_type and not body.lstrip().startswith("{"):
            raise ClassroomClientError("平台返回的不是 JSON，登录会话可能已失效")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ClassroomClientError("平台 JSON 解析失败") from exc

        status = payload.get("status")
        if status is None:
            status = payload.get("code")
        if status not in (0, 200, 1000):
            message = payload.get("message") or payload.get("msg") or "平台接口返回失败"
            raise ClassroomClientError(str(message))
        return payload

    def _get_json_cached(
        self,
        cache_name: str,
        path: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        cached = self.cache.get(cache_name)
        if cached is not None:
            return cached
        payload = self._get_json(path, params)
        self.cache.set(cache_name, payload)
        return payload

    def _get_bytes(
        self,
        path: str,
        params: dict[str, str],
        *,
        no_cache: bool = False,
    ) -> tuple[bytes, str]:
        url = self._absolute_url(path, params)
        headers = _request_headers("*/*")
        if no_cache:
            headers["Cache-Control"] = "no-cache"
            headers["Pragma"] = "no-cache"
        request = Request(url, headers=headers)
        opener = build_opener(HTTPCookieProcessor(self._cookie_jar()))
        try:
            with opener.open(request, timeout=60) as response:
                return response.read(), response.headers.get("content-type", "")
        except HTTPError as exc:
            message = _http_error_message(exc)
            raise ClassroomClientError(message) from exc
        except URLError as exc:
            raise ClassroomClientError(f"平台文件下载失败：{exc.reason}") from exc

    def _cookie_jar(self) -> CookieJar:
        if not self.config.storage_state_path.exists():
            raise ClassroomClientError("尚未保存登录会话，请先登录")

        storage_state = json.loads(self.config.storage_state_path.read_text(encoding="utf-8"))
        jar = CookieJar()
        for item in storage_state.get("cookies", []):
            domain = item.get("domain") or urlparse(BASE_URL).hostname or ""
            jar.set_cookie(
                Cookie(
                    version=0,
                    name=item["name"],
                    value=item["value"],
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=domain.startswith("."),
                    domain_initial_dot=domain.startswith("."),
                    path=item.get("path") or "/",
                    path_specified=True,
                    secure=bool(item.get("secure")),
                    expires=int(item["expires"]) if item.get("expires", -1) not in (-1, None) else None,
                    discard=item.get("expires", -1) in (-1, None),
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )
        return jar

    @staticmethod
    def _result(payload: dict[str, Any]) -> dict[str, Any]:
        params = payload.get("params")
        if isinstance(params, dict):
            result = params.get("result")
            if isinstance(result, dict):
                return result
            data = params.get("data")
            if isinstance(data, dict):
                return data

        data = payload.get("data")
        if isinstance(data, dict):
            nested = data.get("data")
            if isinstance(nested, dict):
                return nested
            return data
        raise ClassroomClientError("平台响应缺少 result/data 字段")

    @staticmethod
    def _term_from_row(row: dict[str, Any]) -> Term:
        return Term(
            id=str(row.get("id") or ""),
            name=str(row.get("term_name") or row.get("name") or ""),
            year=str(row.get("year") or ""),
            season=str(row.get("season") or ""),
            begin_date=str(row.get("begin_date") or ""),
            end_date=str(row.get("end_date") or ""),
            current=bool(row.get("current")),
            raw=row,
        )

    @staticmethod
    def _course_from_row(row: dict[str, Any]) -> Course:
        return Course(
            id=str(row.get("Id") or row.get("id") or ""),
            title=str(row.get("Title") or row.get("title") or "未命名课程"),
            teacher=str(row.get("Teacher") or row.get("teacher") or ""),
            type=str(row.get("Type") or row.get("type") or ""),
            model=str(row.get("Model") or row.get("model") or ""),
            start_at=_format_time(row.get("StartAt") or row.get("start_at")),
            end_at=_format_time(row.get("EndAt") or row.get("end_at")),
            raw=row,
        )

    @staticmethod
    def _course_from_detail(row: dict[str, Any]) -> Course:
        course = ClassroomClient._course_from_row(row)
        return Course(
            id=course.id,
            title=course.title,
            teacher=str(row.get("teacher") or row.get("Teacher") or row.get("realname") or ""),
            type=course.type,
            model=course.model,
            start_at=course.start_at,
            end_at=course.end_at,
            lesson_count=len(row.get("subject", [])),
            raw=row,
        )

    @staticmethod
    def _lesson_from_row(row: dict[str, Any], course_id: str) -> Lesson:
        content = row.get("content") if isinstance(row.get("content"), dict) else {}
        api_pass = content.get("api_pass") if isinstance(content.get("api_pass"), dict) else {}
        is_live = any(str(api_pass.get(key) or "") == "running" for key in ("ppt_status", "qlite_status"))
        status_label = str(row.get("status_label") or "")
        if is_live:
            status_label = "直播中"
        return Lesson(
            id=str(row.get("id") or ""),
            title=str(row.get("title") or "未命名课次"),
            status_label=status_label,
            start_at=_format_time(row.get("start_at")),
            end_at=_format_time(row.get("end_at")),
            duration_seconds=_to_int(row.get("duration")),
            lecturer=str(row.get("lecturer_name") or ""),
            resources=ClassroomClient._resources_from_content(content, str(row.get("id") or ""), course_id),
            is_live=is_live,
            room_name=str(content.get("address") or ""),
            raw=row,
        )

    @staticmethod
    def _resources_from_content(content: dict[str, Any], lesson_id: str, course_id: str) -> list[LessonResource]:
        resources: list[LessonResource] = []
        for item in content.get("file_list", []) or []:
            if not isinstance(item, dict):
                continue
            url = item.get("file_name")
            file_type = str(item.get("file_type") or "")
            if not url:
                continue
            kind = _resource_kind(url, file_type)
            resources.append(
                LessonResource(
                    kind=kind,
                    title=_resource_title(kind, file_type),
                    url=str(url),
                    file_type=file_type,
                )
            )

        api_pass = content.get("api_pass") if isinstance(content.get("api_pass"), dict) else {}
        ppt_token = api_pass.get("ppt")
        if ppt_token:
            resources.append(
                LessonResource(
                    kind="ppt",
                    title="下载课件",
                    url=ClassroomClient._absolute_url(
                        PPT_EXPORT_PATH,
                        {"course_id": course_id, "sub_id": lesson_id},
                    ),
                    token=str(ppt_token),
                    status=str(api_pass.get("ppt_status") or ""),
                )
            )
        qlite_token = api_pass.get("qlite")
        if qlite_token:
            resources.append(
                LessonResource(
                    kind="transcript",
                    title="导出转写",
                    url=ClassroomClient._absolute_url(
                        TRANS_EXPORT_PATH,
                        {"course_id": course_id, "sub_id": lesson_id},
                    ),
                    token=str(qlite_token),
                    status=str(api_pass.get("qlite_status") or ""),
                )
            )
        return resources

    @staticmethod
    def _absolute_url(path: str, params: dict[str, str] | None = None) -> str:
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        return url

    @staticmethod
    def _course_matches_term(row: dict[str, Any], term: Term) -> bool:
        term_start = _parse_date(term.begin_date)
        term_end = _parse_date(term.end_date)
        course_start = _date_from_timestamp(row.get("StartAt") or row.get("start_at"))
        course_end = _date_from_timestamp(row.get("EndAt") or row.get("end_at"))
        if term_start is None or term_end is None or course_start is None or course_end is None:
            return False
        return course_start <= term_end and course_end >= term_start


def _format_time(value: Any) -> str:
    seconds = _to_int(value)
    if seconds is None:
        return ""
    return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _date_from_timestamp(value: Any) -> date | None:
    seconds = _to_int(value)
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds).date()


def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _resource_kind(url: str, file_type: str) -> str:
    lowered = f"{url} {file_type}".lower()
    if ".mp4" in lowered or "mp4" in lowered:
        return "video"
    if any(ext in lowered for ext in (".jpg", ".jpeg", ".png", ".webp")):
        return "cover"
    return "file"


def _resource_title(kind: str, file_type: str) -> str:
    if kind == "video":
        return "课堂录像"
    if kind == "cover":
        return "封面/预览图"
    return file_type or "资源文件"


def _safe_filename(value: str) -> str:
    cleaned = "".join(char if char not in r'\/:*?"<>|' else "_" for char in value).strip()
    return cleaned[:80] or "未命名"


def _pptx_slide_count(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        return sum(
            1
            for name in archive.namelist()
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
        )


def _export_kind_info(export_kind: str) -> tuple[str, str, str]:
    if export_kind == "ppt":
        return (
            "课件",
            ".pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
    if export_kind == "transcript":
        return (
            "转写",
            ".docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    raise ClassroomClientError("不支持的导出类型")


def _request_headers(accept: str) -> dict[str, str]:
    return {
        "Accept": accept,
        "Referer": f"{BASE_URL}/?tenant_code=21",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
    }


def _changke_session_from_state(storage_state: dict[str, Any]) -> str:
    for item in storage_state.get("cookies", []):
        if not isinstance(item, dict) or item.get("name") != "session":
            continue
        domain = str(item.get("domain") or "").lstrip(".").lower()
        if domain != "courses.guet.edu.cn":
            continue
        value = str(item.get("value") or "")
        if value:
            return value
    return ""


def is_allowed_live_media_url(url: str) -> bool:
    return _is_allowed_classroom_asset(url, "/pgc/")


def rewrite_live_playlist(content: str, source_url: str, proxy_path: str = "/api/live/media") -> str:
    """Rewrite every HLS child URI through the restricted local media proxy."""

    def proxy_url(target: str) -> str:
        absolute = urljoin(source_url, target)
        if not is_allowed_live_media_url(absolute):
            raise ClassroomClientError("直播播放列表包含不允许的媒体地址")
        return f"{proxy_path}?url={quote(absolute, safe='')}"

    uri_pattern = re.compile(r'URI="([^"]+)"')
    rewritten: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            line = proxy_url(stripped)
        elif "URI=" in line:
            line = uri_pattern.sub(lambda match: f'URI="{proxy_url(match.group(1))}"', line)
        rewritten.append(line)
    return "\n".join(rewritten) + ("\n" if content.endswith("\n") else "")


def _is_allowed_classroom_asset(url: str, path_prefix: str) -> bool:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "classroom.guet.edu.cn"
        and port in (None, 443)
        and not parsed.username
        and not parsed.password
        and parsed.path.startswith(path_prefix)
    )


def _is_allowed_live_media_url(url: str) -> bool:
    return is_allowed_live_media_url(url)


def _http_error_message(exc: HTTPError) -> str:
    body = exc.read().decode("utf-8", errors="replace").strip()
    if body.startswith("{"):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        message = payload.get("message") or payload.get("msg")
        if message:
            return str(message)
    if "wengine-auth-failed" in body or "访问出错 - 403" in body:
        return "平台认证失败或登录会话已失效，请重新登录后再试"
    reason = exc.reason or "平台拒绝了请求"
    return f"平台接口请求失败：HTTP {exc.code} {reason}"
