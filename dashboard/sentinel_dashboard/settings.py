"""mr-sentinel dashboard settings — a local, single-machine tool (127.0.0.1).

Two databases, on purpose:
- "default"  (dashboard/dashboard.db): Django's own tables — users, sessions,
  admin log. Django migrates these.
- "history"  (the repo's sentinel.db): owned by the stdlib `history` package.
  Django never migrates it (every model there is managed=False, see routers.py)
  and only ever *adds* rows to the three human tables.
"""
import json
import os
import secrets
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # dashboard/
REPO_ROOT = BASE_DIR.parent                                # mr-sentinel/
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))                     # `import history` (the core)

def _sentinel_db() -> Path:
    """Same precedence as the core (history.db.resolve_path), without loading the
    tokens: MR_SENTINEL_DB > config.json history.db_path > <repo>/sentinel.db."""
    if os.environ.get("MR_SENTINEL_DB"):
        return _from_repo(os.environ["MR_SENTINEL_DB"])
    try:
        configured = (json.loads((REPO_ROOT / "config.json").read_text())
                      .get("history") or {}).get("db_path")
    except (OSError, ValueError):
        configured = None
    return _from_repo(configured or REPO_ROOT / "sentinel.db")


def _from_repo(value) -> Path:
    """Relative paths mean relative to the repo — as in the core — so the
    dashboard (cwd dashboard/) and the jobs it starts (cwd repo) agree."""
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


SENTINEL_DB = _sentinel_db()


def _secret_key() -> str:
    """Generated once per machine, kept out of git (dashboard/.secret_key)."""
    path = BASE_DIR / ".secret_key"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(50))
        path.chmod(0o600)
    return path.read_text().strip()


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or _secret_key()
DEBUG = os.environ.get("DASHBOARD_DEBUG") == "1"
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

INSTALLED_APPS = [
    "unfold",                       # must precede django.contrib.admin (replaces admin.site)
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "reviews",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # per-person performance data: every page needs a login, even on localhost
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "sentinel_dashboard.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "sentinel_dashboard.wsgi.application"

DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "dashboard.db"},
    "history": {"ENGINE": "django.db.backends.sqlite3", "NAME": SENTINEL_DB,
                "OPTIONS": {"timeout": 30},
                # a real file (not :memory:), so the core's own sqlite3 connection
                # sees the same test database as the ORM
                "TEST": {"NAME": BASE_DIR / ".test_sentinel.db"}},
}
DATABASE_ROUTERS = ["sentinel_dashboard.routers.HistoryRouter"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]
LOGIN_URL = "admin:login"
LOGIN_REDIRECT_URL = "admin:index"
LOGOUT_REDIRECT_URL = "admin:login"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 8 * 3600

LANGUAGE_CODE = "zh-hant"
TIME_ZONE = "Asia/Taipei"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------- django-unfold (admin theme; also the shell of our own pages) ----------
from django.templatetags.static import static  # noqa: E402
from django.urls import reverse_lazy  # noqa: E402


def _nav(title, icon, link):
    return {"title": title, "icon": icon, "link": link}


UNFOLD = {
    "SITE_TITLE": "mr-sentinel",
    "SITE_HEADER": "mr-sentinel",
    "SITE_SUBHEADER": "Review 歷史",
    "SITE_SYMBOL": "shield_person",
    "SITE_URL": None,
    "THEME": "light",               # light only, no switcher
    "SHOW_HISTORY": False,
    "SHOW_VIEW_ON_SITE": False,
    "STYLES": [lambda request: static("dashboard/dashboard.css")],
    "DASHBOARD_CALLBACK": "reviews.views.dashboard_callback",
    "COLORS": {                     # Linear-like: neutral base + one indigo accent
        "primary": {
            "50": "oklch(96.2% .018 272.314)", "100": "oklch(93% .034 272.788)",
            "200": "oklch(87% .065 274.039)", "300": "oklch(78.5% .115 274.713)",
            "400": "oklch(67.3% .182 276.935)", "500": "oklch(58.5% .16 277.117)",
            "600": "oklch(53% .15 276.966)", "700": "oklch(45.7% .14 277.023)",
            "800": "oklch(39.8% .12 277.366)", "900": "oklch(35.9% .1 278.697)",
            "950": "oklch(25.7% .07 281.288)",
        },
        "font": {
            "default-light": "var(--color-base-700)",
            "important-light": "var(--color-base-900)",
        },
    },
    "SIDEBAR": {
        "show_search": False,
        "show_all_applications": False,
        "navigation": [
            {"title": "Review 歷史", "separator": False, "items": [
                _nav("團隊總覽", "monitoring", reverse_lazy("admin:index")),
                _nav("評分變動紀錄", "timeline", reverse_lazy("score_log")),
            ]},
            {"title": "設定", "separator": True, "items": [
                _nav("成員管理", "group", reverse_lazy("members")),
                _nav("Email 對應", "alternate_email", reverse_lazy("emails")),
                _nav("評分設定", "tune", reverse_lazy("scoring")),
            ]},
            {"title": "原始資料(唯讀)", "separator": True, "collapsible": True, "items": [
                _nav("開發者", "person_search", reverse_lazy("admin:reviews_person_changelist")),
                _nav("Merge requests", "merge", reverse_lazy("admin:reviews_mergerequest_changelist")),
                _nav("Findings", "bug_report", reverse_lazy("admin:reviews_finding_changelist")),
            ]},
            {"title": "紀錄", "separator": True, "collapsible": True, "items": [
                _nav("覆核紀錄", "fact_check", reverse_lazy("admin:reviews_findingreview_changelist")),
                _nav("角色紀錄", "badge", reverse_lazy("admin:reviews_personrole_changelist")),
                _nav("Email 紀錄", "contact_mail", reverse_lazy("admin:reviews_emailalias_changelist")),
                _nav("評分版本", "history", reverse_lazy("admin:reviews_scoringconfig_changelist")),
                _nav("同步請求", "sync", reverse_lazy("admin:reviews_syncrequest_changelist")),
            ]},
            {"title": "帳號", "separator": True, "collapsible": True, "items": [
                _nav("使用者", "person", reverse_lazy("admin:auth_user_changelist")),
            ]},
        ],
    },
}
