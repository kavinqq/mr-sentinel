"""Admin = the raw browser + the human tables' log. Raw tables are view-only;
the human tables accept new rows only (overrides happen on the person page,
scoring changes on /scoring/, where they are validated)."""
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group, User
from unfold.admin import ModelAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

from .models import (EmailAlias, Finding, FindingReview, FollowupReview, MergeRequest, Person,
                     PersonRole,
                     RosterAddition, ScoreEvent, ScoringConfig, SyncRequest, SyncState)

# re-register auth with unfold's styling (django registers the plain ones first)
admin.site.unregister(User)
admin.site.unregister(Group)


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm


@admin.register(Group)
class GroupAdmin(BaseGroupAdmin, ModelAdmin):
    pass


class ViewOnlyAdmin(ModelAdmin):
    """No add / change / delete — and so no bulk "delete selected" action, which
    would otherwise bypass the models' own save()/delete() guards."""
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Person)
class PersonAdmin(ViewOnlyAdmin):
    list_display = ("username", "name", "gitlab_id")
    search_fields = ("username", "name")


@admin.register(MergeRequest)
class MergeRequestAdmin(ViewOnlyAdmin):
    list_display = ("project", "iid", "title", "author", "state", "is_fix", "reviewed", "created_at")
    list_filter = ("project", "state", "is_fix", "reviewed")
    search_fields = ("title", "source_branch")


@admin.register(Finding)
class FindingAdmin(ViewOnlyAdmin):
    list_display = ("title", "severity", "category", "category_source", "status", "appeal_verdict",
                    "present", "mr", "created_at")
    list_filter = ("severity", "category", "category_legacy", "category_source", "status",
                   "appeal_verdict", "present")
    search_fields = ("title", "file", "body")
    list_select_related = ("mr",)


@admin.register(FindingReview)
class FindingReviewAdmin(ViewOnlyAdmin):
    list_display = ("created_at", "actor", "note", "category", "excluded", "reason")
    list_filter = ("actor", "category", "excluded")


@admin.register(FollowupReview)
class FollowupReviewAdmin(ViewOnlyAdmin):
    list_display = ("created_at", "actor", "feature_mr_id", "kind", "source_ref", "verdict", "reason")
    list_filter = ("actor", "kind", "verdict")


@admin.register(PersonRole)
class PersonRoleAdmin(ViewOnlyAdmin):
    list_display = ("created_at", "actor", "person", "role")
    list_filter = ("role",)


@admin.register(ScoringConfig)
class ScoringConfigAdmin(ViewOnlyAdmin):
    list_display = ("version", "created_at", "actor", "note")


@admin.register(SyncRequest)
class SyncRequestAdmin(ViewOnlyAdmin):
    list_display = ("id", "kind", "requested_by", "requested_at", "started_at", "finished_at",
                    "result")


@admin.register(SyncState)
class SyncStateAdmin(ViewOnlyAdmin):
    list_display = ("key", "value")


@admin.register(EmailAlias)
class EmailAliasAdmin(ViewOnlyAdmin):
    list_display = ("created_at", "actor", "email", "person")


@admin.register(RosterAddition)
class RosterAdditionAdmin(ViewOnlyAdmin):
    list_display = ("created_at", "actor", "username", "resolved_id", "error")


@admin.register(ScoreEvent)
class ScoreEventAdmin(ViewOnlyAdmin):
    list_display = ("created_at", "name", "old_score", "new_score", "old_level", "new_level",
                    "trigger", "actor")
    list_filter = ("actor",)
