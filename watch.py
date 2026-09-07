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


# Le titre doit matcher UN motif de niveau ET UN motif de metier.
LEVEL = [
    r"\bintern\b", r"\binterns\b", r"internship", r"\bstage\b", r"\bstagiaire",
    r"\bgraduate\b", r"\bcampus\b", r"new grad", r"\bjunior\b",
    r"\bplacement\b", r"\btrainee\b", r"apprentice", r"\bphd\b",
    r"\bstudent\b", r"\bentry.level\b", r"\bsummer\b", r"\bvie\b",
]

ROLE = [
    r"quant", r"research", r"trading", r"\btrader\b", r"\bstrat\b",
    r"developer", r"engineer", r"software", r"\bdata\b", r"machine learning",
    r"\bml\b", r"python", r"c\+\+", r"modell?ing", r"algorithm",
]

# Rejet immediat si un de ces motifs apparait.
EXCLUDE = [
    r"\binternal\b", r"\binternational\b", r"\bsales\b", r"marketing",
    r"recruit", r"\bhr\b", r"human resources", r"\blegal\b",
    r"customer", r"account manager", r"business development",
    r"\bdesign\b", r"\bux\b", r"communicat", r"\baudit\b",
]

# Si non vide : le lieu doit matcher un de ces motifs. Vide = pas de filtre.
LOCATIONS = [
    r"london", r"paris", r"france", r"united kingdom", r"amsterdam",
    r"geneva", r"zurich", r"dublin",
]

SEEN_FILE = Path(__file__).parent / "seen.json"
STATUS_FILE = Path(__file__).parent / "statuts.csv"

# Statuts reconnus dans statuts.csv, dans l'ordre d'affichage du rapport.
STATUTS = [
    ("entretien", "\U0001F7E2 Entretien / reponse positive"),
    ("postule", "\U0001F535 Postule - en attente"),
    ("", "\u26AA A traiter"),
    ("mort", "\U0001F534 Mort (refus, offre fermee)"),
    ("ignore", "\u26AB Ignore"),
    ("cdi-2027", "\U0001F7E3 CDI 2027 - a rouvrir en fevrier"),
]
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
    vus = set()
    offset = 0
    while offset < 200:
        payload = _get_json(
            url, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""}
        )
        posts = payload.get("jobPostings", [])
        if not posts:
            break
        paths = {j.get("externalPath", "") for j in posts}
        if paths & vus:
            break
        vus |= paths
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


def short_id(job_id):
    """gh:jumptrading:8010307 -> 8010307, l'identifiant a taper dans statuts.csv."""
    return job_id.rsplit(":", 1)[-1].strip("/").replace("/", "-")


def load_statuts():
    """statuts.csv : id,statut  (postule / entretien / mort / ignore)"""
    import csv
    out = {}
    if not STATUS_FILE.exists():
        return out
    with open(STATUS_FILE, newline="") as f:
        for r in csv.reader(f):
            if len(r) >= 2 and r[0].strip() and r[0].strip().lower() != "id":
                out[r[0].strip()] = r[1].strip().lower()
    return out


def matches(job):
    title = job["title"].lower()
    if any(re.search(p, title) for p in EXCLUDE):
        return False
    if not any(re.search(p, title) for p in LEVEL):
        return False
    if not any(re.search(p, title) for p in ROLE):
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

    uniques = {}
    for j in jobs:
        uniques.setdefault(j["id"], j)
    jobs = list(uniques.values())

    relevant = [j for j in jobs if matches(j)]

    # Rapport groupe par statut, regenere a chaque run.
    statuts = load_statuts()
    groupes = {cle: [] for cle, _ in STATUTS}
    for j in sorted(relevant, key=lambda x: (x["company"], x["title"])):
        sid = short_id(j["id"])
        st = statuts.get(sid, "")
        groupes[st if st in groupes else ""].append((sid, j))

    report = ["# Offres pertinentes", ""]
    report.append(" | ".join(
        f"{lib.split(' ')[0]} {len(groupes[cle])}" for cle, lib in STATUTS
    ))
    report.append("")

    for cle, libelle in STATUTS:
        lot = groupes[cle]
        if not lot:
            continue
        report.append(f"## {libelle} ({len(lot)})")
        report.append("")
        for sid, j in lot:
            report.append(f"- `{sid}` **{j['company']}** — [{j['title']}]({j['url']})")
            report.append(f"  <sub>{j['location'] or 'lieu non precise'}</sub>")
        report.append("")

    Path(__file__).parent.joinpath("offres.md").write_text("\n".join(report))

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
