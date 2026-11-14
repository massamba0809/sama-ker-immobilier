"""Garde-fous contre les instructions malveillantes contenues dans les emails.

Deux couches complémentaires :
1. Cette détection déterministe (avant tout appel LLM) : un email qui tente de
   modifier les consignes est escaladé à un humain sans passer par le modèle.
2. Le prompt système + l'encadrement `<email_client_non_fiable>` pour tout le reste.

Et surtout, la garantie structurelle : même un modèle manipulé ne peut ni envoyer
ni réserver, car ces actions exigent un accord humain (voir graph.py).
"""
from __future__ import annotations

import re
import unicodedata

# Motifs volontairement précis, pour limiter les faux positifs sur de vrais clients.
_INJECTION_PATTERNS = [
    r"ignor\w*\s+(tes|vos|les|toutes?\s+(les|tes|vos)?)?\s*(consignes?|instructions?|regles?|directives?)",
    r"oubli\w*\s+(tes|vos|les)?\s*(consignes?|instructions?|regles?)",
    r"ignore\s+(all|any|previous|prior|your)\s+(previous\s+)?(instructions?|rules?|prompts?)",
    r"disregard\s+(all|previous|your)",
    r"(system|developer)\s*prompt",
    r"prompt\s+systeme",
    r"nouvelles?\s+(consignes?|instructions?)\s*:",
    r"(tu|vous)\s+(es|etes|devez|dois)\s+(maintenant|desormais)",
    r"you\s+are\s+now",
    r"(envoie|envoyez|donne|donnez|transmets?|affiche|liste)[\w\s,'-]{0,30}(liste|base|donnees|coordonnees|emails?|numeros?)\s+(des\s+|de\s+tous\s+les\s+)?clients",
    r"(liste|base|coordonnees|emails?|numeros?)\s+(de\s+tous\s+les\s+|des\s+)clients",
    r"mode\s+(admin|developpeur|debug|dieu)",
    r"jailbreak|\bdan\b\s+mode",
]
_COMPILED = [re.compile(p) for p in _INJECTION_PATTERNS]


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower())


def detect_injection(text: str) -> list[str]:
    """Retourne les motifs suspects trouvés (liste vide = rien de détecté)."""
    norm = _normalize(text)
    return [p.pattern for p in _COMPILED if p.search(norm)]


_TAG_RE = re.compile(r"</?\s*(email_client_non_fiable|donnee_non_fiable|system|assistant|tool)[^>]*>", re.I)


def neutralize(text: str) -> str:
    """Empêche un email de refermer/falsifier nos balises d'encadrement."""
    return _TAG_RE.sub("[balise retirée]", text)


def wrap_untrusted(text: str, label: str = "email_client_non_fiable") -> str:
    return f"<{label}>\n{neutralize(text)}\n</{label}>"
