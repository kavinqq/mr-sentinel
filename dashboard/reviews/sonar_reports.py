"""SonarQube fix-list reports written by ~/sonarqube/export_report.py, served from the
dashboard so RDs open them here instead of through a separate port. Read from disk on
every request: a regenerated report shows up without restarting anything."""
import re
from pathlib import Path

REPORTS_DIR = Path.home() / "sonarqube" / "reports"
NAME_RE = re.compile(r"[A-Za-z0-9_.-]+\.html")
INDEX = "index.html"


def read(name: str, directory: Path = REPORTS_DIR) -> bytes | None:
    if not NAME_RE.fullmatch(name):
        return None
    path = (directory / name).resolve()
    if path.parent != directory.resolve() or not path.is_file():
        return None
    return path.read_bytes()
