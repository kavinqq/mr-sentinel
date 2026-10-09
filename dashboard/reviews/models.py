"""Django views of sentinel.db. Every model is managed=False: the schema belongs
to the stdlib `history` package (history/db.py), and Django never migrates it.

Two kinds of table, enforced here rather than trusted to the views:
- raw (Person, MergeRequest, Finding, Followup, SyncState): written only by the
  core from GitLab — read-only here, saving one raises;
- human (FindingReview, ScoringConfig, SyncRequest): append-only — rows are
  created, never edited or deleted, so every number stays traceable.

Timestamps are TEXT in UTC ('YYYY-MM-DDTHH:MM:SSZ'), exactly as the core stores them.
"""
from django.core.exceptions import PermissionDenied
from django.db import models


class ReadOnlyModel(models.Model):
    class Meta:
        abstract = True
        managed = False

    def save(self, *args, **kwargs):
        raise PermissionDenied(f"{type(self).__name__} is written by the history sync only")

    def delete(self, *args, **kwargs):
        raise PermissionDenied(f"{type(self).__name__} is written by the history sync only")


class AppendOnlyModel(models.Model):
    class Meta:
        abstract = True
        managed = False

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionDenied(f"{type(self).__name__} rows are append-only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied(f"{type(self).__name__} rows are append-only")


class Person(ReadOnlyModel):
    gitlab_id = models.IntegerField(primary_key=True)
    username = models.TextField()
    name = models.TextField(null=True)

    class Meta(ReadOnlyModel.Meta):
        db_table = "people"
        ordering = ["username"]

    def __str__(self):
        return f"{self.name or self.username} ({self.username})"


class MergeRequest(ReadOnlyModel):
    mr_id = models.IntegerField(primary_key=True)
    project = models.TextField()
    iid = models.IntegerField()
    author = models.ForeignKey(Person, models.DO_NOTHING, db_column="author_id", null=True,
                               related_name="merge_requests")
    title = models.TextField(null=True)
    state = models.TextField(null=True)
    source_branch = models.TextField(null=True)
    target_branch = models.TextField(null=True)
    web_url = models.TextField(null=True)
    created_at = models.TextField(null=True)
    merged_at = models.TextField(null=True)
    updated_at = models.TextField(null=True)
    is_fix = models.IntegerField(default=0)
    reviewed = models.IntegerField(default=0)
    files_synced = models.IntegerField(default=0)
    synced_at = models.TextField(null=True)

    class Meta(ReadOnlyModel.Meta):
        db_table = "mrs"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.project}!{self.iid}"


class Finding(ReadOnlyModel):
    note_id = models.IntegerField(primary_key=True)
    discussion_id = models.TextField(null=True)
    mr = models.ForeignKey(MergeRequest, models.DO_NOTHING, db_column="mr_id",
                           related_name="findings")
    severity = models.TextField(null=True)
    title = models.TextField(null=True)
    category = models.TextField(null=True)
    category_source = models.TextField(null=True)
    file = models.TextField(null=True)
    line = models.IntegerField(null=True)
    body = models.TextField(null=True)
    created_at = models.TextField(null=True)
    status = models.TextField(null=True)
    appeal_verdict = models.TextField(null=True)
    present = models.IntegerField(default=1)
    last_seen_at = models.TextField(null=True)

    class Meta(ReadOnlyModel.Meta):
        db_table = "findings"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title or f"note {self.note_id}"

    @property
    def gitlab_url(self) -> str | None:
        return f"{self.mr.web_url}#note_{self.note_id}" if self.mr.web_url else None


class Followup(ReadOnlyModel):
    pk = models.CompositePrimaryKey("feature_mr", "kind", "source_ref")
    feature_mr = models.ForeignKey(MergeRequest, models.DO_NOTHING, db_column="feature_mr_id",
                                   related_name="followups")
    kind = models.TextField()
    source_ref = models.TextField()
    file = models.TextField(null=True)
    days_after = models.FloatField(null=True)

    class Meta(ReadOnlyModel.Meta):
        db_table = "followups"


class SyncState(ReadOnlyModel):
    key = models.TextField(primary_key=True)
    value = models.TextField(null=True)

    class Meta(ReadOnlyModel.Meta):
        db_table = "sync_state"


