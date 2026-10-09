"""Our pages live inside the unfold admin shell (sidebar, header, login), so
every view is wrapped in admin.site.admin_view (urls.py) and renders with the
admin's context. The overview *is* the admin index (DASHBOARD_CALLBACK)."""
import json

from django.contrib import admin, messages
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from history import db as hdb
from history.parse import CATEGORIES

from . import services
from .models import Finding, ScoringConfig


def _page(request, template, title, **context):
    return render(request, template, {**admin.site.each_context(request), "title": title,
                                      **context})


def dashboard_callback(request, context):
    """UNFOLD DASHBOARD_CALLBACK: the admin index becomes the team overview."""
    context.update(services.overview())
    context.update(status=services.sync_status(), sync_kinds=services.SYNC_KINDS,
                   title="團隊總覽")
    return context


@require_GET
def person(request, author_id: int):
    detail = services.person_detail(author_id)
    if detail is None:
        raise Http404("沒有這個人的 MR")
    show = request.GET.get("show", "window")
    findings, followups = detail["findings"], detail["followups"]
    if show == "window":          # same range as the score above, so the numbers match
        findings = [f for f in findings if f["in_window"]]
        followups = [fu for fu in followups if fu["in_window"]]
    category = request.GET.get("category")
    if category:
        findings = [f for f in findings if f["category"] == category]
    return _page(request, "reviews/person.html", detail["person"].name or detail["person"].username,
                 **detail, shown=findings, shown_followups=followups, show=show,
                 category=category, categories=services.CATEGORY_LABELS,
                 settable_categories=CATEGORIES, severity_labels=services.SEVERITY_LABELS,
                 roles=services.ROLES)


def _back(request, fallback: str) -> str:
    back = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        back = fallback                                     # never redirect off-site
    return back


@require_POST
def review_finding(request, note_id: int):
    finding = Finding.objects.select_related("mr").filter(note_id=note_id).first()
    if finding is None:
        raise Http404
    action = request.POST.get("action")
    reason = request.POST.get("reason", "")
    try:
        if action == "category":
            services.review_finding(note_id, request.user.username,
                                    category=request.POST.get("category"), reason=reason)
            messages.success(request, f"已把「{finding.title}」改成 "
                                      f"{CATEGORIES.get(request.POST.get('category'), '')}")
        elif action in ("exclude", "include"):
            if action == "exclude" and not reason.strip():
                messages.error(request, "標記誤判要寫原因(會留在覆核紀錄裡)")
            else:
                services.review_finding(note_id, request.user.username,
                                        excluded=action == "exclude", reason=reason)
                messages.success(request, ("已標記為誤判,不計分" if action == "exclude"
                                           else "已恢復計分") + f":{finding.title}")
        else:
            return HttpResponseBadRequest("unknown action")
    except ValueError as exc:
        messages.error(request, str(exc))
    owner = services.finding_owner(note_id) or finding.mr.author_id    # blame may have moved it
    return redirect(f"{_back(request, reverse('person', args=[owner]))}#f{note_id}")


@require_POST
def review_followup(request, author_id: int):
    try:
        services.review_followup(int(request.POST.get("feature_mr_id", "")),
                                 request.POST.get("kind", ""), request.POST.get("source_ref", ""),
                                 request.POST.get("verdict", ""), request.user.username,
                                 request.POST.get("reason", ""))
    except ValueError as exc:
        messages.error(request, f"沒有存:{exc}")
    else:
        messages.success(request, f"已記錄:{services.FOLLOWUP_VERDICTS[request.POST['verdict']]},"
                                  f"分數已重新計算")
    return redirect(f"{reverse('person', args=[author_id])}#followups")


@require_POST
def set_role(request, author_id: int):
    role = request.POST.get("role", "")
    try:
        services.set_role(author_id, role, request.user.username)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, {"lead": "已設為 Team leader,不排入評分",
                                   "departed": "已標記為已離職,不排入評分",
                                   "member": "已改為成員,排入評分"}.get(role, "已更新"))
    return redirect("person", author_id=author_id)


def scoring(request):
    current = ScoringConfig.objects.order_by("-version").first()
    draft = request.POST.get("config") if request.method == "POST" else None
    if request.method == "POST":
        try:
            cfg = json.loads(draft or "")
        except ValueError as exc:
            messages.error(request, f"不是合法的 JSON:{exc}")
        else:
            try:
                saved = services.new_scoring_version(cfg, request.POST.get("note", ""),
                                                     request.user.username)
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"已建立評分公式 v{saved.version},立即生效")
                return redirect("scoring")
    elif request.method != "GET":
        return HttpResponseBadRequest()
    current_cfg = json.loads(current.config)
    return _page(request, "reviews/scoring.html", "評分設定", categories=CATEGORIES,
                 current_cfg=current_cfg,
                 max_total=current_cfg.get("item_max", 5) * len(CATEGORIES),
                 current=current, versions=ScoringConfig.objects.all()[:20],
                 draft=draft or json.dumps(json.loads(current.config), ensure_ascii=False, indent=2),
                 defaults=json.dumps(hdb.DEFAULT_SCORING, ensure_ascii=False, indent=2))


@require_POST
def sync_request(request):
    kind = request.POST.get("kind", "sync")
    try:
        services.request_sync(kind, request.user.username)
    except (ValueError, services.SyncStartError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"已送出{services.SYNC_KINDS[kind]},背景執行中"
                                  f"(完整同步約 2 分鐘、評價約 1 分鐘),重新整理即可看到結果")
    return redirect(_back(request, reverse("admin:index")))


def members(request):
    if request.method == "POST":
        try:
            added = services.add_member(request.POST.get("username", ""), request.user.username)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"已加入 {added.username},背景同步會向 GitLab 確認帳號"
                                      f"(約 1 分鐘),重新整理即可看到")
        return redirect("members")
    return _page(request, "reviews/members.html", "成員管理", **services.members())


@require_POST
def member_role(request, author_id: int):
    role = request.POST.get("role", "")
    try:
        services.set_role(author_id, role, request.user.username)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"已改為{services.ROLES[role]}")
    return redirect(_back(request, reverse("members")))


def emails(request):
    if request.method == "POST":
        raw = request.POST.get("person", "")
        try:
            gitlab_id = None if raw == "ignore" else int(raw)
            services.confirm_email(request.POST.get("email", ""), gitlab_id, request.user.username)
        except (ValueError, TypeError) as exc:
            messages.error(request, f"沒有存:{exc}")
        else:
            messages.success(request, "已儲存,分數已重新計算")
        return redirect("emails")
    return _page(request, "reviews/emails.html", "Email 對應", **services.email_page())


@require_GET
def score_log(request):
    person = request.GET.get("person")
    gitlab_id = int(person) if person and person.lstrip("-").isdigit() else None
    return _page(request, "reviews/score_log.html", "評分變動紀錄",
                 events=services.score_log(gitlab_id), person=gitlab_id,
                 people=services.Person.objects.all())
