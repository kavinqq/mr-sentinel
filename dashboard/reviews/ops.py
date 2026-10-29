"""本機服務狀態:mr-sentinel 和 alert-doctor 的 launchd 服務、它們依賴的東西,
以及兩個 Slack Socket listener 的開關。只讀 launchctl / log 檔,只有 SOCKETS 能被切換。"""
import json
import os
import re
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()
SENTINEL = Path(__file__).resolve().parents[2]
AGENTS = HOME / "Library" / "LaunchAgents"
UID = os.getuid()

LOG_LINES = 40
REFRESH_SECONDS = 15


@dataclass(frozen=True)
class Service:
    label: str
    project: str
    title: str
    schedule: str
    logs: tuple


SERVICES = (
    Service("com.example.mr-sentinel-bot-socket", "mr-sentinel", "Slack Socket(按鈕 / @指令)", "常駐",
            (SENTINEL / "slack_bot.log", SENTINEL / "launchd-bot-socket.err.log")),
    Service("com.example.mr-sentinel", "mr-sentinel", "poller(抓新 MR、派 review)", "每 60 秒",
            (SENTINEL / "poller.log", SENTINEL / "launchd.err.log")),
    Service("com.example.mr-sentinel-history", "mr-sentinel", "history(review 歷史與評分)", "每天 03:30",
            (SENTINEL / "history.log", SENTINEL / "launchd-history.err.log")),
    Service("tw.pocket.alert-doctor.collector", "alert-doctor", "collector(Slack Socket 收告警)", "常駐",
            (HOME / "alert-doctor/logs/collector.log",)),
    Service("tw.pocket.alert-doctor.analyzer", "alert-doctor", "analyzer(LLM 根因分析)", "常駐",
            (HOME / "alert-doctor/logs/analyzer.log",)),
    Service("tw.pocket.alert-doctor.health", "alert-doctor", "health(健康檢查)", "每 10 分鐘",
            (HOME / "alert-doctor/logs/health.log",)),
    Service("tw.pocket.alert-doctor.tuner", "alert-doctor", "tuner(基線 / 自動規則)", "每小時",
            (HOME / "alert-doctor/logs/tuner.log",)),
    Service("tw.pocket.alert-doctor.report", "alert-doctor", "report(日報)", "每天 09:00",
            (HOME / "alert-doctor/logs/report.log",)),
    Service("tw.pocket.alert-doctor.weekly", "alert-doctor", "weekly(週報)", "週一 09:30",
            (HOME / "alert-doctor/logs/weekly.log",)),
)
# one listener per Slack app: switching these is the whole point of the panel
SOCKETS = {"com.example.mr-sentinel-bot-socket", "tw.pocket.alert-doctor.collector"}
BY_LABEL = {s.label: s for s in SERVICES}

SECRET_RE = re.compile(
    r"(glpat-|xox[abpr]-|xapp-|squ_|sqa_|sqp_)[A-Za-z0-9_-]+"
    r"|((?:sonar\.)?token|SONAR_TOKEN|PRIVATE-TOKEN|password|secret)(\s*[=:]\s*)\S+",
    re.IGNORECASE)


def redact(text: str) -> str:
    def repl(m):
        return f"{m.group(1)}<redacted>" if m.group(1) else f"{m.group(2)}{m.group(3)}<redacted>"
    return SECRET_RE.sub(repl, text)


def launchctl(args: list[str]) -> tuple[int, str]:
    done = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=20)
    return done.returncode, done.stdout + done.stderr


def parse_print(text: str) -> dict:
    """Top-level `key = value` lines of `launchctl print` (nested blocks skipped)."""
    out = {}
    for line in text.splitlines():
        if line.startswith("\t") and not line.startswith("\t\t") and " = " in line:
            key, _, value = line.strip().partition(" = ")
            if value != "{":
                out.setdefault(key, value)
    return out


def parse_disabled(text: str) -> dict:
    return {m.group(1): m.group(2) in ("disabled", "true")
            for m in re.finditer(r'"([^"]+)" => (\w+)', text)}


def switch(label: str, on: bool) -> str:
    """Turn a Socket listener on or off for good: disable + bootout survives a reboot,
    a plain kill would be undone by KeepAlive. Raises ValueError for anything else."""
    if label not in SOCKETS:
        raise ValueError(f"{label} cannot be switched from the panel")
    target = f"gui/{UID}/{label}"
    if on:
        steps = [["enable", target], ["bootstrap", f"gui/{UID}", str(AGENTS / f"{label}.plist")]]
    else:
        steps = [["disable", target], ["bootout", target]]
    results = [launchctl(step) for step in steps]
    return "; ".join(f"{step[0]} rc={rc} {out.strip()}" for step, (rc, out) in zip(steps, results))


