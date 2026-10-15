"""Post findings as MR comments. The AI engine never posts anything itself.

Inline discussions (with position) first; positions the API rejects (400/422)
and findings without a line number fall back to plain notes.
"""
import argparse
import json
import logging
import urllib.error

import gitlab_client
import review_common

log = logging.getLogger("mr_sentinel.post_comment")


def _note_prefix(finding: dict) -> str:
    if finding.get("file") and finding.get("line") is not None:
        return f"`{finding['file']}:{finding['line']}`\n\n"
    if finding.get("file"):
        return f"`{finding['file']}`\n\n"
    return ""


def _landed(base: str, token: str, project, iid, body: str) -> bool:
    """Did a write that answered 5xx / timed out actually create the note?

    GitLab can create the note and then fail in a post-save hook (seen: 500 one
    second after the comment appeared), so an error response alone proves nothing.
    """
    try:
        discussions = gitlab_client.list_discussions(base, token, project, iid)
    except Exception:
        return False
    return any((n.get("body") or "").strip() == body.strip()
               for d in discussions for n in d.get("notes") or [])


def post_findings(base: str, token: str, project, iid, findings: list, diff_refs: dict,
                  signature: str = review_common.DEFAULT_SIGNATURE) -> int:
    """Post every finding and return how many are on the MR.

    A finding whose write fails for a reason other than "position rejected" is
    checked against GitLab and then skipped, never raised: one flaky response must
    not discard the rest of the review. The caller compares the return value with
    len(findings) to tell the user about the gap.
    """
    posted = 0
    for f in review_common.sort_findings(findings):
        body = review_common.format_comment_body(f, signature)
        position = review_common.build_position(f, diff_refs)
        attempted = body
        try:
            if position is not None:
                try:
                    gitlab_client.post_discussion(base, token, project, iid, body, position)
                    posted += 1
                    continue
                except urllib.error.HTTPError as exc:
                    if exc.code not in (400, 422):
                        raise
                    # position rejected -> fall through to a plain note
            attempted = _note_prefix(f) + body
            gitlab_client.post_note(base, token, project, iid, attempted)
            posted += 1
        except (urllib.error.URLError, OSError) as exc:
            if _landed(base, token, project, iid, attempted):
                log.warning("finding %r: %s, but the comment is on the MR", f.get("title"), exc)
                posted += 1
            else:
                log.error("finding %r not posted: %s", f.get("title"), exc)
    return posted


def main() -> int:
    import sentinel_config

    ap = argparse.ArgumentParser(description="Post findings JSON as MR comments")
    ap.add_argument("--findings", required=True)
    args = ap.parse_args()
    cfg = sentinel_config.load_config()
    data = json.loads(open(args.findings).read())
    mr = data["mr"]
    n = post_findings(cfg["gitlab_url"], cfg["gitlab_token"],
                      mr["project"], mr["iid"], data.get("findings", []), mr["diff_refs"])
    print(f"posted {n} comment(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
