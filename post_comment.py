"""Post findings as MR comments. The AI engine never posts anything itself.

Inline discussions (with position) first; positions the API rejects (400/422)
and findings without a line number fall back to plain notes.
"""
import argparse
import json
import urllib.error

import gitlab_client
import review_common


def _note_prefix(finding: dict) -> str:
    if finding.get("file") and finding.get("line") is not None:
        return f"`{finding['file']}:{finding['line']}`\n\n"
    if finding.get("file"):
        return f"`{finding['file']}`\n\n"
    return ""


def post_findings(base: str, token: str, project, iid, findings: list, diff_refs: dict,
                  signature: str = review_common.DEFAULT_SIGNATURE) -> int:
    posted = 0
    for f in review_common.sort_findings(findings):
        body = review_common.format_comment_body(f, signature)
        position = review_common.build_position(f, diff_refs)
        if position is not None:
            try:
                gitlab_client.post_discussion(base, token, project, iid, body, position)
                posted += 1
                continue
            except urllib.error.HTTPError as exc:
                if exc.code not in (400, 422):
                    raise
                # position rejected -> fall through to a plain note
        gitlab_client.post_note(base, token, project, iid, _note_prefix(f) + body)
        posted += 1
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
