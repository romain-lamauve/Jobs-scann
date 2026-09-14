#!/usr/bin/env python3
"""
Veille offres quant — poll les job boards ATS et produit un rapport trie.

Fichiers :
  boards_config.csv  liste des boites a interroger (genere par build_config.py)
  statuts.csv        TOI : id,statut,date  -> postule / entretien / mort / ignore
  historique.json    AUTO : memoire entre les runs, ne pas editer
  offres.md          AUTO : le rapport a lire
  new_jobs.md        AUTO : corps de l'issue GitHub

Stdlib uniquement.
"""

import csv
import json
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

ICI = Path(__file__).parent
CONFIG_FILE = ICI / "boards_config.csv"
STATUS_FILE = ICI / "statuts.csv"
HIST_FILE = ICI / "historique.json"

# ============================================================================
# REGLAGES — c'est ici que tu ajustes
# ============================================================================

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
    r"analytics", r"analyst", r"forecast", r"optimi[sz]ation",
]

EXCLUDE = [
    r"\binternal\b", r"\binternational\b", r"\bsales\b", r"marketing",
    r"recruit", r"\bhr\b", r"human resources", r"\blegal\b",
    r"customer", r"account manager", r"business development",
    r"\bdesign\b", r"\bux\b", r"communicat", r"\baudit\b",
    r"\bqa\b", r"quality assurance", r"\bhelpdesk\b",
]

# Si non vide : le lieu doit matcher un de ces motifs.
LOCATIONS = [
    r"london", r"paris", r"france", r"united kingdom", r"amsterdam",
    r"geneva", r"zurich", r"dublin", r"berlin", r"netherlands",
    r"germany", r"switzerland", r"ireland", r"grenoble", r"luxembourg",
]

# Score de pertinence : (ou chercher, motif, points).
# "t" = titre, "l" = lieu. Ajuste librement, c'est transparent.
SCORING = [
    # Lieu
    ("l", r"paris|france|grenoble|toulouse|saclay",           5),
    ("l", r"london|amsterdam|geneva|zurich|dublin|berlin",    2),

    # Coeur de cible : recherche quantitative
    ("t", r"quantitative research|quant research",            6),
    ("t", r"\bquant\b|quantitative",                          4),
    ("t", r"research scientist|applied scientist",            4),
    ("t", r"\bresearch\b|\bresearcher\b|\bR&D\b",             3),

    # Competences que tu veux sur le CV
    ("t", r"\bc\+\+\b",                                       4),
    ("t", r"optimi[sz]ation|operations research|\bOR\b",      3),
    ("t", r"modell?ing|simulation|numerical",                 3),
    ("t", r"machine learning|deep learning|\bml\b|\bai\b",    2),
    ("t", r"algorithm|statistic|probabil|stochastic",         3),
    ("t", r"signal|forecast|prediction|time series",          3),
    ("t", r"\bpython\b|\bdata scien",                         1),

    # Calendrier et format
    ("t", r"2027",                                            3),
    ("t", r"m1/m2|stage|six.month|6.month|final.year",        3),
    ("t", r"\bintern\b|internship|\bstagiaire\b",             2),
    ("t", r"\bgraduate\b|full.time|new grad",                -3),

    # Ce que tu ne veux pas
    ("t", r"fpga|hardware|embedded|firmware",                 -4),
    ("t", r"systems engineer|site reliability|\bdevops\b",    -4),
    ("t", r"front.end|frontend|\bweb\b|mobile|\bios\b|android", -5),
    ("t", r"support|helpdesk|operations analyst",             -4),
    ("t", r"\bsecurity\b|penetration|compliance",            -3),
    ("t", r"\bui\b|\bux\b|product manager",                   -4),
]

# Seuils : avec 1600 boards, on ne lit et on n'alerte que le haut du panier.
SEUIL_ALERTE = 9        # score minimum pour declencher un mail
SEUIL_AFFICHAGE = 5     # score minimum pour apparaitre dans la section "A traiter"
MAX_A_TRAITER = 60      # nombre max de lignes affichees dans "A traiter"


# Statuts reconnus, dans l'ordre d'affichage du rapport.
STATUTS = [
    ("entretien", "\U0001F7E2 Entretien / reponse positive"),
    ("postule",   "\U0001F535 Postule - en attente"),
    ("",          "\u26AA A traiter"),
    ("mort",      "\U0001F534 Mort (refus, offre fermee)"),
    ("ignore",    "\u26AB Ignore"),
]

RELANCE_JOURS = 10      # relancer au-dela de N jours sans reponse
RUNS_AVANT_MORT = 2     # offre absente N runs d'affilee -> fermee
WORKERS = 16            # requetes en parallele

UA = {"User-Agent": "quant-watch/2.0 (personal job alert)"}

# ============================================================================
# Recuperation
# ============================================================================


