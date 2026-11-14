"""Faux LLM scripté : permet de tester la boucle sans clé API ni réseau."""
from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


def tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def say(text: str) -> AIMessage:
    return AIMessage(content=text)


class ScriptedLLM(BaseChatModel):
    """Rejoue une liste de réponses. Une exception dans la liste est levée (simule une panne fournisseur)."""

    script: list[Any]
    seen: list[list[BaseMessage]] = []
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        if self.cursor >= len(self.script):
            raise RuntimeError("script épuisé : le LLM a été appelé plus que prévu")
        item = self.script[self.cursor]
        self.cursor += 1
        if isinstance(item, Exception):
            raise item
        return ChatResult(generations=[ChatGeneration(message=item)])