class PersonRole(AppendOnlyModel):
    """Team role per person; the latest row wins. A 'lead' is never ranked."""
    id = models.AutoField(primary_key=True)
    person = models.ForeignKey(Person, models.DO_NOTHING, db_column="gitlab_id",
                               related_name="roles")
    role = models.TextField()
    actor = models.TextField()
    created_at = models.TextField()

    class Meta(AppendOnlyModel.Meta):
        db_table = "person_roles"
        ordering = ["-created_at", "-id"]


class FindingReview(AppendOnlyModel):
    """A human override on one finding. The latest non-null value per field wins
    (history.score.effective_findings); the raw finding is never touched."""
    id = models.AutoField(primary_key=True)
    note = models.ForeignKey(Finding, models.DO_NOTHING, db_column="note_id",
                             related_name="reviews")
    category = models.TextField(null=True, blank=True)
    excluded = models.IntegerField(null=True, blank=True)
    reason = models.TextField(null=True, blank=True)
    actor = models.TextField()
    created_at = models.TextField()

    class Meta(AppendOnlyModel.Meta):
        db_table = "finding_reviews"
        ordering = ["-created_at", "-id"]


class ScoringConfig(AppendOnlyModel):
    """Each change is a new version; the highest version is in force."""
    version = models.IntegerField(primary_key=True)
    config = models.TextField()
    note = models.TextField(null=True, blank=True)
    actor = models.TextField()
    created_at = models.TextField()

    class Meta(AppendOnlyModel.Meta):
        db_table = "scoring_configs"
        ordering = ["-version"]

    @classmethod
    def lower_is_better_versions(cls) -> set[int]:
        """Versions from before the 10-point scale (no deduction_per_weight)."""
        return {c.version for c in cls.objects.all() if "deduction_per_weight" not in c.config}


class SyncRequest(AppendOnlyModel):
    """Queued here, executed by `python3 -m history run` — never inside a web request."""
    id = models.AutoField(primary_key=True)
    kind = models.TextField()
    requested_by = models.TextField()
    requested_at = models.TextField()
    started_at = models.TextField(null=True)
    finished_at = models.TextField(null=True)
    result = models.TextField(null=True)

    class Meta(AppendOnlyModel.Meta):
        db_table = "sync_requests"
        ordering = ["-id"]


class EmailAlias(AppendOnlyModel):
    """A commit email confirmed as one person (person NULL = ignore it); latest wins."""
    id = models.AutoField(primary_key=True)
    email = models.TextField()
    person = models.ForeignKey(Person, models.DO_NOTHING, db_column="gitlab_id", null=True,
                               related_name="email_aliases")
    actor = models.TextField()
    created_at = models.TextField()

    class Meta(AppendOnlyModel.Meta):
        db_table = "email_aliases"
        ordering = ["-created_at", "-id"]


class RosterAddition(AppendOnlyModel):
    """A member added by GitLab username; the sync resolves it (resolved_id / error)."""
    id = models.AutoField(primary_key=True)
    username = models.TextField()
    actor = models.TextField()
    created_at = models.TextField()
    resolved_id = models.IntegerField(null=True)
    error = models.TextField(null=True)

    class Meta(AppendOnlyModel.Meta):
        db_table = "roster_additions"
        ordering = ["-created_at", "-id"]


class ScoreEvent(ReadOnlyModel):
    """One score / level change, written by history.snapshot.record."""
    id = models.AutoField(primary_key=True)
    created_at = models.TextField()
    gitlab_id = models.IntegerField()
    name = models.TextField(null=True)
    old_score = models.FloatField(null=True)
    new_score = models.FloatField(null=True)
    old_level = models.TextField(null=True)
    new_level = models.TextField(null=True)
    old_findings = models.IntegerField(null=True)
    new_findings = models.IntegerField(null=True)
    formula_version = models.IntegerField(null=True)
    trigger = models.TextField()
    actor = models.TextField()

    class Meta(ReadOnlyModel.Meta):
        db_table = "score_events"
        ordering = ["-id"]

    @property
    def rescaled(self):
        """No meaningful +/− : the 10-point switch itself (old and new on different
        scales), or an event from before it (lower was better then)."""
        from history.db import SCALE_CHANGE_NOTE
        return self.trigger.startswith(SCALE_CHANGE_NOTE) or \
            self.formula_version in ScoringConfig.lower_is_better_versions()

    @property
    def delta(self):
        if self.old_score is None or self.new_score is None or self.rescaled:
            return None
        return round(self.new_score - self.old_score, 2)