def _get_json(url, data=None):
    req = urllib.request.Request(url, headers=dict(UA))
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_greenhouse(company, token):
    payload = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    return [
        {
            "company": company,
            "title": j.get("title", "").strip(),
            "location": (j.get("location") or {}).get("name", "").strip(),
            "url": j.get("absolute_url", ""),
        }
        for j in payload.get("jobs", [])
    ]


def fetch_workday(company, instance, tenant, site):
    base = f"https://{tenant}.{instance}.myworkdayjobs.com"
    url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    out, vus, offset = [], set(), 0
    while offset < 200:
        payload = _get_json(
            url, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""}
        )
        posts = payload.get("jobPostings", [])
        if not posts:
            break
        paths = {j.get("externalPath", "") for j in posts}
        if paths & vus:          # pagination cassee : meme page renvoyee
            break
        vus |= paths
        for j in posts:
            out.append(
                {
                    "company": company,
                    "title": j.get("title", "").strip(),
                    "location": j.get("locationsText", "").strip(),
                    "url": f"{base}/{site}{j.get('externalPath', '')}",
                }
            )
        offset += 20
    return out


def load_boards():
    gh, wd = [], []
    with open(CONFIG_FILE, newline="") as f:
        for r in csv.DictReader(f):
            if r["kind"] == "greenhouse":
                gh.append((r["name"], r["a"]))
            elif r["kind"] == "workday":
                wd.append((r["name"], r["a"], r["b"], r["c"]))
    return gh, wd


def recuperer_tout():
    gh, wd = load_boards()
    print(f"{len(gh)} boards Greenhouse + {len(wd)} Workday")
    taches = [(fetch_greenhouse, a) for a in gh] + [(fetch_workday, a) for a in wd]
    jobs, erreurs = [], []

    def run(t):
        fn, args = t
        try:
            return fn(*args), None
        except Exception as e:
            return [], f"{args[0]}: {e}"

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for res, err in ex.map(run, taches):
            jobs.extend(res)
            if err:
                erreurs.append(err)
    return jobs, erreurs


# ============================================================================
# Identite, filtrage, score
# ============================================================================


def cle(job):
    """Identite stable d'un poste : boite + titre normalise + lieu.
    Resiste a une republication sous un nouvel id ATS."""
    t = re.sub(r"[^a-z0-9]+", "", job["title"].lower())
    l = re.sub(r"[^a-z0-9]+", "", job["location"].lower())[:40]
    c = re.sub(r"[^a-z0-9]+", "", job["company"].lower())
    return f"{c}|{t}|{l}"


def code_court(k):
    """Code court et stable a taper dans statuts.csv, ex. JUM-4f2a."""
    prefixe = re.sub(r"[^A-Z]", "", k.split("|")[0].upper())[:3] or "XXX"
    h = 0
    for ch in k:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return f"{prefixe}-{h:08x}"[:12]


def retenu(job):
    t = job["title"].lower()
    if not t:
        return False
    if any(re.search(p, t) for p in EXCLUDE):
        return False
    if not any(re.search(p, t) for p in LEVEL):
        return False
    if not any(re.search(p, t) for p in ROLE):
        return False
    if LOCATIONS:
        l = job["location"].lower()
        if l and not any(re.search(p, l) for p in LOCATIONS):
            return False
    return True


def score(job):
    t, l = job["title"].lower(), job["location"].lower()
    return sum(
        pts for champ, motif, pts in SCORING
        if re.search(motif, t if champ == "t" else l)
    )


# ============================================================================
# Statuts et historique
# ============================================================================


def load_statuts():
    """statuts.csv : id,statut,date"""
    out = {}
    if not STATUS_FILE.exists():
        return out
    with open(STATUS_FILE, newline="") as f:
        for r in csv.reader(f):
            if not r or not r[0].strip() or r[0].strip().startswith("#"):
                continue
            if r[0].strip().lower() == "id":
                continue
            out[r[0].strip()] = {
                "statut": (r[1].strip().lower() if len(r) > 1 else ""),
                "date": (r[2].strip() if len(r) > 2 else ""),
            }
    return out


def jours_depuis(d):
    try:
        return (date.today() - datetime.strptime(d, "%Y-%m-%d").date()).days
    except Exception:
        return None


# ============================================================================
# Rapport
# ============================================================================


def ligne(entree, st):
    j = entree
    bits = [f"- `{j['code']}` **{j['company']}** — [{j['title']}]({j['url']})"]
    meta = [j["location"] or "lieu non precise", f"score {j['score']}"]
    if j.get("first_seen"):
        meta.append(f"vue le {j['first_seen']}")
    if st.get("date"):
        n = jours_depuis(st["date"])
        if n is not None:
            meta.append(f"**postule il y a {n} j**" if n >= RELANCE_JOURS
                        else f"postule il y a {n} j")
    if j.get("ferme"):
        meta.append(f"**disparue le {j['ferme']}**")
    bits.append(f"  <sub>{' · '.join(meta)}</sub>")
    return "\n".join(bits)


