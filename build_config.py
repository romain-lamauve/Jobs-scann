#!/usr/bin/env python3
"""
Regenere boards_config.csv depuis les donnees du projet jobseek.

Usage :
    python build_config.py            # secteur finance uniquement
    python build_config.py 2 12 21    # plusieurs codes secteur
    python build_config.py all        # tout (5000+ boites, deconseille)

Donnees : github.com/colophon-group/jobseek (CC BY-NC 4.0)
"""

import csv
import json
import sys
import urllib.request
from io import StringIO
from pathlib import Path

BASE = "https://raw.githubusercontent.com/colophon-group/jobseek/main/apps/crawler/data"
FINANCE = "2"


def get_csv(name):
    with urllib.request.urlopen(f"{BASE}/{name}", timeout=60) as r:
        return list(csv.DictReader(StringIO(r.read().decode())))


def main():
    args = sys.argv[1:]
    if args == ["all"]:
        wanted = None
    elif args:
        wanted = set(args)
    else:
        wanted = {FINANCE}

    companies = {r["slug"]: r for r in get_csv("companies.csv")}
    boards = get_csv("boards.csv")

    rows = []
    for b in boards:
        c = companies.get(b["company_slug"])
        if not c:
            continue
        if wanted is not None and c["industry"] not in wanted:
            continue
        try:
            m = json.loads(b["monitor_config"] or "{}")
        except Exception:
            continue

        if b["monitor_type"] == "greenhouse" and m.get("token"):
            rows.append(
                {"name": c["name"], "kind": "greenhouse",
                 "a": m["token"], "b": "", "c": ""}
            )
        elif b["monitor_type"] == "workday" and m.get("company") and m.get("wd_instance"):
            rows.append(
                {"name": c["name"], "kind": "workday", "a": m["wd_instance"],
                 "b": m["company"], "c": m.get("site", "")}
            )

    rows.sort(key=lambda r: r["name"].lower())
    out = Path(__file__).parent / "boards_config.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "kind", "a", "b", "c"])
        w.writeheader()
        w.writerows(rows)

    gh = sum(1 for r in rows if r["kind"] == "greenhouse")
    print(f"{len(rows)} boards ecrits dans {out.name} "
          f"({gh} greenhouse, {len(rows) - gh} workday)")
    print("Ouvre le fichier et supprime les lignes qui ne t'interessent pas.")


if __name__ == "__main__":
    main()
