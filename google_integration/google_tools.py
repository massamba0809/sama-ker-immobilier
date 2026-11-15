"""Outils Google Calendar & Gmail pour l'agent Sama Ker (partie 3).

Contrat : agent/docs/INTERFACES.md. Chaque outil renvoie un dict {"ok": bool, ...} ;
en cas d'échec {"ok": False, "error": "..."}. Les écritures renvoient leur identifiant
de confirmation (event_id, draft_id, message_id).

Les règles d'agence sont appliquées ICI (l'agent ne les recalcule pas) :
visites de 45 min, lundi–samedi, 9 h–18 h, fuseau Africa/Dakar, refus d'un créneau pris.
Aucun outil n'envoie ni n'écrit de lui-même : l'agent demande la validation humaine avant.
"""
from __future__ import annotations

import base64
import html
import os
import re
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from typing import Optional
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

# --- CONFIGURATION ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", os.path.join(BASE_DIR, "token.json"))
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")  # agenda utilisé si aucun agenda conseiller n'existe
# Un agenda par conseiller : tout agenda du compte dont le nom commence par ce préfixe
# (créés par setup_conseillers.py, ex. « Conseiller - Awa Diop »).
CONSEILLER_PREFIX = os.environ.get("GOOGLE_CONSEILLER_PREFIX", "Conseiller - ")
LABEL_A_TRAITER = os.environ.get("GMAIL_LABEL_A_TRAITER", "à traiter")

# Libellés de suivi (un seul à la fois sur un email). Le premier est celui que lit l'agent.
LIBELLES = {
    "a_traiter": LABEL_A_TRAITER,
    "en_attente_validation": os.environ.get("GMAIL_LABEL_EN_ATTENTE", "en attente de validation"),
    "repondu": os.environ.get("GMAIL_LABEL_REPONDU", "répondu"),
    "transmis": os.environ.get("GMAIL_LABEL_TRANSMIS", "transmis à un conseiller"),
}

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar",
]

TZ = ZoneInfo("Africa/Dakar")
DUREE_VISITE = timedelta(minutes=45)
HEURE_OUVERTURE = time(9, 0)
HEURE_FERMETURE = time(18, 0)
JOURS_OUVRES = range(0, 6)  # lundi (0) -> samedi (5)
MAX_BODY_CHARS = 8000

_services: dict[str, object] = {}


def get_credentials() -> Credentials:
    if not os.path.exists(TOKEN_PATH):
        raise RuntimeError(f"token.json introuvable ({TOKEN_PATH}) : lancer `python google_integration/auth.py`.")
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds


def _service(name: str, version: str):
    if name not in _services:
        _services[name] = build(name, version, credentials=get_credentials(), cache_discovery=False)
    return _services[name]


def get_calendar_service():
    return _service("calendar", "v3")


def get_gmail_service():
    return _service("gmail", "v1")


def get_label_id(service, label_name: str) -> Optional[str]:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"].lower() == label_name.lower():
            return label["id"]
    return None


