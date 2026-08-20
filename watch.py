#!/usr/bin/env python3
"""
Veille offres quant — poll les job boards ATS des fonds systematiques.
Aucune dependance externe (stdlib uniquement).
"""

import json
import os
import re
import urllib.request
import urllib.error
from pathlib import Path

# --- Configuration -----------------------------------------------------------

CONFIG_FILE = Path(__file__).parent / "boards_config.csv"


def load_boards():
    """Lit boards_config.csv : name,kind,a,b,c
    greenhouse -> a = token
    workday    -> a = instance (wd103), b = tenant, c = site
    """
    import csv
    gh, wd = {}, []
    with open(CONFIG_FILE, newline="") as f:
        for r in csv.DictReader(f):
            if r["kind"] == "greenhouse":
                gh[r["name"]] = r["a"]
            elif r["kind"] == "workday":
                wd.append((r["name"], r["a"], r["b"], r["c"]))
    return gh, wd


# Un titre doit matcher AU MOINS un motif pour etre retenu.
KEYWORDS = [
    r"intern",
    r"internship",
    r"stage",
    r"graduate",
    r"campus",
    r"quantitative research",
    r"quant research",
    r"quantitative develop",
    r"quant develop",
    r"researcher",
]

# Si non vide : le lieu doit matcher un de ces motifs. Vide = pas de filtre.
LOCATIONS = [
    r"london",
    r"paris",
    r"france",
    r"united kingdom",
    r"amsterdam",
    r"geneva",
    r"zurich",
    r"remote",
]

SEEN_FILE = Path(__file__).parent / "seen.json"
UA = {"User-Agent": "quant-watch/1.0 (personal job alert script)"}

# --- Fetchers ----------------------------------------------------------------


def _get_json(url, data=None):
    req = urllib.request.Request(url, headers=dict(UA))
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_greenhouse(company, token):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    payload = _get_json(url)
    out = []
    for j in payload.get("jobs", []):
        out.append(
            {
                "id": f"gh:{token}:{j['id']}",
                "company": company,
                "title": j.get("title", ""),
                "location": (j.get("location") or {}).get("name", ""),
                "url": j.get("absolute_url", ""),
            }
        )
    return out


def fetch_workday(company, instance, tenant, site):
    url = (
        f"https://{tenant}.{instance}.myworkdayjobs.com"
        f"/wday/cxs/{tenant}/{site}/jobs"
    )
    out = []
    offset = 0
    while offset < 200:
        payload = _get_json(
            url, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""}
        )
        posts = payload.get("jobPostings", [])
        if not posts:
            break
        for j in posts:
            path = j.get("externalPath", "")
            out.append(
                {
                    "id": f"wd:{tenant}:{path}",
                    "company": company,
                    "title": j.get("title", ""),
                    "location": j.get("locationsText", ""),
                    "url": f"https://{tenant}.{instance}.myworkdayjobs.com"
                    f"/{site}{path}",
                }
            )
        offset += 20
    return out


# --- Filtrage ----------------------------------------------------------------


def matches(job):
    title = job["title"].lower()
    if not any(re.search(p, title) for p in KEYWORDS):
        return False
    if LOCATIONS:
        loc = job["location"].lower()
        if loc and not any(re.search(p, loc) for p in LOCATIONS):
            return False
    return True


# --- Main --------------------------------------------------------------------


def main():
    jobs = []
    errors = []

    GREENHOUSE, WORKDAY = load_boards()
    print(f"{len(GREENHOUSE)} boards Greenhouse + {len(WORKDAY)} Workday")

    for company, token in GREENHOUSE.items():
        try:
            jobs.extend(fetch_greenhouse(company, token))
        except Exception as e:
            errors.append(f"{company}: {e}")

    for company, instance, tenant, site in WORKDAY:
        try:
            jobs.extend(fetch_workday(company, instance, tenant, site))
        except Exception as e:
            errors.append(f"{company}: {e}")

    relevant = [j for j in jobs if matches(j)]

    seen = set()
    if SEEN_FILE.exists():
        seen = set(json.loads(SEEN_FILE.read_text()))

    new = [j for j in relevant if j["id"] not in seen]

    # Premier run : on enregistre tout sans alerter, sinon 80 notifications.
    first_run = not SEEN_FILE.exists()

    SEEN_FILE.write_text(
        json.dumps(sorted(seen | {j["id"] for j in relevant}), indent=1)
    )

    print(f"{len(jobs)} offres recuperees, {len(relevant)} pertinentes, {len(new)} nouvelles")
    for e in errors:
        print(f"  [erreur] {e}")

    if first_run or not new:
        return

    lines = []
    for j in sorted(new, key=lambda x: x["company"]):
        lines.append(f"- **{j['company']}** — {j['title']}")
        lines.append(f"  {j['location'] or 'lieu non precise'} — {j['url']}")
    body = "\n".join(lines)

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        Path("new_jobs.md").write_text(body)
        with open(out, "a") as f:
            f.write(f"has_new=true\n")
            f.write(f"count={len(new)}\n")
    else:
        print("\n" + body)


if __name__ == "__main__":
    main()
