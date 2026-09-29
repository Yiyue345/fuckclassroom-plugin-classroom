from __future__ import annotations

from pathlib import Path

from fuckclassroom.core.plugins import AccountPanel, NavigationGroup, NavigationItem, PluginContext, PluginSpec, SettingsPanel, UIAsset


PLUGIN_DIR = Path(__file__).resolve().parent

def setup_services(context: PluginContext):
    from .services import setup_services as setup
    return setup(context)

async def startup(context: PluginContext):
    from .services import startup as worker_startup
    await worker_startup(context)
    from .lifecycle import startup as hook
    return await hook(context)


async def shutdown(context: PluginContext):
    from .services import shutdown as hook
    return await hook(context)


def build_routes(context: PluginContext):
    from .routes import build_router
    return build_router(context)


def build_account_context(services, request):
    from .accounts import build_account_context as build
    return build(services, request)


def build_course_settings_context(services, request):
    from .settings_context import build_course_settings_context as build
    return build(services, request)


def clear_session(context: PluginContext):
    return context.services.get("auth_service").clear_session()


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="classroom",
        ui_assets=(
            UIAsset("downloads.js?v=20260921-1", pages=("downloads", "lesson_outputs")),
            UIAsset("/static/vendor/hls.min.js?v=1.7.3", pages=("course_detail",)),
            UIAsset("course_media.css?v=20260922-1", "style", ("course_detail",)),
            UIAsset("course_media_fit.css?v=20260922-1", "style", ("course_detail",)),
            UIAsset("course_media.js?v=20260928-1", pages=("course_detail",)),
            UIAsset("course_media_fit.js?v=20260927-1", pages=("course_detail",)),
        ),
        name="课程录播",
        order=10,
        requires=("core_ui",),
        service_factory=setup_services,
        route_factory=build_routes,
        startup=startup,
        shutdown=shutdown,
        clear_session=clear_session,
        template_dir=PLUGIN_DIR / "templates",
        static_dir=PLUGIN_DIR / "static",
        stylesheets=("/plugins/classroom/static/classroom.css?v=20260922-1",),
        navigation_groups=(
            NavigationGroup(
                key="classroom",
                label="课程录播",
                aria_label="课程录播导航",
                system="classroom",
                order=10,
                status_template="classroom_nav_status.html",
                items=(
                    NavigationItem(key="overview", label="概览", href="/classroom", icon="layout-dashboard", active_keys=("overview",)),
                    NavigationItem(key="courses", label="我的课程", href="/courses", icon="book-open", active_keys=("courses",), badge_template="classroom_badge_courses.html"),
                    NavigationItem(key="downloads", label="文件管理", href="/downloads", icon="folder", active_keys=("downloads",)),
                ),
            ),
        ),
        account_panels=(
            AccountPanel(
                key="classroom",
                template="classroom/account_panel.html",
                order=10,
                context_factory=build_account_context,
                shortcut_label="课程录播",
                shortcut_href="/courses",
                shortcut_icon="book-open",
            ),
        ),
        settings_panels=(
            SettingsPanel(
                key="courses",
                label="课程与资源",
                template="classroom_settings_courses.html",
                order=20,
                context_factory=build_course_settings_context,
            ),
        ),
        sidebar_templates=("classroom_sidebar_session.html",),
        topbar_status_templates=("classroom_topbar_status.html",),
        system_labels=(("classroom", "课程录播"),),
    )


__all__ = ["build_plugin"]
