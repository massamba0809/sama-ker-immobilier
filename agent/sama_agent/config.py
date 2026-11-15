"""Configuration centralisée. Toutes les valeurs viennent de l'environnement / .env."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

AGENT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=AGENT_ROOT / ".env", extra="ignore")

    # Modèle
    llm_provider: Literal["gemini", "ollama"] = "gemini"
    llm_model: str = "gemini-2.5-flash"
    google_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"

    # Boucle
    max_steps: int = 8
    max_tool_calls_per_turn: int = 4
    llm_retries: int = 2
    repeat_call_limit: int = 3
    llm_backoff_seconds: float = 1.0
    llm_timeout_seconds: float = 60.0

    # Outils
    tools_mode: Literal["mock", "hybrid", "integrated"] = "mock"
    mcp_transport: Literal["stdio", "streamable_http"] = "streamable_http"
    mcp_url: str = "http://localhost:8000/mcp"
    mcp_command: str = "python"
    mcp_args: str = ""
    # Dossier depuis lequel le serveur MCP stdio est lancé (défaut : racine du dépôt, où se trouve catalog/)
    mcp_cwd: str = ""
    google_tools_module: str = ""

    # Divers
    data_dir: str = "data"
    trace_dir: str = "traces"
    timezone: str = "Africa/Dakar"

    def path(self, name: str) -> Path:
        p = Path(name)
        return p if p.is_absolute() else AGENT_ROOT / p

    @property
    def data_path(self) -> Path:
        d = self.path(self.data_dir)
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def trace_path(self) -> Path:
        d = self.path(self.trace_dir)
        d.mkdir(parents=True, exist_ok=True)
        return d
