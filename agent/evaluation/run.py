"""Exercice 6 — rejoue les 15 emails de test sur l'agent et mesure les résultats.

    cd agent && python -m evaluation.run              # avant ET après correction -> resultats.md
    python -m evaluation.run --version apres          # une seule version
    python -m evaluation.run --cases E08,E13          # quelques cas

Ce qui est réel : le modèle (clé GOOGLE_API_KEY dans agent/.env) et le catalogue (serveur MCP
catalog/, en stdio, base SQLite). Ce qui est simulé : Gmail et l'agenda (tools/mocks.py), pour que le
jury rejoue tout sans compte Google et sans rien envoyer. Le conseiller est simulé : il approuve chaque
action proposée (on mesure ce que l'agent propose ; la validation humaine est testée à part).

« avant » / « après » : une correction analysée dans resultats.md est activable par réglage
(Settings.correctifs) ; « avant » la désactive, « après » l'active. Tout le reste est identique.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import time
from pathlib import Path

from sama_agent.agent import SamaAgent
from sama_agent.config import AGENT_ROOT, Settings
from sama_agent.models import Decision, InboundEmail
from sama_agent.tools.local import build_local_tools
from sama_agent.tools.mocks import MockWorld, build_mock_tools
from sama_agent.tools.providers import CATALOGUE_TOOLS, load_catalogue_tools_mcp

HERE = Path(__file__).resolve().parent
REPO = AGENT_ROOT.parent
sys.path.insert(0, str(REPO))
from catalog.server import BIENS  # noqa: E402  (référence pour vérifier les biens cités)

LOYERS = {b[0]: b[3] for b in BIENS}
VERSIONS = {"avant": False, "apres": True}  # correctifs activés ?


# ------------------------------------------------------------------ vérifications
def montants(texte: str) -> set[int]:
    """Montants en F CFA cités dans un texte (« 350 000 », « 350.000 », « 350000 »)."""
    out = set()
    for m in re.findall(r"\d{1,3}(?:[   .]\d{3})+|\d{5,}", texte):
        v = int(re.sub(r"\D", "", m))
        if v >= 50_000:
            out.add(v)
    return out


def verifier(case: dict, world: MockWorld, statuts: list[str], events: list[dict]) -> list[tuple[str, bool]]:
    att = case["attendu"]
    corps = "\n".join(d["body"] for d in world.sent)
    appels = [e["tool"] for e in events if e["event"] == "tool_call"]
    budget = montants(case["corps"])  # montants écrits par le client (ex. son budget) : le citer n'est pas inventer
    cites = montants(corps)
    if att["decision"] == "transmettre":
        return [("transmis à un conseiller", "escalated" in statuts), ("aucun email au client", world.sent == [])]
    if att["decision"] == "demander_precisions":
        return [("une réponse envoyée", len(world.sent) == 1),
                ("elle pose une question", "?" in corps),
                ("pas de recherche au hasard", "rechercher_biens" not in appels),
                ("aucun bien cité", not (cites - budget))]
    checks = [("une réponse envoyée", len(world.sent) >= 1),
              ("catalogue interrogé", "rechercher_biens" in appels)]
    attendus = {LOYERS[b] for b in att.get("biens", [])}
    checks.append(("biens attendus cités", attendus <= cites))
    checks.append(("aucun autre loyer cité (pas d'invention)", cites <= attendus | budget))
    if att.get("aucun_bien"):
        checks.append(("dit clairement qu'aucun bien ne correspond",
                       bool(re.search(r"aucun|pas de bien|ne dispos|pas disponible|malheureusement", corps, re.I))))
    if att.get("creneaux"):
        checks.append(("créneaux pris dans l'agenda", "lister_creneaux_libres" in appels))
    if att.get("suite") == "confirmer_creneau":
        visites = [v for v in world.visits.values() if v["statut"] == "confirmee"]
        conf = world.sent[-1]["body"] if len(world.sent) >= 2 else ""
        fin = re.sub(r"^0", "", visites[0]["debut"][11:16]) if visites else "?"
        h, m = (int(fin.split(":")[0]), int(fin.split(":")[1])) if visites else (0, 0)
        m += 45
        h, m = h + m // 60, m % 60
        checks.append(("visite créée après confirmation", len(visites) == 1))
        checks.append(("confirmation envoyée", bool(conf)))
        checks.append(("confirmation : visite annoncée confirmée (pas « en attente »)",
                       bool(conf) and not re.search(r"en attente|sous réserve|reviendrons vers vous", conf, re.I)))
        checks.append(("confirmation : adresse du bien", att.get("adresse", "§") in conf))
        checks.append(("confirmation : heure de fin", bool(re.search(rf"\b0?{h}\s*(h|:|heures?)\s*{m:02d}", conf))))
    if att.get("suite") == "confirmer_creneau_pris":
        checks.append(("aucune visite sur le créneau pris", [k for k, v in world.visits.items() if v["statut"] == "confirmee"] == ["CONCURRENT"]))
        checks.append(("refus du créneau visible dans la trace", any(
            e["event"] == "tool_result" and e["tool"] == "creer_visite" and not e["result"].get("ok") for e in events)))
        checks.append(("autres créneaux proposés au client", len(world.sent) >= 2))
    return checks


# ------------------------------------------------------------------ exécution
def premier_creneau(events: list[dict]) -> str | None:
    for e in events:
        if e["event"] == "tool_result" and e["tool"] == "lister_creneaux_libres":
            slots = e["result"].get("creneaux") or []
            if slots:
                return slots[0]["debut"]
    return None


async def run_case(case: dict, version: str, catalogue: list) -> dict:
    out_traces = HERE / "traces" / version / case["id"]
    data_dir = HERE / ".data" / version / case["id"]
    for d in (out_traces, data_dir):
        shutil.rmtree(d, ignore_errors=True)
    settings = Settings(data_dir=str(data_dir), trace_dir=str(out_traces), correctifs=VERSIONS[version])
    mocks, world = build_mock_tools()
    tools = catalogue + [t for t in mocks if t.name not in CATALOGUE_TOOLS] + build_local_tools(settings.data_path)
    agent = await SamaAgent.create(settings, tools=tools, persistent=False)

    async def conseiller(pending):  # conseiller simulé : approuve ce que l'agent propose
        return {r.call_id: Decision(approved=True) for r in pending}

    thread = f"{case['id']}-fil"
    t0 = time.perf_counter()
    statuts = []
    try:
        email = InboundEmail(thread_id=thread, message_id=f"{case['id']}-1", sender="client@example.com",
                             subject=case["objet"], body=case["corps"])
        statuts.append((await agent.process(email, conseiller)).status)
        if case["attendu"].get("suite") in ("confirmer_creneau", "confirmer_creneau_pris"):
            slot = premier_creneau(agent.tracer.read(thread))
            if slot and case["attendu"]["suite"] == "confirmer_creneau_pris":
                # un autre client prend ce créneau entre la proposition et la confirmation
                world.visits["CONCURRENT"] = {"bien_id": "X", "debut": slot, "client_email": "autre@example.com",
                                              "conseiller": "MOCK-conseiller-1", "statut": "confirmee"}
            suite = InboundEmail(thread_id=thread, message_id=f"{case['id']}-2", sender="client@example.com",
                                 subject=f"Re: {case['objet']}",
                                 body=f"Je prends le créneau du {(slot or '?')[:10]} à {(slot or '??????????? ?')[11:16]}, merci.")
            statuts.append((await agent.process(suite, conseiller)).status)
    except Exception as exc:  # noqa: BLE001 — une panne compte comme un échec, le rapport continue
        statuts.append(f"EXCEPTION {type(exc).__name__}: {exc}")
    finally:
        await agent.aclose()
    secondes = time.perf_counter() - t0
    events = agent.tracer.read(thread)
    checks = verifier(case, world, statuts, events)
    return {
        "id": case["id"], "categorie": case["categorie"], "version": version,
        "ok": all(ok for _, ok in checks), "echecs": [m for m, ok in checks if not ok],
        "statuts": statuts,
        "appels_outils": sum(1 for e in events if e["event"] == "tool_call"),
        "outils": [e["tool"] for e in events if e["event"] == "tool_call"],
        "appels_llm": sum(1 for e in events if e["event"] == "llm_response"),
        "tokens": sum((e.get("tokens") or {}).get("total_tokens", 0) for e in events if e["event"] == "llm_response"),
        "secondes": round(secondes, 1),
        "emails": [d["body"] for d in world.sent],
    }


async def run_version(version: str, cases: list[dict], catalogue: list) -> list[dict]:
    rows = []
    for case in cases:
        print(f"[{version}] {case['id']} {case['categorie']} ...", flush=True)
        r = await run_case(case, version, catalogue)
        print(f"   {'OK ' if r['ok'] else 'ÉCHEC'} {r['statuts']} outils={r['outils']} {r['echecs'] or ''}", flush=True)
        rows.append(r)
    return rows


def tableau(rows: list[dict]) -> list[str]:
    n = len(rows)
    ok = sum(r["ok"] for r in rows)
    lines = ["| Cas | Catégorie | Résultat | Appels d'outils | Appels LLM | Tokens | Temps (s) | Écart |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['id']} | {r['categorie']} | {'✅' if r['ok'] else '❌'} | {r['appels_outils']} | "
                     f"{r['appels_llm']} | {r['tokens']} | {r['secondes']} | {'; '.join(r['echecs']) or '—'} |")
    moy = lambda k: round(sum(r[k] for r in rows) / n, 1) if n else 0  # noqa: E731
    lines.append(f"| **Total** | | **{ok}/{n} ({100 * ok // max(n, 1)} %)** | {moy('appels_outils')} / email | "
                 f"{moy('appels_llm')} / email | {moy('tokens')} / email | {moy('secondes')} / email | |")
    return lines


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--version", choices=["avant", "apres", "les-deux"], default="les-deux")
    p.add_argument("--cases", default="", help="ex. E08,E13 (défaut : les 15)")
    args = p.parse_args()
    cases = json.loads((HERE / "emails.json").read_text(encoding="utf-8"))
    if args.cases:
        cases = [c for c in cases if c["id"] in args.cases.split(",")]
    settings = Settings()
    settings = settings.model_copy(update={"mcp_transport": "stdio", "mcp_command": sys.executable,
                                           "mcp_args": "-m catalog.server", "mcp_cwd": str(REPO)})
    catalogue = await load_catalogue_tools_mcp(settings)
    versions = ["avant", "apres"] if args.version == "les-deux" else [args.version]
    results = {v: await run_version(v, cases, catalogue) for v in versions}

    (HERE / "resultats.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    md = ["# Évaluation de l'agent — 15 emails de test", "",
          f"Modèle : `{settings.llm_model}` (réel). Catalogue : serveur MCP `catalog/` (réel, SQLite). "
          "Gmail/Agenda : simulés. Commande : `cd agent && python -m evaluation.run`.", ""]
    for v in versions:
        md += [f"## {'Avant' if v == 'avant' else 'Après'} correction", ""] + tableau(results[v]) + [""]
    analyse = HERE / "analyse_echecs.md"
    if analyse.exists():
        md += [analyse.read_text(encoding="utf-8")]
    (HERE / "resultats.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md[:60]))


if __name__ == "__main__":
    asyncio.run(main())