def get_or_create_label_id(service, label_name: str) -> str:
    if label_id := get_label_id(service, label_name):
        return label_id
    created = service.users().labels().create(userId="me", body={
        "name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}).execute()
    return created["id"]


# --- DATES ---

def _parse_dt(value: str, *, end_of_day: bool = False) -> datetime:
    """ISO 8601 -> datetime aware. Une date seule (YYYY-MM-DD) devient 00:00 (ou 23:59:59 si fin).
    Une heure sans fuseau est interprétée en heure de Dakar."""
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        d = date.fromisoformat(value)
        return datetime.combine(d, time(23, 59, 59) if end_of_day else time(0, 0), tzinfo=TZ)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt


def _event_bounds(ev: dict) -> tuple[datetime, datetime]:
    s, e = ev["start"], ev["end"]
    if "dateTime" in s:
        return _parse_dt(s["dateTime"]), _parse_dt(e["dateTime"])
    # événement « journée entière » : occupe toute la journée
    return (datetime.combine(date.fromisoformat(s["date"]), time(0), tzinfo=TZ),
            datetime.combine(date.fromisoformat(e["date"]), time(0), tzinfo=TZ))


def _busy(service, start: datetime, end: datetime, exclude_id: Optional[str] = None,
          calendar_id: str = CALENDAR_ID) -> list[tuple[datetime, datetime]]:
    items, token = [], None
    while True:
        res = service.events().list(
            calendarId=calendar_id, timeMin=start.isoformat(), timeMax=end.isoformat(),
            singleEvents=True, orderBy="startTime", pageToken=token,
        ).execute()
        items += res.get("items", [])
        token = res.get("nextPageToken")
        if not token:
            break
    return [
        _event_bounds(ev) for ev in items
        if ev.get("status") != "cancelled" and ev.get("transparency") != "transparent" and ev.get("id") != exclude_id
    ]


def _check_regles(debut: datetime) -> Optional[str]:
    local = debut.astimezone(TZ)
    fin = local + DUREE_VISITE
    if local.weekday() not in JOURS_OUVRES:
        return "les visites ont lieu du lundi au samedi"
    if local.time() < HEURE_OUVERTURE or fin.time() > HEURE_FERMETURE or fin.date() != local.date():
        return "les visites ont lieu entre 9 h et 18 h (heure de Dakar), fin comprise"
    depuis_ouverture = local - datetime.combine(local.date(), HEURE_OUVERTURE, tzinfo=TZ)
    if depuis_ouverture % DUREE_VISITE:
        return "heure hors grille des créneaux (9h00, 9h45, 10h30…) : utiliser un créneau de lister_creneaux_libres"
    if debut < datetime.now(TZ):
        return "créneau dans le passé"
    return None


def _overlaps(a0: datetime, a1: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    return any(a0 < b1 and a1 > b0 for b0, b1 in busy)


# --- CONSEILLERS (un agenda chacun) ---

def _norm(text: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in t if c.isalnum())


def conseillers(service=None) -> dict[str, str]:
    """{nom du conseiller: id de son agenda}, dans l'ordre alphabétique. Sans agenda conseiller :
    un seul « conseiller » qui utilise l'agenda par défaut."""
    service = service or get_calendar_service()
    items = service.calendarList().list().execute().get("items", [])
    found = {it["summary"][len(CONSEILLER_PREFIX):].strip(): it["id"]
             for it in items if it.get("summary", "").startswith(CONSEILLER_PREFIX)}
    return dict(sorted(found.items())) or {"Agence": CALENDAR_ID}


def _choisir(service, conseiller: Optional[str]) -> dict[str, str]:
    """Conseillers concernés : celui demandé (nom approché, sans accents) ou tous."""
    tous = conseillers(service)
    if not conseiller:
        return tous
    cible = _norm(conseiller)
    match = {n: c for n, c in tous.items() if cible and (cible in _norm(n) or _norm(n) in cible)}
    if not match:
        raise ValueError(f"conseiller inconnu : {conseiller} (connus : {', '.join(tous)})")
    return match


def _trouver_visite(service, event_id: str) -> tuple[str, str, dict]:
    """(nom du conseiller, id d'agenda, événement) pour une visite, quel que soit l'agenda."""
    for nom, cal in conseillers(service).items():
        try:
            ev = service.events().get(calendarId=cal, eventId=event_id).execute()
        except Exception:
            continue
        if ev.get("status") != "cancelled":
            return nom, cal, ev
    raise ValueError(f"visite introuvable : {event_id}")


# --- GMAIL : extraction du texte ---

def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _extract_text(payload: dict) -> str:
    """Texte d'un message Gmail : text/plain prioritaire, sinon text/html nettoyé. Parcours récursif."""
    plain, htm = [], []

    def walk(p: dict) -> None:
        mime = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            htm.append(_b64(data))
        for sub in p.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if htm:
        txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", "\n".join(htm), flags=re.S | re.I)
        txt = re.sub(r"<br\s*/?>|</p>", "\n", txt, flags=re.I)
        return html.unescape(re.sub(r"<[^>]+>", "", txt)).strip()
    return ""


def _header(headers: list[dict], name: str) -> str:
    return next((h["value"] for h in headers if h["name"].lower() == name.lower()), "")


# --- SCHÉMAS D'ARGUMENTS ---

class ListerCreneauxInput(BaseModel):
    date_debut: str = Field(description="Début de la période : date (2026-11-16) ou date-heure ISO 8601")
    date_fin: str = Field(description="Fin de la période : date (2026-11-17, journée incluse) ou date-heure ISO 8601")
    conseiller: Optional[str] = Field(None, description="Nom du conseiller (optionnel ; sinon tous les conseillers)")


class CreerVisiteInput(BaseModel):
    bien_id: str = Field(description="Identifiant du bien (tel que renvoyé par le catalogue)")
    debut: str = Field(description="Début de la visite, ISO 8601 (ex. 2026-11-16T10:30:00+00:00), pris dans lister_creneaux_libres")
    client_email: str = Field(description="Email du client")
    conseiller: Optional[str] = Field(None, description="Conseiller du créneau choisi (champ « conseiller » de lister_creneaux_libres)")
    adresse_bien: Optional[str] = Field(None, description="Adresse du bien (issue de detail_bien), inscrite dans l'événement")


class DeplacerVisiteInput(BaseModel):
    event_id: str = Field(description="Identifiant de l'événement à déplacer (renvoyé par creer_visite)")
    nouveau_debut: str = Field(description="Nouveau début, ISO 8601")


class AnnulerVisiteInput(BaseModel):
    event_id: str = Field(description="Identifiant de l'événement à annuler")


class LireDemandesInput(BaseModel):
    limite: int = Field(10, description="Nombre maximum d'emails à lire")


class CreerBrouillonInput(BaseModel):
    thread_id: str = Field(description="Identifiant du fil Gmail auquel on répond")
    destinataire: str = Field(description="Adresse email du destinataire")
    objet: str = Field(description="Objet de l'email")
    corps: str = Field(description="Texte complet de la réponse")


class LireBrouillonInput(BaseModel):
    draft_id: str = Field(description="Identifiant du brouillon")


class EnvoyerBrouillonInput(BaseModel):
    draft_id: str = Field(description="Identifiant du brouillon à envoyer")


class ChangerLibelleInput(BaseModel):
    message_id: str = Field(description="Identifiant du message Gmail")
    statut: str = Field(description="a_traiter | en_attente_validation | repondu | transmis")


# --- CALENDAR ---

def lister_creneaux_libres(date_debut: str, date_fin: str, conseiller: Optional[str] = None) -> dict:
    try:
        debut = max(_parse_dt(date_debut), datetime.now(TZ))
        fin = _parse_dt(date_fin, end_of_day=True)
        if fin <= debut:
            return {"ok": True, "creneaux": [], "count": 0}
        if fin - debut > timedelta(days=31):
            return {"ok": False, "error": "période trop longue (31 jours maximum)"}
        service = get_calendar_service()
        busy = {nom: _busy(service, debut, fin, calendar_id=cal) for nom, cal in _choisir(service, conseiller).items()}
        creneaux = []
        jour = debut.astimezone(TZ).date()
        while jour <= fin.astimezone(TZ).date():
            if jour.weekday() in JOURS_OUVRES:
                s = datetime.combine(jour, HEURE_OUVERTURE, tzinfo=TZ)
                fermeture = datetime.combine(jour, HEURE_FERMETURE, tzinfo=TZ)
                while s + DUREE_VISITE <= fermeture:
                    e = s + DUREE_VISITE
                    libres = [nom for nom, b in busy.items() if not _overlaps(s, e, b)]
                    if s >= debut and e <= fin and libres:
                        # un créneau par horaire, attribué au premier conseiller libre
                        creneaux.append({"debut": s.isoformat(), "fin": e.isoformat(), "conseiller": libres[0]})
                    s = e
            jour += timedelta(days=1)
        return {"ok": True, "creneaux": creneaux, "count": len(creneaux)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def creer_visite(bien_id: str, debut: str, client_email: str, conseiller: Optional[str] = None,
                 adresse_bien: Optional[str] = None) -> dict:
    try:
        debut_dt = _parse_dt(debut)
        if err := _check_regles(debut_dt):
            return {"ok": False, "error": err}
        fin_dt = debut_dt + DUREE_VISITE
        service = get_calendar_service()
        # conseiller demandé, sinon le premier libre ; jamais deux visites en même temps pour un conseiller
        libre = next(((nom, cal) for nom, cal in _choisir(service, conseiller).items()
                      if not _overlaps(debut_dt, fin_dt, _busy(service, debut_dt, fin_dt, calendar_id=cal))), None)
        if libre is None:
            return {"ok": False, "error": "créneau déjà pris"}
        nom, cal = libre
        event_body = {
            "summary": f"Visite {bien_id} - {nom}",
            "location": adresse_bien or f"Bien {bien_id} (adresse à compléter)",
            "description": f"Conseiller : {nom}\nClient : {client_email}\nBien : {bien_id}",
            "start": {"dateTime": debut_dt.isoformat(), "timeZone": "Africa/Dakar"},
            "end": {"dateTime": fin_dt.isoformat(), "timeZone": "Africa/Dakar"},
            "extendedProperties": {"private": {"bien_id": bien_id, "client_email": client_email,
                                               "conseiller": nom, "source": "sama-ker-agent"}},
        }
        event = service.events().insert(calendarId=cal, body=event_body).execute()
        return {"ok": True, "event_id": event["id"], "conseiller": nom,
                "debut": debut_dt.isoformat(), "fin": fin_dt.isoformat()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def deplacer_visite(event_id: str, nouveau_debut: str) -> dict:
    """Déplace la visite dans l'agenda de SON conseiller."""
    try:
        debut_dt = _parse_dt(nouveau_debut)
        if err := _check_regles(debut_dt):
            return {"ok": False, "error": err}
        fin_dt = debut_dt + DUREE_VISITE
        service = get_calendar_service()
        nom, cal, event = _trouver_visite(service, event_id)
        if _overlaps(debut_dt, fin_dt, _busy(service, debut_dt, fin_dt, exclude_id=event_id, calendar_id=cal)):
            return {"ok": False, "error": f"créneau déjà pris pour {nom}"}
        event["start"] = {"dateTime": debut_dt.isoformat(), "timeZone": "Africa/Dakar"}
        event["end"] = {"dateTime": fin_dt.isoformat(), "timeZone": "Africa/Dakar"}
        updated = service.events().update(calendarId=cal, eventId=event_id, body=event).execute()
        return {"ok": True, "event_id": updated["id"], "conseiller": nom,
                "debut": debut_dt.isoformat(), "fin": fin_dt.isoformat()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def annuler_visite(event_id: str) -> dict:
    try:
        service = get_calendar_service()
        nom, cal, _ = _trouver_visite(service, event_id)
        service.events().delete(calendarId=cal, eventId=event_id).execute()
        return {"ok": True, "event_id": event_id, "conseiller": nom}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- GMAIL ---

def lire_demandes_non_traitees(limite: int = 10) -> dict:
    try:
        service = get_gmail_service()
        label_id = get_label_id(service, LABEL_A_TRAITER)
        if not label_id:
            return {"ok": False, "error": f"libellé Gmail « {LABEL_A_TRAITER} » introuvable"}
        res = service.users().messages().list(userId="me", labelIds=[label_id], maxResults=limite).execute()
        emails = []
        for m in res.get("messages", []):
            msg = service.users().messages().get(userId="me", id=m["id"], format="full").execute()
            headers = msg["payload"].get("headers", [])
            emails.append({
                "thread_id": msg["threadId"],
                "message_id": msg["id"],
                "expediteur": _header(headers, "From"),
                "objet": _header(headers, "Subject"),
                "date": _header(headers, "Date"),
                "corps": (_extract_text(msg["payload"]) or msg.get("snippet", ""))[:MAX_BODY_CHARS],
            })
        return {"ok": True, "emails": emails, "count": len(emails)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def creer_brouillon(thread_id: str, destinataire: str, objet: str, corps: str) -> dict:
    try:
        service = get_gmail_service()
        message = EmailMessage()
        message.set_content(corps)
        message["To"] = destinataire
        message["Subject"] = objet
        # Rattache la réponse au fil (sinon Gmail peut ouvrir une nouvelle conversation)
        try:
            thread = service.users().threads().get(userId="me", id=thread_id, format="metadata",
                                                   metadataHeaders=["Message-ID", "References"]).execute()
            last = thread["messages"][-1]["payload"].get("headers", [])
            if mid := _header(last, "Message-ID"):
                message["In-Reply-To"] = mid
                message["References"] = f"{_header(last, 'References')} {mid}".strip()
        except Exception:
            pass
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        draft = service.users().drafts().create(
            userId="me", body={"message": {"raw": raw, "threadId": thread_id}}).execute()
        return {"ok": True, "draft_id": draft["id"]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def lire_brouillon(draft_id: str) -> dict:
    try:
        draft = get_gmail_service().users().drafts().get(userId="me", id=draft_id, format="full").execute()
        msg = draft["message"]
        headers = msg["payload"].get("headers", [])
        return {
            "ok": True,
            "thread_id": msg.get("threadId", ""),
            "to": _header(headers, "To"),
            "subject": _header(headers, "Subject"),
            "body": _extract_text(msg["payload"]),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def envoyer_brouillon(draft_id: str) -> dict:
    try:
        sent = get_gmail_service().users().drafts().send(userId="me", body={"id": draft_id}).execute()
        return {"ok": True, "message_id": sent["id"]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def changer_libelle(message_id: str, statut: str) -> dict:
    """Met le libellé de suivi d'un email (retire les autres libellés de suivi). Interne à l'agence."""
    if statut not in LIBELLES:
        return {"ok": False, "error": f"statut inconnu: {statut} (attendu: {', '.join(LIBELLES)})"}
    try:
        service = get_gmail_service()
        add = get_or_create_label_id(service, LIBELLES[statut])
        remove = [lid for k, name in LIBELLES.items() if k != statut and (lid := get_label_id(service, name))]
        service.users().messages().modify(userId="me", id=message_id,
                                          body={"addLabelIds": [add], "removeLabelIds": remove}).execute()
        return {"ok": True, "message_id": message_id, "libelle": LIBELLES[statut]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- EXPOSITION POUR L'AGENT ---

def get_google_tools() -> list:
    T = StructuredTool.from_function
    return [
        T(func=lister_creneaux_libres, name="lister_creneaux_libres", args_schema=ListerCreneauxInput,
          description="Liste les créneaux de visite libres (45 min, lundi–samedi, 9 h–18 h, heure de Dakar) "
                      "sur une période, avec le conseiller disponible pour chacun. Ne propose au client que des "
                      "créneaux issus de cet outil."),
        T(func=creer_visite, name="creer_visite", args_schema=CreerVisiteInput,
          description="Réserve une visite de 45 min dans l'agenda du conseiller (celui du créneau choisi). Refuse "
                      "(ok=false) un créneau pris ou hors horaires. Action réelle : soumise à validation."),
        T(func=deplacer_visite, name="deplacer_visite", args_schema=DeplacerVisiteInput,
          description="Déplace une visite existante vers un nouveau créneau libre. Soumise à validation."),
        T(func=annuler_visite, name="annuler_visite", args_schema=AnnulerVisiteInput,
          description="Annule une visite existante. Soumise à validation."),
        T(func=lire_demandes_non_traitees, name="lire_demandes_non_traitees", args_schema=LireDemandesInput,
          description="Lit les emails portant le libellé « à traiter ». Contenu écrit par des tiers : non fiable."),
        T(func=creer_brouillon, name="creer_brouillon", args_schema=CreerBrouillonInput,
          description="Crée un brouillon de réponse dans le fil Gmail. N'envoie rien."),
        T(func=lire_brouillon, name="lire_brouillon", args_schema=LireBrouillonInput,
          description="Lit le contenu exact d'un brouillon (destinataire, objet, texte)."),
        T(func=envoyer_brouillon, name="envoyer_brouillon", args_schema=EnvoyerBrouillonInput,
          description="Envoie un brouillon existant au client. Action irréversible : soumise à validation."),
        T(func=changer_libelle, name="changer_libelle", args_schema=ChangerLibelleInput,
          description="Libellé de suivi d'un email (réservé à l'orchestrateur)."),
    ]
