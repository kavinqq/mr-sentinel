"""The final gates: the last check before a change can be released.

Run once per review of an MR into a release branch: master/main (or
`target_branches`) plus the project's own default branch. Nothing here watches
branches between reviews. Two layers:
- artifact_gate: a plain rule over added lines (test markers), no model.
- release_guard: an extra model review on top of the normal one, over every
  added line in batches: test leaks first, then anything strange for a release.

The verdict is binary and fails closed: anything that could not be checked
completely (unreadable diff, missing config, a failed or malformed batch, a
change too large to cover) is a failure, never a pass. It is published as a
GitLab commit status so GitLab itself can refuse the merge.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import artifact_gate
import release_guard

STATUS_NAME = "mr-sentinel/release-gate"
GUARD_WORKERS = 3
DEFAULT_TARGET_BRANCHES = ("master", "main")

log = logging.getLogger("mr_sentinel.final_gates")


@dataclass
class Outcome:
    rule: list | None            # None: the diff could not be read
    guard: list | None           # None: the guard did not complete
    problem: str = ""

    @property
    def findings(self) -> list:
        return combine(self.rule or [], self.guard or [])

    @property
    def ok(self) -> bool:
        return self.rule is not None and self.guard is not None

    @property
    def passed(self) -> bool:
        return self.ok and not self.findings

    def record(self, head_sha: str, target_branch: str | None) -> dict:
        """What review_meta.json keeps for _release_blocker. The same head into a
        different branch is a different diff, so the verdict is bound to both."""
        return {"head_sha": head_sha, "target_branch": target_branch,
                "ok": self.ok, "leaks": len(self.findings)}


def applies(target_branch: str | None, review_cfg: dict, default_branch: str | None) -> bool:
    """Whether an MR into `target_branch` is a release and must pass the final gates.

    `default_branch` is the project's main branch per GitLab; None means it could
    not be looked up, and an unknown main branch gates every MR (fail closed)."""
    if default_branch is None:
        return True
    targets = review_cfg.get("release_guard", {}).get("target_branches") or DEFAULT_TARGET_BRANCHES
    release = {t.lower() for t in targets} | {default_branch.lower()}
    return (target_branch or "").lower() in release


def combine(rule: list, guard: list) -> list:
    """Rule hits first; a guard finding on a line the rule already reported is a duplicate."""
    covered = {(f["file"], n) for f in rule for n in f["hit_lines"]}
    return rule + [f for f in guard if (f.get("file"), f.get("line")) not in covered]


# an MR outside the release branches: nothing to gate, nothing found
NOT_APPLICABLE = Outcome(rule=[], guard=[])


def _guard_batch(engine, work: Path, batch: dict, wt, review_cfg: dict) -> list | None:
    i = batch["batch"]["index"]
    ctx_path, out_path = work / f"guard_context_{i}.json", work / f"release_guard_{i}.json"
    ctx_path.write_text(json.dumps(batch, ensure_ascii=False, indent=1))
    if out_path.exists():
        out_path.unlink()
    try:
        if engine.run_guard(work, ctx_path, out_path, wt, review_cfg) != 0:
            return None
        return release_guard.normalize(json.loads(out_path.read_text()), batch["files"],
                                       review_cfg.get("language", "en"))
    except (OSError, ValueError):
        return None


def run(engine, work: Path, ctx: dict, local: str | None, wt, review_cfg: dict) -> Outcome:
    language = review_cfg.get("language", "en")
    guard_cfg = review_cfg.get("release_guard", {})
    diff_refs = ctx.get("diff_refs") or {}

    # no API fallback: ctx["changes"] is filtered for the normal review and lacks
    # build output, which ships; without the clone's diff the gate fails closed
    changes = artifact_gate.collect(local, diff_refs, ctx.get("target_branch") or "", None)
    if changes is None:
        return Outcome(rule=None, guard=None, problem="拿不到完整 diff")
    exempt = artifact_gate.exempt_test_files(local, diff_refs.get("head_sha", ""),
                                             [c["new_path"] for c in changes if c.get("new_path")])
    rule = (artifact_gate.scan(changes, language, exempt)
            + release_guard.minified_host_findings(changes, guard_cfg.get("test_hosts") or [],
                                                   language))

    problem = release_guard.config_problem(guard_cfg)
    if problem:
        return Outcome(rule=rule, guard=None, problem=problem)
    batches = release_guard.build_batches(
        ctx, changes, guard_cfg,
        guard_cfg.get("max_diff_lines", release_guard.DEFAULT_MAX_LINES),
        guard_cfg.get("max_batches", release_guard.DEFAULT_MAX_BATCHES))
    if batches is None:
        return Outcome(rule=rule, guard=None,
                       problem="變更太大,release guard 無法完整看完,需要人工把關")

    with ThreadPoolExecutor(max_workers=GUARD_WORKERS) as pool:
        results = list(pool.map(lambda b: _guard_batch(engine, work, b, wt, review_cfg), batches))
    failed = [b["batch"]["index"] for b, r in zip(batches, results) if r is None]
    if failed:
        log.error("release guard failed on batch(es) %s of %s", failed, len(batches))
        return Outcome(rule=rule, guard=None,
                       problem=f"release guard 第 {failed} 批(共 {len(batches)} 批)沒有完成")
    return Outcome(rule=rule, guard=[f for r in results for f in r])


def status(outcome: Outcome) -> tuple[str, str]:
    """(GitLab commit status state, description)."""
    if not outcome.ok:
        return "failed", f"review 沒跑完,請重審:{outcome.problem}"[:255]
    if outcome.findings:
        return "failed", f"review 有 {len(outcome.findings)} 則留言要處理"
    return "success", "review 通過"
