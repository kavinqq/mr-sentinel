"""Fetch MR diff + metadata, drop noise files, and produce the context the AI engine reads."""
import argparse
import json
from pathlib import Path

import gitlab_client
import review_common


def build_context(base: str, token: str, project, iid) -> dict:
    raw = gitlab_client.get_mr_changes(base, token, project, iid)
    changes = review_common.filter_noise_changes(raw.get("changes", []))
    files, lines = review_common.diff_stats(changes)
    return {
        "project": project,
        "iid": iid,
        "title": raw.get("title"),
        "web_url": raw.get("web_url"),
        "source_branch": raw.get("source_branch"),
        "target_branch": raw.get("target_branch"),
        "diff_refs": raw["diff_refs"],
        "changes": [
            {"new_path": c.get("new_path"), "old_path": c.get("old_path"), "diff": c.get("diff")}
            for c in changes
        ],
        "stats": {"files": files, "lines": lines},
    }


def write_context(path, ctx: dict) -> None:
    Path(path).write_text(json.dumps(ctx, ensure_ascii=False, indent=1))


def main() -> int:
    import sentinel_config

    ap = argparse.ArgumentParser(description="Fetch MR context for the AI engine")
    ap.add_argument("--project", required=True, help="project id or full path (group/name)")
    ap.add_argument("--iid", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    cfg = sentinel_config.load_config()
    ctx = build_context(cfg["gitlab_url"], cfg["gitlab_token"], args.project, args.iid)
    write_context(args.out, ctx)
    print(f"context written to {args.out} (files={ctx['stats']['files']}, lines={ctx['stats']['lines']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
