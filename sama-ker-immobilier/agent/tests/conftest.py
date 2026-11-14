from __future__ import annotations

import itertools
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from sama_agent.agent import SamaAgent
from sama_agent.config import Settings
from sama_agent.models import InboundEmail
from sama_agent.tools.local import build_local_tools
from sama_agent.tools.mocks import build_mock_tools

from .fakes import ScriptedLLM


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=str(tmp_path / "data"), trace_dir=str(tmp_path / "traces"),
                    llm_backoff_seconds=0.0, max_steps=6, google_api_key="x", _env_file=None)


@pytest.fixture
def make_agent(settings):
    async def _make(script, tools=None, world=None):
        if tools is None:
            tools, world = build_mock_tools()
            tools = tools + build_local_tools(settings.data_path)
        llm = ScriptedLLM(script=script, seen=[])
        now = lambda: datetime(2026, 11, 16, 10, 0, tzinfo=ZoneInfo("Africa/Dakar"))
        agent = await SamaAgent.create(settings, llm=llm, tools=tools, persistent=False, now_fn=now)
        return agent, llm, world

    return _make


_counter = itertools.count(1)


def email(body="Bonjour, je cherche un F3 à Mermoz, budget 400 000 F.", thread="t1", subject="Recherche F3"):
    return InboundEmail(thread_id=thread, message_id=f"m{next(_counter)}", sender="client@example.com", subject=subject, body=body)
