"""The unfold sidebar. 個人軌跡 has one entry per member, each with an icon when
something needs a look (high 個案 / 要關注 / 一起查看) or improved a lot (明顯改善)."""
from django.urls import reverse, reverse_lazy
from django.utils.html import escape

from . import services


def _nav(title, icon, link, **extra):
    return {"title": title, "icon": icon, "link": link, **extra}


def _is_person(pid):
    def active(request):
        match = request.resolver_match
        return bool(match and match.url_name == "trajectory" and request.GET.get("person") == str(pid))
    return active


def _overview_active(request):
    match = request.resolver_match
    return bool(match and match.url_name == "trajectory" and not request.GET.get("person"))


def _person_title(person) -> str:
    flag = person["flag"]
    icon = services.FLAG_ICON[flag]
    mark = (f'<span class="ms-nav-flag f-{flag} material-symbols-outlined" role="img" '
            f'aria-label="{services.FLAG_TEXT[flag]}" title="{services.FLAG_TEXT[flag]}">{icon}</span>') if icon else ""
    dot = f'<i class="ms-nav-dot pc{services.person_color(person["pid"])}" aria-hidden="true"></i>'
    return f'{dot}<span class="ms-nav-person">{escape(person["name"])}</span>{mark}'


def trajectory_items():
    url = reverse("trajectory")
    items = [_nav("全員一覽", "groups", url, active=_overview_active)]
    for person in services.trajectory_nav():
        items.append(_nav(_person_title(person), "person", f"{url}?person={person['pid']}",
                          active=_is_person(person["pid"])))
    return items


def sidebar(request):
    return [
        {"title": "Review 歷史", "separator": False, "items": [
            _nav("團隊總覽", "monitoring", reverse_lazy("admin:index")),
            _nav("評分變動紀錄", "timeline", reverse_lazy("score_log")),
        ]},
        {"title": "個人軌跡", "separator": True, "items": trajectory_items()},
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
            _nav("後續 bug 覆核", "bug_report", reverse_lazy("admin:reviews_followupreview_changelist")),
            _nav("角色紀錄", "badge", reverse_lazy("admin:reviews_personrole_changelist")),
            _nav("Email 紀錄", "contact_mail", reverse_lazy("admin:reviews_emailalias_changelist")),
            _nav("評分版本", "history", reverse_lazy("admin:reviews_scoringconfig_changelist")),
            _nav("同步請求", "sync", reverse_lazy("admin:reviews_syncrequest_changelist")),
        ]},
        {"title": "帳號", "separator": True, "collapsible": True, "items": [
            _nav("使用者", "person", reverse_lazy("admin:auth_user_changelist")),
        ]},
    ]
