"""Validation vocale du conseiller (option --voix de la CLI).

L'agent lit à voix haute chaque email reçu puis chaque action à valider (texte exact du brouillon,
date et conseiller de la visite) ; le conseiller répond à l'oral : « oui, envoie », « non »,
« non, ajoute l'adresse » (refus avec consigne), « répète ».

Sécurité : seule une réponse qui commence clairement par un accord (« oui », « d'accord », « valide »…)
sans négation ni réserve approuve l'action. Une réponse inaudible ou trop courte n'est jamais devinée :
l'agent redemande, puis passe au clavier. La décision comprise est relue avant d'être appliquée.

Synthèse : `say` (macOS). Micro : `sounddevice`. Transcription : Gemini (clé GOOGLE_API_KEY).
"""
from __future__ import annotations

import io
import math
import json
import re
import shutil
import subprocess
import time
import unicodedata
import wave
from array import array
from datetime import datetime
from typing import Callable

from .config import Settings
from .models import ApprovalRequest, Decision, InboundEmail

REPEAT = "repeat"

_YES = {"oui", "ouais", "ok", "okay", "accord", "daccord", "valide", "valider", "approuve", "approuver",
        "confirme", "confirmer", "vasy", "envoie", "envoyer", "parfait", "cest bon", "go"}
_NO = {"non", "refuse", "refuser", "stop", "nenvoie", "attends", "attend", "pas"}
_RESERVE = {"mais", "sauf", "pas", "ne", "nenvoie", "attends", "attend", "avant", "plutot", "change", "modifie",
            "ajoute", "corrige", "enleve", "retire"}
_REPEAT = {"repete", "repeter", "relis", "relire", "encore", "pardon", "quoi"}
_FILLERS = {"euh", "heu", "hum", "alors", "bon", "bah", "ben", "donc", "et", "je", "tu", "le", "la", "les",
            "ca", "cest", "a", "merci", "sil", "te", "vous", "plait", "brouillon", "email", "mail", "visite",
            "lui", "moi", "y", "vas", "accord", "d"}

_JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
         "novembre", "décembre"]


