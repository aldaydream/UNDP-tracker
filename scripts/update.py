#!/usr/bin/env python3
"""AMF 2026 participants tracker: snapshot + diff.

Downloads the public attendee list of the IMF / World Bank Annual Meetings
(the same feed IMF Connect uses), compares it with the previous state and
records who appeared in the list and who disappeared from it.

Outputs (stdlib only, no dependencies):
  data/participants.json  everyone ever seen: first_seen (f) / removed_at (r).
                          Rewritten only when something changed.
  data/status.json        result of the latest check. Rewritten on every run.

Emails, bios and profile links from the source are NOT stored: the site only
needs names, titles, organisations, countries and categories.

Env overrides (for tests): SOURCE_URL, SOURCE_FILE, DATA_DIR.
"""
import hashlib
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SOURCE_URL = os.environ.get(
    "SOURCE_URL",
    "https://www.imfconnect.org/content/dam/imf/AMAttendeeList/AMAttendeeList.json",
)
SOURCE_FILE = os.environ.get("SOURCE_FILE")
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
PEOPLE_FILE = DATA_DIR / "participants.json"
STATUS_FILE = DATA_DIR / "status.json"

# If the fresh list is much shorter than the current one, the source is most
# likely broken (partial upload, error page). Stop instead of "removing" people.
MIN_RATIO = 0.7
USER_AGENT = "Mozilla/5.0 (compatible; amf-participants-tracker/1.0)"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean(value):
    return " ".join(str(value or "").split())


def fetch():
    """Return (parsed_json, last_modified_header)."""
    if SOURCE_FILE:
        return json.loads(Path(SOURCE_FILE).read_text("utf-8")), None
    request = urllib.request.Request(
        SOURCE_URL, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                body = response.read().decode("utf-8")
                return json.loads(body), response.headers.get("Last-Modified")
        except Exception as exc:  # network error, HTML error page, bad JSON
            error = exc
            time.sleep(15 * (attempt + 1))
    sys.exit(f"ERROR: could not load {SOURCE_URL}: {error}")


def person_id(item):
    """Stable short id. Email is unique in the source; it is hashed and dropped."""
    basis = clean(item.get("email")).lower()
    if not basis:
        keys = ("firstName", "lastName", "organization", "country", "categoryId")
        basis = "|".join(clean(item.get(k)).lower() for k in keys)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def normalize(raw):
    groups = raw.get("amAttendees") if isinstance(raw, dict) else None
    if not isinstance(groups, dict):
        sys.exit("ERROR: unexpected source format (no 'amAttendees')")
    people = {}
    for group, items in groups.items():
        if group == "ALL" or not isinstance(items, list):
            continue
        for item in items:
            pid = person_id(item)
            areas = (clean(x) for x in str(item.get("areaOfInterest") or "").split("||"))
            people[pid] = {
                "id": pid,
                "n": clean(" ".join([item.get("firstName", ""), item.get("middleName", ""), item.get("lastName", "")])),
                "l": clean(item.get("lastName")),
                "t": clean(item.get("title")),
                "o": clean(item.get("organization")),
                "c": clean(item.get("country")),
                "k": clean(item.get("category")) or group,
                "g": clean(item.get("guestCategory")),
                "a": " · ".join(a for a in areas if a),
            }
    return people


def load_state():
    if not PEOPLE_FILE.exists():
        return None
    return json.loads(PEOPLE_FILE.read_text("utf-8"))


def dump_state(state):
    """One person per line: git diffs then show exactly who was added/removed."""
    head = {k: state[k] for k in ("meta", "runs")}
    lines = [json.dumps(p, ensure_ascii=False, separators=(",", ":")) for p in state["people"]]
    text = json.dumps(head, ensure_ascii=False, separators=(",", ":"))[:-1]
    text += ',"people":[\n' + ",\n".join(lines) + "\n]}\n"
    return text


def main():
    raw, last_modified = fetch()
    current = normalize(raw)
    if not current:
        sys.exit("ERROR: source list is empty, nothing changed")

    ts = now_iso()
    state = load_state()
    added = removed = 0

    if state is None:  # first run = baseline, nobody is "new"
        people = {pid: {**p, "f": ts, "r": None} for pid, p in current.items()}
        state = {"meta": {"source": SOURCE_URL, "baseline_at": ts}, "runs": []}
        state["runs"].append({"at": ts, "total": len(people), "added": 0, "removed": 0, "baseline": True})
    else:
        people = {p["id"]: p for p in state["people"]}
        active = sum(1 for p in people.values() if not p.get("r"))
        if active and len(current) < active * MIN_RATIO:
            sys.exit(f"ERROR: source has {len(current)} people vs {active} tracked; looks broken, skipped")
        for pid, fresh in current.items():
            old = people.get(pid)
            if old is None or old.get("r"):  # new, or came back after removal
                people[pid] = {**fresh, "f": ts, "r": None}
                added += 1
            else:
                old.update(fresh)  # keep title/organisation up to date
        for pid, old in people.items():
            if not old.get("r") and pid not in current:
                old["r"] = ts
                removed += 1
        if added or removed:
            state["runs"].append({"at": ts, "total": len(current), "added": added, "removed": removed})

    state["people"] = sorted(people.values(), key=lambda p: (p["l"].lower(), p["n"].lower(), p["id"]))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new_text = dump_state(state)
    old_text = PEOPLE_FILE.read_text("utf-8") if PEOPLE_FILE.exists() else ""
    if new_text != old_text:
        PEOPLE_FILE.write_text(new_text, "utf-8")

    status = {
        "checked_at": ts,
        "total": len(current),
        "added": added,
        "removed": removed,
        "source_last_modified": last_modified,
    }
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=1) + "\n", "utf-8")
    print(f"{ts}: {len(current)} in list, +{added} / -{removed}")


if __name__ == "__main__":
    main()
