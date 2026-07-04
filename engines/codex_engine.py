"""Codex CLI engine (experimental): two `codex exec` passes + mechanical finalize.

Codex has no subagent mechanism, so the adversarial vetting becomes:
  pass 1  scan     -> candidate findings (prompts/codex_scan.md)
  pass 2  skeptic  -> keep/drop verdicts (prompts/skeptic.md persona, shared
                      with the claude engine so the vetting rules stay identical)
  python  finalize -> apply verdicts, sort, write final_findings.json
Applying verdicts is mechanical, so no third AI call is needed.

Safety: `--sandbox read-only` keeps the AI from writing anything; replies come
back via `--output-last-message` (written by the CLI outside the sandbox).

Note: the engine invokes plain `codex` from PATH. Shell aliases/functions
(e.g. a wrapper adding --add-dir) do not propagate to subprocesses.
"""
import json
import subprocess
from pathlib import Path

from engines.claude_engine import language_name, render_prompt

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def parse_json_reply(text: str) -> dict:
    """Models sometimes wrap JSON in fences or prose; extract the object."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        return json.loads(text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.index("{"), text.rindex("}") + 1
        return json.loads(text[start:end])


def apply_verdicts(candidates: list[dict], verdicts: list[dict]) -> list[dict]:
    """Keep only explicitly kept findings; a missing verdict counts as a drop."""
    kept = []
    by_index = {v.get("index"): v for v in verdicts}
    for i, cand in enumerate(candidates):
        v = by_index.get(i)
        if not v or v.get("verdict") != "keep":
            continue
        if v.get("severity") in ("high", "medium", "low"):
            cand = {**cand, "severity": v["severity"]}
        kept.append(cand)
    return kept


def build_cmd(prompt: str, work_dir: str, last_message_file: str, model: str) -> list[str]:
    cmd = ["codex", "exec", prompt,
           "--sandbox", "read-only",
           "--cd", str(work_dir),
           "--output-last-message", str(last_message_file)]
    if model:
        cmd += ["-m", model]
    return cmd


def label(review_cfg: dict) -> str:
    c = review_cfg["codex"]
    scan = c.get("model") or "codex default"
    vet = c.get("skeptic_model") or scan
    return f"scanned by {scan}, vetted by {vet} (codex)"


def _exec_pass(prompt: str, work_dir: Path, model: str, timeout: int) -> str:
    """Run one codex exec pass and return the model's last message."""
    last = work_dir / ".codex_last_message.txt"
    if last.exists():
        last.unlink()
    with open(work_dir / "codex.log", "a") as logf:
        proc = subprocess.run(build_cmd(prompt, str(work_dir), str(last), model),
                              cwd=str(work_dir), stdout=logf, stderr=subprocess.STDOUT,
                              timeout=timeout)
    if proc.returncode != 0 or not last.exists():
        raise RuntimeError(f"codex exec failed (rc={proc.returncode})")
    return last.read_text()


def run_review(work_dir: Path, context_file: Path, output_file: Path,
               repo_dir, review_cfg: dict) -> int:
    codex_cfg = review_cfg["codex"]
    language = review_cfg.get("language", "en")
    timeout = review_cfg["review_timeout_seconds"]
    ctx = json.loads(context_file.read_text())

    try:
        # pass 1: scan
        scan_tpl = (PROMPTS_DIR / "codex_scan.md").read_text()
        scan_prompt = render_prompt(scan_tpl, language, str(repo_dir),
                                    context_file.name, output_file.name)
        candidates = parse_json_reply(
            _exec_pass(scan_prompt, work_dir, codex_cfg.get("model", ""), timeout)
        ).get("findings", [])

        # pass 2: adversarial vetting (skipped when there is nothing to vet)
        if candidates:
            skeptic_tpl = (PROMPTS_DIR / "skeptic.md").read_text()
            persona = render_prompt(skeptic_tpl, language, str(repo_dir),
                                    context_file.name, output_file.name)
            diffs = {c.get("new_path"): c.get("diff") for c in ctx.get("changes", [])}
            payload = [{"index": i, "finding": f, "hunk": diffs.get(f.get("file"), "")}
                       for i, f in enumerate(candidates)]
            skeptic_prompt = (persona + "\n\n## Candidate findings (with their hunks)\n"
                              + json.dumps(payload, ensure_ascii=False))
            verdicts = parse_json_reply(
                _exec_pass(skeptic_prompt, work_dir,
                           codex_cfg.get("skeptic_model") or codex_cfg.get("model", ""),
                           timeout)
            ).get("verdicts", [])
            findings = apply_verdicts(candidates, verdicts)
        else:
            findings = []

        # finalize: mechanical — no third AI call
        import review_common
        output_file.write_text(json.dumps({
            "mr": {"project": ctx.get("project"), "iid": ctx.get("iid"),
                   "diff_refs": ctx.get("diff_refs", {})},
            "findings": review_common.sort_findings(findings),
        }, ensure_ascii=False, indent=1))
        return 0
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return 1