# ------------------------------------------------------------------ texte -> décision (pur, testé)
def _mots(texte: str) -> list[str]:
    t = unicodedata.normalize("NFD", texte.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = t.replace("d'accord", "daccord").replace("vas-y", "vasy").replace("c'est bon", "cest_bon")
    t = re.sub(r"(\w)'(\w)", r"\1\2", t)  # n'envoie -> nenvoie, s'il -> sil
    return [m.replace("cest_bon", "cest bon") for m in re.findall(r"[a-z_]+", t)]


def interpret(texte: str | None) -> Decision | str | None:
    """Réponse orale -> Decision, REPEAT (relire) ou None (incompris : redemander)."""
    if not texte or "inaudible" in texte.lower():
        return None
    mots = _mots(texte)
    if not mots:
        return None
    utiles = [m for m in mots if m not in _FILLERS]
    if set(mots) & _REPEAT and len(utiles) <= 3 and not set(mots) & (_YES | _NO):
        return REPEAT
    if mots[0] in _NO or (mots[0] in _FILLERS and any(m in _NO for m in mots[:2])):
        # « non », « non, ajoute l'adresse » : refus, avec la suite comme consigne
        reste = re.sub(r"^\W*(euh|bon|alors)?\W*(non|refuse[rz]?|stop|attends?)\b\W*", "", texte.strip(), flags=re.I)
        consigne = reste.strip(" ,.;:!") if len([m for m in _mots(reste) if m not in _FILLERS]) >= 2 else ""
        return Decision(approved=False, reason=consigne)
    if any(m in _YES for m in mots[:3]):
        if set(mots) & _RESERVE:
            # « oui mais change l'heure » : ce n'est pas un accord, c'est une consigne
            consigne = re.split(r"\b(mais|sauf)\b", texte, maxsplit=1, flags=re.I)[-1].strip(" ,.;:!")
            return Decision(approved=False, reason=consigne or texte.strip())
        return Decision(approved=True)
    if len(utiles) >= 3:
        # ni oui ni non, mais une vraie phrase : consigne (comme au clavier, où tout texte = refus avec consigne)
        return Decision(approved=False, reason=texte.strip(" ,.;:!"))
    return None


# ------------------------------------------------------------------ ce que l'agent dit
def date_parlee(iso: str) -> str:
    """'2026-11-19T16:30:00+00:00' -> 'jeudi 19 novembre à 16 h 30'."""
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return str(iso)
    heure = f"{d.hour} h" + (f" {d.minute:02d}" if d.minute else "")
    return f"{_JOURS[d.weekday()]} {d.day} {_MOIS[d.month - 1]} à {heure}"


def _sans_citation(corps: str) -> str:
    """Retire l'historique cité d'une réponse (« Le … a écrit : », lignes « > »)."""
    corps = re.split(r"\n\s*Le .{5,120}a écrit\s*:", corps, maxsplit=1)[0]
    lignes = [l for l in corps.splitlines() if not l.lstrip().startswith(">")]
    return re.sub(r"\s+", " ", " ".join(lignes)).strip()


def annonce_email(email: InboundEmail, max_car: int = 700) -> str:
    nom = re.sub(r"\s*<.*?>", "", email.sender).strip().strip('"') or email.sender
    corps = _sans_citation(email.body)
    if len(corps) > max_car:
        corps = corps[:max_car].rsplit(" ", 1)[0] + "… (suite à l'écran)"
    return f"Nouvel email de {nom}. Objet : {email.subject or 'sans objet'}. {corps}"


def decrire(r: ApprovalRequest) -> str:
    a = r.args
    if r.tool == "envoyer_brouillon":
        try:
            p = json.loads(r.preview or "{}")
        except ValueError:
            p = {"body": r.preview or ""}
        dest = re.sub(r"\s*<.*?>", "", p.get("to", "")).strip() or "le client"
        return (f"L'agent propose d'envoyer cet email à {dest}. Objet : {p.get('subject', '')}. "
                f"{p.get('body', '').strip() or 'Aperçu indisponible : regardez l écran.'}")
    if r.tool == "creer_visite":
        qui = f" avec {a['conseiller']}" if a.get("conseiller") else ""
        ou = f", {a['adresse_bien']}" if a.get("adresse_bien") else ""
        return (f"L'agent propose de créer une visite du bien {a.get('bien_id', '')}{ou}, "
                f"le {date_parlee(a.get('debut', ''))}{qui}, pour {a.get('client_email', 'le client')}.")
    if r.tool == "deplacer_visite":
        return f"L'agent propose de déplacer la visite au {date_parlee(a.get('nouveau_debut', ''))}."
    if r.tool == "annuler_visite":
        return "L'agent propose d'annuler la visite."
    return f"L'agent propose l'action {r.tool.replace('_', ' ')}."


def relecture(d: Decision) -> str:
    if d.approved:
        return "Compris : j'approuve."
    return f"Compris : je refuse, avec la consigne : {d.reason}." if d.reason else "Compris : je refuse."


# ------------------------------------------------------------------ voix, micro, transcription
def parler(texte: str, voix: str = "Thomas", debit: int = 185) -> None:
    print(f"🔊 {texte}")
    if shutil.which("say"):
        subprocess.run(["say", "-v", voix, "-r", str(debit), texte], check=False)


def _bip() -> None:
    son = "/System/Library/Sounds/Tink.aiff"
    if shutil.which("afplay"):
        subprocess.run(["afplay", son], check=False)


def enregistrer(max_secondes: float = 12.0, silence_fin: float = 1.4, attente_max: float = 7.0,
                taux: int = 16000) -> bytes | None:
    """Enregistre jusqu'à un silence après la parole. Renvoie un WAV (mono 16 kHz) ou None si personne n'a parlé."""
    import sounddevice as sd  # import ici : seul le mode --voix en a besoin

    bloc = int(taux * 0.1)
    morceaux, parle, silence, seuil, bruit = [], False, 0.0, None, []
    debut = time.monotonic()
    with sd.RawInputStream(samplerate=taux, channels=1, dtype="int16", blocksize=bloc) as flux:
        while time.monotonic() - debut < max_secondes:
            data, _ = flux.read(bloc)
            data = bytes(data)
            ech = array("h", data)
            niveau = math.sqrt(sum(x * x for x in ech) / max(len(ech), 1))
            if seuil is None:  # 0,3 s pour estimer le bruit de fond
                bruit.append(niveau)
                if len(bruit) >= 3:
                    seuil = max(400.0, 3 * (sum(bruit) / len(bruit)))
                continue
            if niveau > seuil:
                parle, silence = True, 0.0
            elif parle:
                silence += 0.1
            if parle:
                morceaux.append(data)
                if silence >= silence_fin:
                    break
            elif time.monotonic() - debut > attente_max:
                return None
    if not morceaux:
        return None
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(taux)
        w.writeframes(b"".join(morceaux))
    return buf.getvalue()


_PROMPT_STT = ("Transcris mot pour mot, en français, ce que dit la personne dans cet enregistrement. "
               "Réponds uniquement par la transcription, sans guillemets. N'invente et ne devine aucun mot : "
               "une hésitation (« euh », « hum »), un souffle, un bruit ou un silence se transcrivent tels "
               "quels (« euh ») ou par [inaudible]. Si rien n'est clairement prononcé, réponds exactement : [inaudible]")


def transcrire(wav: bytes, settings: Settings) -> str:
    """Transcription par Gemini, avec la même politique d'attente que l'agent en cas de quota (429)."""
    from google import genai
    from google.genai import types

    from .graph import retry_delay

    client = genai.Client(api_key=settings.google_api_key)
    modele = settings.voice_stt_model or settings.llm_model
    for tentative in range(settings.llm_retries + 1):
        try:
            rep = client.models.generate_content(
                model=modele,
                contents=[types.Part.from_bytes(data=wav, mime_type="audio/wav"), _PROMPT_STT],
                config=types.GenerateContentConfig(temperature=0),
            )
            return (rep.text or "").strip()
        except Exception as exc:  # noqa: BLE001 — quota, réseau : on réessaie puis on abandonne
            if tentative >= settings.llm_retries:
                print(f"[voix] transcription impossible : {exc}"[:300])
                return ""
            time.sleep(retry_delay(exc, tentative, settings))
    return ""


# ------------------------------------------------------------------ le conseiller vocal
class ConseillerVocal:
    """Remplace la validation au clavier. `parler` / `ecouter` sont injectables (tests)."""

    def __init__(self, settings: Settings, parler_fn: Callable[[str], None] | None = None,
                 ecouter_fn: Callable[[], str | None] | None = None,
                 clavier_fn: Callable[[list[ApprovalRequest]], dict[str, Decision]] | None = None,
                 essais: int = 2):
        self.settings = settings
        self.parler = parler_fn or (lambda t: parler(t, settings.voice_name))
        self.ecouter = ecouter_fn or self._ecouter_micro
        self.clavier = clavier_fn
        self.essais = essais

    def _ecouter_micro(self) -> str | None:
        _bip()
        wav = enregistrer(max_secondes=self.settings.voice_max_seconds)
        if wav is None:
            return None
        texte = transcrire(wav, self.settings)
        print(f"🎤 « {texte} »")
        return texte

    def annoncer(self, email: InboundEmail) -> None:
        self.parler(annonce_email(email))

    def statut(self, status: str) -> None:
        self.parler({"done": "Traitement terminé.", "escalated": "Demande transmise à un conseiller humain.",
                     "llm_error": "Le modèle ne répond pas ; l'email reste à traiter."}.get(status, ""))

    def decider(self, pending: list[ApprovalRequest]) -> dict[str, Decision]:
        out: dict[str, Decision] = {}
        for r in pending:
            print("\n" + "=" * 70)
            print(f"ACTION À VALIDER : {r.tool}")
            print(f"Paramètres      : {json.dumps(r.args, ensure_ascii=False, indent=2)}")
            if r.preview:
                print(f"Contenu         : {r.preview}")
            out[r.call_id] = self._une(r)
        return out

    def _une(self, r: ApprovalRequest) -> Decision:
        self.parler(decrire(r) + " Vous validez ?")
        ratés = 0
        while ratés < self.essais:
            rep = interpret(self.ecouter())
            if rep == REPEAT:
                self.parler(decrire(r) + " Vous validez ?")
                continue
            if isinstance(rep, Decision):
                self.parler(relecture(rep))
                return rep
            ratés += 1
            if ratés < self.essais:
                self.parler("Je n'ai pas compris. Dites oui, non, ou non suivi d'une consigne.")
        self.parler("Je n'ai toujours pas compris : répondez au clavier.")
        if self.clavier:
            return self.clavier([r])[r.call_id]
        return Decision(approved=False)  # jamais d'accord deviné
