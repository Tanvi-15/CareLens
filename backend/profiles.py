"""Patient profiles stored in a local JSON file on this device (never uploaded anywhere).

Shape: {profile_id: {id, name, age, sex, medications, conditions, reports: {report_id: report}}}
report: {id, date, filename, text, analysis, stats}
"""

import json
import os
import threading
import uuid
from pathlib import Path

DATA_FILE = Path(os.getenv("CARELENS_DATA", Path(__file__).resolve().parent.parent / "data" / "profiles.json"))
_lock = threading.Lock()


def load() -> dict:
    if not DATA_FILE.exists():
        return {}
    return json.loads(DATA_FILE.read_text())


def save(profiles: dict) -> None:
    with _lock:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(profiles, indent=1))
        tmp.replace(DATA_FILE)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def summary(p: dict) -> dict:
    return {k: p.get(k) for k in ("id", "name", "age", "sex", "medications", "conditions")} | {
        "report_count": len(p.get("reports", {}))
    }


def find_report(profiles: dict, report_id: str) -> tuple[dict | None, dict | None]:
    for p in profiles.values():
        if report_id in p.get("reports", {}):
            return p, p["reports"][report_id]
    return None, None


def past_reports(profile: dict, current: dict) -> list[dict]:
    """Other reports of this patient dated on/before the current one, oldest first."""
    cur = current.get("date") or "9999"
    rows = [
        {"date": r.get("date") or "unknown date", "results": r["analysis"].get("results", [])}
        for r in profile.get("reports", {}).values()
        if r["id"] != current["id"] and (r.get("date") or "0000") <= cur
    ]
    return sorted(rows, key=lambda r: r["date"])


def describe(p: dict) -> str:
    parts = [p.get("name") or "Patient"]
    if p.get("age"):
        parts.append(f"age {p['age']}")
    if p.get("sex"):
        parts.append(p["sex"])
    if p.get("conditions"):
        parts.append("known conditions: " + ", ".join(p["conditions"]))
    return ", ".join(parts)
