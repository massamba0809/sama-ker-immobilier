"""Chargement des outils selon TOOLS_MODE.

mock       : tout simulé (tools/mocks.py).
integrated : catalogue via client MCP (serveur d'Alassane) + Calendar/Gmail via le module
             de Baye Serigne (GOOGLE_TOOLS_MODULE exposant `get_google_tools()`).

Le moteur de l'agent ne change pas : seul ce fichier connaît l'origine des outils.
"""
from __future__ import annotations

import importlib
import shlex

from langchain_core.tools import BaseTool

from ..config import Settings
from .local import build_local_tools
from .mocks import MockWorld, build_mock_tools


async def load_catalogue_tools_mcp(settings: Settings) -> list[BaseTool]:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    if settings.mcp_transport == "stdio":
        conn = {"transport": "stdio", "command": settings.mcp_command, "args": shlex.split(settings.mcp_args)}
    else:
        conn = {"transport": "streamable_http", "url": settings.mcp_url}
    client = MultiServerMCPClient({"catalogue": conn})
    return await client.get_tools()


def load_google_tools(settings: Settings) -> list[BaseTool]:
    if not settings.google_tools_module:
        raise RuntimeError("GOOGLE_TOOLS_MODULE non défini (module de Baye Serigne attendu).")
    module = importlib.import_module(settings.google_tools_module)
    return list(module.get_google_tools())


async def load_tools(settings: Settings) -> tuple[list[BaseTool], MockWorld | None]:
    local = build_local_tools(settings.data_path)
    if settings.tools_mode == "mock":
        tools, world = build_mock_tools()
        return tools + local, world
    catalogue = await load_catalogue_tools_mcp(settings)
    google = load_google_tools(settings)
    return catalogue + google + local, None