def construire_rapport(entrees, statuts):
    groupes = {c: [] for c, _ in STATUTS}
    for e in entrees:
        st = statuts.get(e["code"], {}).get("statut", "")
        if e.get("ferme") and st in ("", "postule"):
            st = "mort"
        groupes[st if st in groupes else ""].append(e)

    for lot in groupes.values():
        lot.sort(key=lambda x: (-x["score"], x["company"]))

    out = ["# Offres pertinentes", "",
           f"_Mis a jour le {date.today().isoformat()}_", ""]
    out.append(" | ".join(f"{lib.split(' ')[0]} {len(groupes[c])}"
                          for c, lib in STATUTS))
    out.append("")

    # Relances a faire
    relances = [
        e for e in groupes["postule"]
        if (n := jours_depuis(statuts.get(e["code"], {}).get("date", ""))) is not None
        and n >= RELANCE_JOURS
    ]
    if relances:
        out += [f"## \u23F0 A relancer ({len(relances)})", ""]
        out += [ligne(e, statuts.get(e["code"], {})) for e in relances]
        out.append("")

    for c, lib in STATUTS:
        lot = groupes[c]
        if not lot:
            continue
        masques = 0
        if c == "":
            total = len(lot)
            lot = [e for e in lot if e["score"] >= SEUIL_AFFICHAGE][:MAX_A_TRAITER]
            masques = total - len(lot)
        titre = f"## {lib} ({len(lot)})"
        if masques:
            titre += f" — {masques} autres sous le seuil de score {SEUIL_AFFICHAGE}"
        out += [titre, ""]
        out += [ligne(e, statuts.get(e["code"], {})) for e in lot]
        out.append("")

    out += ["---", "",
            "**Marquer une candidature** : ajoute une ligne dans `statuts.csv`",
            "au format `code,statut,AAAA-MM-JJ`.",
            "Statuts : `postule`, `entretien`, `mort`, `ignore`."]
    return "\n".join(out)


# ============================================================================
# Main
# ============================================================================


def main():
    jobs, erreurs = recuperer_tout()
    aujourdhui = date.today().isoformat()

    # Dedoublonnage semantique
    vus = {}
    for j in jobs:
        vus.setdefault(cle(j), j)
    pertinents = {k: j for k, j in vus.items() if retenu(j)}
    print(f"{len(jobs)} offres brutes, {len(vus)} uniques, "
          f"{len(pertinents)} pertinentes")
    for e in erreurs[:15]:
        print(f"  [erreur] {e}")
    if len(erreurs) > 15:
        print(f"  ... et {len(erreurs) - 15} autres erreurs")

    hist = json.loads(HIST_FILE.read_text()) if HIST_FILE.exists() else {}
    premier_run = not HIST_FILE.exists()
    nouvelles = []

    for k, j in pertinents.items():
        h = hist.get(k)
        if h is None:
            h = {"first_seen": aujourdhui}
            nouvelles.append(k)
        h.update(
            company=j["company"], title=j["title"], location=j["location"],
            url=j["url"], last_seen=aujourdhui, absente=0,
        )
        h.pop("ferme", None)
        hist[k] = h

    # Offres disparues du flux
    for k, h in hist.items():
        if k in pertinents:
            continue
        h["absente"] = h.get("absente", 0) + 1
        if h["absente"] >= RUNS_AVANT_MORT and not h.get("ferme"):
            h["ferme"] = aujourdhui

    HIST_FILE.write_text(json.dumps(hist, indent=1, ensure_ascii=False))

    entrees = []
    for k, h in hist.items():
        e = dict(h)
        e["code"] = code_court(k)
        e["score"] = score(h)
        entrees.append(e)

    statuts = load_statuts()
    (ICI / "offres.md").write_text(construire_rapport(entrees, statuts))

    if premier_run or not nouvelles:
        print("premier run ou aucune nouveaute : pas d'alerte")
        return

    lot = sorted(
        (dict(hist[k], code=code_court(k), score=score(hist[k])) for k in nouvelles),
        key=lambda x: -x["score"],
    )
    sous_seuil = len([e for e in lot if e["score"] < SEUIL_ALERTE])
    lot = [e for e in lot if e["score"] >= SEUIL_ALERTE]
    if not lot:
        print(f"{sous_seuil} nouveautes, toutes sous le seuil d'alerte : pas de mail")
        return
    corps = [f"{len(lot)} nouvelle(s) offre(s) au-dessus du seuil, "
             f"triees par pertinence.", ""]
    if sous_seuil:
        corps.append(f"_({sous_seuil} autres nouveautes sous le seuil, "
                     f"visibles dans offres.md)_")
        corps.append("")
    corps += [ligne(e, {}) for e in lot]
    (ICI / "new_jobs.md").write_text("\n".join(corps))

    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a") as f:
            f.write("has_new=true\n")
            f.write(f"count={len(lot)}\n")
    else:
        print("\n" + "\n".join(corps))


if __name__ == "__main__":
    main()
