"""Everything is under /admin/ so it shares unfold's sidebar, header and login.
Our pages are wrapped in admin_view: logged-in *staff* only."""
from django.contrib import admin
from django.shortcuts import redirect
from django.urls import path

from reviews import views


def _admin(view):
    return admin.site.admin_view(view)


urlpatterns = [
    path("", lambda request: redirect("admin:index")),
    path("admin/review/people/<int:author_id>/", _admin(views.person), name="person"),
    path("admin/review/people/<int:author_id>/role/", _admin(views.set_role), name="set_role"),
    path("admin/review/people/<int:author_id>/followups/review/", _admin(views.review_followup),
         name="review_followup"),
    path("admin/review/findings/<int:note_id>/review/", _admin(views.review_finding),
         name="review_finding"),
    path("admin/review/scoring/", _admin(views.scoring), name="scoring"),
    path("admin/review/members/", _admin(views.members), name="members"),
    path("admin/review/members/<int:author_id>/role/", _admin(views.member_role), name="member_role"),
    path("admin/review/emails/", _admin(views.emails), name="emails"),
    path("admin/review/score-log/", _admin(views.score_log), name="score_log"),
    path("admin/review/trajectory/", _admin(views.trajectory), name="trajectory"),
    path("admin/review/trajectory/alerts/<int:alert_id>/", _admin(views.trajectory_alert),
         name="trajectory_alert"),
    path("admin/review/sync/", _admin(views.sync_request), name="sync_request"),
    path("admin/", admin.site.urls),
]
