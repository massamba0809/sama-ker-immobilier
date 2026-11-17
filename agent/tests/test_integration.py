"""Persistance de l'état et client MCP."""
import json
import shlex
import sys
from pathlib import Path

from langchain_core.messages import ToolMessage

from sama_agent.agent import SamaAgent
from sama_agent.config import Settings
from sama_agent.models import Decision
from sama_agent.tools.local import build_local_tools
from sama_agent.tools.mocks import build_mock_tools
from sama_agent.tools.providers import load_catalogue_tools_mcp

from .conftest import email
from .fakes import ScriptedLLM, say, tool_call

DRAFT = {"thread_id": "t1", "destinataire": "client@example.com", "objet": "Re", "corps": "Bonjour"}


async def test_pending_approval_survives_restart(settings):
    """La validation en attente est conservée dans SQLite : un nouveau process peut la reprendre."""
    tools, world = build_mock_tools()
    tools += build_local_tools(settings.data_path)
    llm1 = ScriptedLLM(script=[tool_call("creer_brouillon", DRAFT, "c1"),
                               tool_call("envoyer_brouillon", {"draft_id": "MOCK-DRAFT-1"}, "c2")], seen=[])
    a1 = await SamaAgent.create(settings, llm=llm1, tools=tools, persistent=True)
    res = await a1.handle_email(email())
    assert res.status == "pending_approval"
    await a1.aclose()  # "arrêt du programme"

    llm2 = ScriptedLLM(script=[say("Envoyé.")], seen=[])
    a2 = await SamaAgent.create(settings, llm=llm2, tools=tools, persistent=True)  # "redémarrage"
    assert [r.tool for r in await a2.pending("t1")] == ["envoyer_brouillon"]
    res = await a2.resume("t1", {"c2": Decision(approved=True)})
    assert res.status == "done" and len(world.sent) == 1
    await a2.aclose()


async def test_mcp_client_loads_tools_and_agent_uses_them(settings):
    fixture = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"
    s = settings.model_copy(update={
        "mcp_transport": "stdio",
        "mcp_command": sys.executable,
        "mcp_args": shlex.quote(str(fixture)),
    })
    mcp_tools = await load_catalogue_tools_mcp(s)
    assert {t.name for t in mcp_tools} == {"rechercher_biens", "detail_bien"}

    tools = mcp_tools + build_local_tools(settings.data_path)
    llm = ScriptedLLM(script=[tool_call("rechercher_biens", {"quartier": "Mermoz"}, "c1"), say("Un bien trouvé.")], seen=[])
    agent = await SamaAgent.create(settings, llm=llm, tools=tools, persistent=False)
    res = await agent.handle_email(email())
    assert res.status == "done"
    tm = next(m for m in llm.seen[1] if isinstance(m, ToolMessage))
    body = json.loads(tm.content)
    assert body["ok"] is True and body["result"]["biens"][0]["id"] == "FIX-1"


from sama_agent.agent import parse_inbox


def test_parse_inbox_is_tolerant_and_skips_unusable_items():
    raw = {"emails": [
        {"thread_id": "T1", "id": "M1", "from": "a@b.c", "objet": "Salut", "corps": "Bonjour"},
        {"thread_id": "", "body": "sans fil"},          # inexploitable : ignoré
        {"thread_id": "T3", "body": ""},                # sans corps : ignoré
        "pas un dict",
    ]}
    (e,) = parse_inbox(raw)
    assert (e.thread_id, e.message_id, e.sender, e.subject, e.body) == ("T1", "M1", "a@b.c", "Salut", "Bonjour")


async def test_inbox_processing_and_no_double_processing(make_agent):
    seen = []

    async def decide(pending):
        seen.extend(pending)
        return {r.call_id: Decision(approved=False) for r in pending}

    agent, llm, world = await make_agent([
        say("Demande 1 traitée."),
        tool_call("transmettre_a_conseiller", {"thread_id": "MOCK-T2", "motif": "négociation"}, "c1"),
        say("Transmis."),
    ])
    results = await agent.process_inbox(decide)
    assert [r.thread_id for r in results] == ["MOCK-T1", "MOCK-T2"]
    assert results[1].status == "escalated"
    cursor = llm.cursor
    again = await agent.process_inbox(decide)          # la boîte renvoie les mêmes emails
    assert [r.status for r in again] == ["skipped", "skipped"] and llm.cursor == cursor


async def test_llm_hidden_inbox_tool_is_refused_if_model_asks(make_agent):
    agent, llm, _ = await make_agent([tool_call("lire_demandes_non_traitees", {}, "c1"), say("ok")])
    await agent.handle_email(email())
    tm = next(m for m in llm.seen[1] if isinstance(m, ToolMessage))
    assert json.loads(tm.content)["ok"] is False


async def test_pending_threads_found_after_restart(settings):
    tools, world = build_mock_tools()
    tools += build_local_tools(settings.data_path)
    llm1 = ScriptedLLM(script=[tool_call("creer_brouillon", DRAFT, "c1"),
                               tool_call("envoyer_brouillon", {"draft_id": "MOCK-DRAFT-1"}, "c2")], seen=[])
    a1 = await SamaAgent.create(settings, llm=llm1, tools=tools, persistent=True)
    await a1.handle_email(email())
    await a1.aclose()
    a2 = await SamaAgent.create(settings, llm=ScriptedLLM(script=[say("ok")], seen=[]), tools=tools, persistent=True)
    waiting = await a2.pending_threads()
    assert list(waiting) == ["t1"] and waiting["t1"][0].tool == "envoyer_brouillon"
    await a2.aclose()
