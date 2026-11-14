"""Interface en ligne de commande pour le conseiller.

    python -m sama_agent.cli email --thread t1 --from client@example.com --subject "..." --body "..."
    python -m sama_agent.cli inbox      # lit les demandes non traitées et les traite une à une
    python -m sama_agent.cli inbox --watch 60   # boucle de démo : relit la boîte toutes les 60 s (Ctrl-C pour arrêter)
    python -m sama_agent.cli pending    # reprend les validations laissées en attente

Chaque action sensible (envoi d'email, écriture agenda) est affichée avec ses paramètres exacts et
n'est exécutée qu'après une réponse explicite du conseiller.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .agent import SamaAgent
from .config import Settings
from .models import ApprovalRequest, Decision, InboundEmail, RunResult


def ask_decisions(pending: list[ApprovalRequest]) -> dict[str, Decision]:
    """Validation humaine bloquante : rien n'est exécuté tant qu'on n'a pas répondu."""
    decisions: dict[str, Decision] = {}
    for r in pending:
        print("\n" + "=" * 70)
        print(f"ACTION À VALIDER : {r.tool}")
        print(f"Paramètres      : {json.dumps(r.args, ensure_ascii=False, indent=2)}")
        if r.preview:
            print(f"Contenu         : {r.preview}")
        ans = input("Approuver ? [o]ui / [n]on (ou texte = refus avec consigne) : ").strip()
        if ans.lower() in ("o", "oui", "y", "yes"):
            decisions[r.call_id] = Decision(approved=True)
        else:
            decisions[r.call_id] = Decision(approved=False, reason="" if ans.lower() in ("n", "non", "") else ans)
    return decisions


async def decide(pending: list[ApprovalRequest]) -> dict[str, Decision]:
    return ask_decisions(pending)


def show(result: RunResult) -> None:
    print(f"\n--- fil {result.thread_id} · statut : {result.status} ---")
    if result.final_text:
        print(result.final_text)
    for e in result.effects:
        print(f"  ✔ {e['tool']} -> {json.dumps(e['result'], ensure_ascii=False)[:200]}")


async def watch_inbox(agent: SamaAgent, interval: float) -> None:
    """Boucle de démo : traite les validations laissées en attente, puis relit la boîte à intervalle régulier.
    Une erreur de lecture (réseau, token) est affichée et la boucle continue."""
    print(f"Surveillance de la boîte toutes les {interval:g} s (Ctrl-C pour arrêter).")
    for thread_id in await agent.pending_threads():
        result = await agent.resume(thread_id, await decide(await agent.pending(thread_id)))
        while result.status == "pending_approval":
            result = await agent.resume(thread_id, await decide(result.pending))
        show(result)
    while True:
        try:
            for r in await agent.process_inbox(decide):
                if r.status != "skipped":
                    show(r)
                    print(f"Trace : {agent.tracer.path_for(r.thread_id)}")
        except RuntimeError as e:
            print(f"[boîte] {e}", file=sys.stderr)
        await asyncio.sleep(interval)


async def amain(args: argparse.Namespace) -> int:
    agent = await SamaAgent.create(Settings())
    try:
        if args.cmd == "email":
            email = InboundEmail(thread_id=args.thread, sender=args.sender, subject=args.subject, body=args.body)
            results = [await agent.process(email, decide)]
        elif args.cmd == "inbox" and args.watch:
            await watch_inbox(agent, args.watch)
            return 0
        elif args.cmd == "inbox":
            results = await agent.process_inbox(decide)
            if not results:
                print("Aucune demande à traiter.")
        else:  # pending
            waiting = await agent.pending_threads()
            if not waiting:
                print("Aucune validation en attente.")
            results = []
            for thread_id in waiting:
                result = await agent.resume(thread_id, await decide(await agent.pending(thread_id)))
                while result.status == "pending_approval":
                    result = await agent.resume(thread_id, await decide(result.pending))
                results.append(result)
        for r in results:
            show(r)
            print(f"Trace : {agent.tracer.path_for(r.thread_id)}")
        return 0 if all(r.status in ("done", "escalated", "skipped") for r in results) else 1
    finally:
        await agent.aclose()


def main() -> None:
    p = argparse.ArgumentParser(description="Agent Sama Kër Immobilier")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("email", help="traiter un email donné")
    e.add_argument("--from", dest="sender", required=True)
    e.add_argument("--subject", default="")
    e.add_argument("--body", required=True)
    e.add_argument("--thread", required=True, help="identifiant du fil Gmail (clé de l'état)")
    i = sub.add_parser("inbox", help="lire et traiter les demandes non traitées")
    i.add_argument("--watch", type=float, default=0, metavar="SECONDES",
                   help="relire la boîte en boucle à cet intervalle (démo)")
    sub.add_parser("pending", help="reprendre les validations en attente")
    try:
        sys.exit(asyncio.run(amain(p.parse_args())))
    except KeyboardInterrupt:
        print("\nArrêt.")
        sys.exit(0)


if __name__ == "__main__":
    main()
