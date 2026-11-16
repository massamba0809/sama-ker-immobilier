"""Crée (une seule fois) un agenda Google par conseiller sur le compte de test.

    python google_integration/setup_conseillers.py            # 3 conseillers par défaut
    python google_integration/setup_conseillers.py "Awa Diop" "Moussa Ndiaye"

Idempotent : un agenda déjà présent n'est pas recréé. Les noms sont fictifs.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google_integration.google_tools import CONSEILLER_PREFIX, conseillers, get_calendar_service  # noqa: E402

DEFAUT = ["Awa Diop", "Moussa Ndiaye", "Fatou Sarr"]


def main(noms: list[str]) -> None:
    service = get_calendar_service()
    existants = conseillers(service)
    for nom in noms:
        if nom in existants:
            print(f"= {nom} : déjà présent")
            continue
        cal = service.calendars().insert(body={"summary": f"{CONSEILLER_PREFIX}{nom}", "timeZone": "Africa/Dakar"}).execute()
        print(f"+ {nom} : agenda créé ({cal['id']})")
    print("Conseillers :", ", ".join(conseillers(service)))


if __name__ == "__main__":
    main(sys.argv[1:] or DEFAUT)
