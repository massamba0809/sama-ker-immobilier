"""Client LLM : fabrique du chat model avec tool calling natif selon la configuration."""
from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from .config import Settings


def build_llm(settings: Settings) -> BaseChatModel:
    if settings.llm_provider == "gemini":
        if not settings.google_api_key:
            raise RuntimeError("GOOGLE_API_KEY manquante (voir .env.example).")
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=settings.llm_model, google_api_key=settings.google_api_key,
                                      temperature=0, timeout=settings.llm_timeout_seconds, max_retries=1)
    from langchain_ollama import ChatOllama

    return ChatOllama(model=settings.llm_model, base_url=settings.ollama_base_url, temperature=0,
                      client_kwargs={"timeout": settings.llm_timeout_seconds})
