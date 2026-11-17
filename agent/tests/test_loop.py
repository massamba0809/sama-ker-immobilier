"""Boucle d'exécution : contexte, tool calls, résultats, erreurs, limites."""
import json

from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from sama_agent.tools.local import build_local_tools
from sama_agent.tools.mocks import build_mock_tools

from .conftest import email
from .fakes import say, tool_call


def tool_messages(llm, call_index):
    return [m for m in llm.seen[call_index] if isinstance(m, ToolMessage)]


async def test_context_contains_system_prompt_date_and_wrapped_email(make_agent):
    agent, llm, _ = await make_agent([say("ok")])
    await agent.handle_email(email())
    first = llm.seen[0]
    assert isinstance(first[0], SystemMessage)
    assert "lundi 16/11/2026" in first[0].content and "Africa/Dakar" in first[0].content
    assert "<email_client_non_fiable>" in first[1].content and "F3 à Mermoz" in first[1].content


async def test_tool_call_executed_and_result_sent_back_to_llm(make_agent):
    agent, llm, _ = await make_agent([
        tool_call("rechercher_biens", {"quartier": "Mermoz", "pieces": 3, "budget_max": 400000}, "c1"),
        say("Un bien correspond."),
    ])
    res = await agent.handle_email(email())
    assert res.status == "done" and res.final_text == "Un bien correspond."
    (tm,) = tool_messages(llm, 1)
    body = json.loads(tm.content)
    assert body["ok"] is True and [b["id"] for b in body["result"]["biens"]] == ["MOCK-001"]
    events = [e["event"] for e in agent.tracer.read("t1")]
    assert "tool_call" in events and "tool_result" in events


async def test_empty_result_is_reported_as_empty(make_agent):
    agent, llm, _ = await make_agent([
        tool_call("rechercher_biens", {"quartier": "Mermoz", "budget_max": 1000}, "c1"),
        say("Aucun bien ne correspond."),
    ])
    await agent.handle_email(email())
    body = json.loads(tool_messages(llm, 1)[0].content)
    assert body["ok"] is True and body["result"]["empty"] is True and body["result"]["count"] == 0


async def test_unknown_tool_is_refused(make_agent):
    agent, llm, _ = await make_agent([tool_call("supprimer_tous_les_biens", {}, "c1"), say("ok")])
    await agent.handle_email(email())
    body = json.loads(tool_messages(llm, 1)[0].content)
    assert body["ok"] is False and "inconnu ou non autorisé" in body["error"]


async def test_invalid_arguments_are_rejected_without_execution(make_agent):
    agent, llm, world = await make_agent([tool_call("detail_bien", {"mauvais": 1}, "c1"), say("ok")])
    await agent.handle_email(email())
    body = json.loads(tool_messages(llm, 1)[0].content)
    assert body["ok"] is False and "Arguments invalides" in body["error"]


async def test_tool_exception_is_returned_as_error_not_crash(make_agent, settings):
    def boom(bien_id: str) -> dict:
        """Panne simulée."""
        raise RuntimeError("base indisponible")

    tools, _ = build_mock_tools()
    tools = [t for t in tools if t.name != "detail_bien"] + [StructuredTool.from_function(boom, name="detail_bien")]
    tools += build_local_tools(settings.data_path)
    agent, llm, _ = await make_agent([tool_call("detail_bien", {"bien_id": "x"}, "c1"), say("Je transmets.")], tools=tools)
    res = await agent.handle_email(email())
    body = json.loads(tool_messages(llm, 1)[0].content)
    assert body["ok"] is False and "base indisponible" in body["error"]
    assert res.status == "done"


async def test_step_limit_stops_the_loop(make_agent, settings):
    # le modèle redemande indéfiniment des appels différents : la boucle doit s'arrêter à MAX_STEPS
    script = [tool_call("detail_bien", {"bien_id": f"id{i}"}, f"c{i}") for i in range(50)]
    agent, llm, _ = await make_agent(script)
    res = await agent.handle_email(email())
    assert res.status == "max_steps"
    assert llm.cursor == settings.max_steps


async def test_identical_read_call_loop_is_blocked(make_agent):
    script = [tool_call("detail_bien", {"bien_id": "MOCK-001"}, f"c{i}") for i in range(3)] + [say("fin")]
    agent, llm, _ = await make_agent(script)
    await agent.handle_email(email())
    last = json.loads(tool_messages(llm, 3)[-1].content)
    assert last["ok"] is False and "répété" in last["error"]


async def test_llm_provider_error_is_retried_then_succeeds(make_agent):
    agent, llm, _ = await make_agent([ConnectionError("réseau"), say("ok")])
    res = await agent.handle_email(email())
    assert res.status == "done" and llm.cursor == 2


async def test_llm_provider_down_gives_llm_error_status(make_agent):
    agent, llm, _ = await make_agent([ConnectionError("1"), ConnectionError("2"), ConnectionError("3")])
    res = await agent.handle_email(email())
    assert res.status == "llm_error"


async def test_write_without_confirmation_key_is_not_reported_as_success(make_agent, settings):
    def creer_visite(bien_id: str, debut: str, client_email: str) -> dict:
        """Outil défectueux : dit ok mais ne renvoie pas d'identifiant."""
        return {"ok": True}

    tools, _ = build_mock_tools()
    tools = [t for t in tools if t.name != "creer_visite"] + [StructuredTool.from_function(creer_visite)]
    tools += build_local_tools(settings.data_path)
    agent, llm, _ = await make_agent(
        [tool_call("creer_visite", {"bien_id": "MOCK-001", "debut": "2026-11-19T16:00:00+00:00", "client_email": "a@b.c"}, "c1"),
         say("ok")], tools=tools)
    res = await agent.handle_email(email())
    assert res.status == "pending_approval"
    res = await agent.resume("t1", {"c1": __import__("sama_agent.models", fromlist=["Decision"]).Decision(approved=True)})
    body = json.loads(tool_messages(llm, 1)[0].content)
    assert body["ok"] is False and "non confirmé" in json.dumps(body, ensure_ascii=False)
    assert res.effects == []


def test_rate_limit_waits_for_provider_delay(settings):
    from sama_agent.graph import retry_delay

    quota = RuntimeError("429 RESOURCE_EXHAUSTED ... Please retry in 12.4s. ... 'retryDelay': '12s'")
    other = RuntimeError("connexion réinitialisée")
    assert retry_delay(quota, 0, settings) == 13.4                       # délai indiqué par Gemini
    assert retry_delay(RuntimeError("429 Too Many Requests"), 0, settings) == settings.llm_rate_limit_wait_seconds
    assert retry_delay(other, 1, settings) == settings.llm_backoff_seconds * 2
    avant = settings.model_copy(update={"correctifs": False})
    assert retry_delay(quota, 0, avant) == avant.llm_backoff_seconds    # comportement d'avant correction