def tail(path: Path, lines: int = LOG_LINES) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 64_000))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return "(沒有這個 log)"
    return redact("\n".join(text.splitlines()[-lines:])) or "(空的)"


def service_status(service: Service, disabled: dict) -> dict:
    rc, text = launchctl(["print", f"gui/{UID}/{service.label}"])
    info = parse_print(text) if rc == 0 else {}
    mtimes = [p.stat().st_mtime for p in service.logs if p.exists()]
    return {"loaded": rc == 0, "disabled": disabled.get(service.label, False),
            "state": info.get("state", "未載入"), "pid": info.get("pid"),
            "runs": info.get("runs"), "exit": info.get("last exit code"),
            "signal": info.get("last terminating signal"),
            "log_age": time.time() - max(mtimes) if mtimes else None}


def reachable(host: str, port: int, timeout: float = 2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def checks() -> list[tuple[str, bool, str]]:
    """Things the services depend on: (name, ok, detail)."""
    out = []
    try:
        url = urllib.parse.urlparse(json.loads((SENTINEL / "config.json").read_text())["gitlab_url"])
        ok = reachable(url.hostname, url.port or 80)
        out.append(("GitLab", ok, f"{url.hostname}:{url.port}" + ("" if ok else " 連不到(在內網或 VPN 上嗎?)")))
    except (OSError, ValueError, KeyError):
        out.append(("GitLab", False, "讀不到 mr-sentinel/config.json"))
    beat = SENTINEL / ".socket-alive"
    age = time.time() - beat.stat().st_mtime if beat.exists() else None
    out.append(("mr-sentinel Socket 心跳", age is not None and age < 120,
                "沒有心跳檔" if age is None else f"{int(age)} 秒前" + ("" if age < 120 else
                                                                  ",超過 120 秒 → review 不會附按鈕")))
    try:
        with urllib.request.urlopen("http://127.0.0.1:9000/api/system/status", timeout=3) as r:
            status = json.loads(r.read()).get("status")
        scans = [p.stat().st_mtime for p in (HOME / "sonarqube/logs").glob("*.log")]
        last = time.strftime("%m/%d %H:%M", time.localtime(max(scans))) if scans else "沒有紀錄"
        out.append(("SonarQube", status == "UP", f"{status},上次掃描 {last}"))
    except (OSError, ValueError):
        out.append(("SonarQube", False, "localhost:9000 沒回應(docker 有開嗎?)"))
    return out


def _ago(seconds) -> str:
    if seconds is None:
        return "—"
    for size, unit in ((86400, "天"), (3600, "小時"), (60, "分")):
        if seconds >= size:
            return f"{int(seconds // size)} {unit}前"
    return f"{int(seconds)} 秒前"


def _badge(service: Service, st: dict) -> tuple[str, str]:
    if st["disabled"] or not st["loaded"]:
        return "off", "已關閉" if st["disabled"] else "未載入"
    if st["state"] == "running":
        return "ok", f"執行中 pid {st['pid']}"
    if st["exit"] not in (None, "0", "(never exited)"):
        return "bad", f"上次失敗 exit {st['exit']}"
    return "idle", "等下次排程" if service.schedule != "常駐" else st["state"]


def page() -> dict:
    """Template context: dependency checks and every service grouped by project."""
    _, text = launchctl(["print-disabled", f"gui/{UID}"])
    disabled = parse_disabled(text)
    groups = []
    for project in ("mr-sentinel", "alert-doctor"):
        rows = []
        for s in (s for s in SERVICES if s.project == project):
            st = service_status(s, disabled)
            kind, label = _badge(s, st)
            rows.append({"service": s, "kind": kind, "label": label, "status": st,
                         "log_ago": _ago(st["log_age"]), "switchable": s.label in SOCKETS,
                         "on": st["loaded"] and not st["disabled"],
                         "logs": [(str(p), tail(p)) for p in s.logs]})
        groups.append((project, rows))
    return {"groups": groups, "checks": checks(), "log_lines": LOG_LINES,
            "refresh": REFRESH_SECONDS}
