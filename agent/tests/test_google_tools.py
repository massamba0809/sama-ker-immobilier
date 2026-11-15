"""Module Google de Baye Serigne (google_integration) testé contre un faux Google en mémoire.

On vérifie le contrat INTERFACES.md et l'intégration avec l'agent (chargement via
GOOGLE_TOOLS_MODULE, parse_inbox, aperçu du brouillon pour la validation)."""
from __future__ import annotations

import base64
import itertools
from datetime import datetime, timedelta
from email import message_from_bytes
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("googleapiclient")

from sama_agent.agent import parse_inbox
from sama_agent.config import Settings
from sama_agent.models import Decision
from sama_agent.tools.local import build_local_tools
from sama_agent.tools.providers import load_google_tools

from .conftest import email
from .fakes import say, tool_call

TZ = ZoneInfo("Africa/Dakar")


# ---------------------------------------------------------------- faux Google
class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _Res:
    def __init__(self, **methods):
        for k, fn in methods.items():
            setattr(self, k, lambda fn=fn, **kw: _Req(lambda: fn(**kw)))


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


class FakeGoogle:
    def __init__(self):
        self.events: dict[str, dict] = {}
        self.drafts: dict[str, dict] = {}
        self.sent: list[dict] = []
        self.inbox: list[dict] = []
        self.labels: list[dict] = [{"id": "L1", "name": "à traiter"}]
        self.msg_labels: dict[str, set] = {}
        self.ids = itertools.count(1)

    # -- calendar
    def _list(self, calendarId, timeMin, timeMax, **_):
        t0, t1 = datetime.fromisoformat(timeMin), datetime.fromisoformat(timeMax)
        items = [e for e in self.events.values()
                 if datetime.fromisoformat(e["start"]["dateTime"]) < t1 and datetime.fromisoformat(e["end"]["dateTime"]) > t0]
        return {"items": items}

    def _insert(self, calendarId, body):
        eid = f"evt{next(self.ids)}"
        self.events[eid] = {**body, "id": eid}
        return self.events[eid]

    def _update(self, calendarId, eventId, body):
        self.events[eventId] = body
        return body

    def calendar(self):
        return type("Cal", (), {"events": lambda _s: _Res(
            list=self._list, insert=self._insert,
            get=lambda calendarId, eventId: dict(self.events[eventId]),
            update=self._update,
            delete=lambda calendarId, eventId: self.events.pop(eventId) and {},
        )})()

    # -- gmail
    def add_email(self, thread, sender, subject, body, html=False):
        mid = f"m{next(self.ids)}"
        part = {"mimeType": "text/html" if html else "text/plain", "body": {"data": _b64(body)}}
        self.inbox.append({"id": mid, "threadId": thread, "snippet": body[:20], "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject},
                        {"name": "Message-ID", "value": f"<{mid}@mail>"}],
            "parts": [part]}})

    def _draft_create(self, userId, body):
        raw = base64.urlsafe_b64decode(body["message"]["raw"])
        msg = message_from_bytes(raw)
        did = f"d{next(self.ids)}"
        # Gmail renvoie un message simple (non multipart) : le corps est dans payload.body
        self.drafts[did] = {"id": did, "message": {"threadId": body["message"].get("threadId"), "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": k, "value": v} for k, v in msg.items()],
            "body": {"data": _b64(msg.get_payload(decode=True).decode())}}}}
        return {"id": did}

    def _label_create(self, userId, body):
        lab = {"id": f"L{next(self.ids)}", "name": body["name"]}
        self.labels.append(lab)
        return lab

    def _modify(self, userId, id, body):
        cur = self.msg_labels.setdefault(id, {"L1"})
        cur |= set(body["addLabelIds"])
        cur -= set(body["removeLabelIds"])
        return {"id": id}

    def label_names(self, mid):
        return {l["name"] for l in self.labels if l["id"] in self.msg_labels.get(mid, {"L1"})}

    def _draft_send(self, userId, body):
        self.sent.append(self.drafts.pop(body["id"]))
        return {"id": f"sent{next(self.ids)}"}

    def gmail(self):
        users = _Res(
            labels=lambda: _Res(list=lambda userId: {"labels": list(self.labels)}, create=self._label_create),
            messages=lambda: _Res(
                modify=self._modify,
                list=lambda userId, labelIds, maxResults=10: {"messages": [{"id": m["id"]} for m in self.inbox][:maxResults]},
                get=lambda userId, id, format=None: next(m for m in self.inbox if m["id"] == id)),
            threads=lambda: _Res(get=lambda userId, id, **_: {"messages": [m for m in self.inbox if m["threadId"] == id]}),
            drafts=lambda: _Res(create=self._draft_create, get=lambda userId, id, format=None: self.drafts[id],
                                send=self._draft_send),
        )
        # labels()/messages()/... sont des méthodes sans argument : on les rend appelables directement
        for name in ("labels", "messages", "threads", "drafts"):
            fn = getattr(users, name)
            setattr(users, name, lambda fn=fn: fn().execute())
        return type("Gm", (), {"users": lambda _s: users})()


@pytest.fixture
def g(monkeypatch):
    from google_integration import google_tools as gt

    fake = FakeGoogle()
    monkeypatch.setattr(gt, "get_calendar_service", fake.calendar)
    monkeypatch.setattr(gt, "get_gmail_service", fake.gmail)
    return gt, fake


def _next_monday(hour=10, minute=0) -> datetime:
    d = datetime.now(TZ).date() + timedelta(days=1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=TZ)


# ---------------------------------------------------------------- contrat
async def test_hybrid_mode_mixes_mock_catalogue_and_real_google(settings):
    from google_integration import google_tools as gt
    from sama_agent.tools.providers import load_tools

    s = settings.model_copy(update={"tools_mode": "hybrid", "google_tools_module": "google_integration.google_tools"})
    tools, world = await load_tools(s)
    by_name = {t.name: t for t in tools}
    assert world is None
    assert by_name["rechercher_biens"].func.__module__ == "sama_agent.tools.mocks"  # catalogue simulé
    assert by_name["creer_visite"].func is gt.creer_visite  # Google réel
    assert "transmettre_a_conseiller" in by_name and "envoyer_brouillon" in by_name


def test_loaded_through_settings_with_policy_names():
    from sama_agent.policy import POLICIES

    tools = load_google_tools(Settings(google_tools_module="google_integration.google_tools", _env_file=None))
    names = {t.name for t in tools}
    assert len(names) == 9 and names <= set(POLICIES)


def test_slots_follow_agency_rules_and_skip_busy(g):
    gt, fake = g
    lundi = _next_monday()
    assert gt.creer_visite("B1", lundi.replace(hour=9).isoformat(), "c@x.sn")["ok"]
    res = gt.lister_creneaux_libres(lundi.date().isoformat(), lundi.date().isoformat())
    assert res["ok"] and res["count"] == len(res["creneaux"]) == 11  # 12 créneaux de 45 min dans 9h-18h, 1 pris
    debuts = [datetime.fromisoformat(c["debut"]) for c in res["creneaux"]]
    assert debuts[0].hour == 9 and debuts[0].minute == 45
    assert all(d + timedelta(minutes=45) <= d.replace(hour=18, minute=0) for d in debuts)
    dimanche = (lundi - timedelta(days=1)).date().isoformat()
    assert gt.lister_creneaux_libres(dimanche, dimanche)["count"] == 0


def test_booking_refuses_taken_slot_and_out_of_hours(g):
    gt, fake = g
    lundi = _next_monday(15, 45)
    first = gt.creer_visite("B1", lundi.isoformat(), "c@x.sn", adresse_bien="12 rue X, Mermoz")
    assert first["ok"] and first["event_id"] in fake.events
    assert fake.events[first["event_id"]]["location"] == "12 rue X, Mermoz"
    assert gt.creer_visite("B2", lundi.isoformat(), "d@x.sn") == {"ok": False, "error": "créneau déjà pris"}
    # rendez-vous posé à la main par un conseiller, hors grille, qui chevauche le créneau de 16h30
    fake._insert("primary", {"start": {"dateTime": lundi.replace(hour=17, minute=0).isoformat()},
                             "end": {"dateTime": lundi.replace(hour=17, minute=30).isoformat()}})
    assert gt.creer_visite("B2", lundi.replace(hour=16, minute=30).isoformat(), "d@x.sn")["error"] == "créneau déjà pris"
    assert not gt.creer_visite("B1", lundi.replace(hour=17, minute=30).isoformat(), "c@x.sn")["ok"]  # finit après 18h
    assert "grille" in gt.creer_visite("B1", lundi.replace(hour=11, minute=0).isoformat(), "c@x.sn")["error"]
    assert not gt.creer_visite("B1", (lundi - timedelta(days=1)).isoformat(), "c@x.sn")["ok"]  # dimanche
    assert len(fake.events) == 2  # la visite + le rendez-vous manuel


def test_move_checks_conflicts_but_not_against_itself(g):
    gt, fake = g
    lundi = _next_monday(9, 45)
    a = gt.creer_visite("B1", lundi.isoformat(), "c@x.sn")["event_id"]
    gt.creer_visite("B2", lundi.replace(hour=12).isoformat(), "d@x.sn")
    assert gt.deplacer_visite(a, lundi.replace(hour=10, minute=30).isoformat())["ok"]
    assert gt.deplacer_visite(a, lundi.replace(hour=12).isoformat())["error"] == "créneau déjà pris"
    assert gt.annuler_visite(a) == {"ok": True, "event_id": a} and a not in fake.events


def test_inbox_returns_real_body_in_agent_format(g):
    gt, fake = g
    fake.add_email("T1", "Awa <awa@x.sn>", "F3 Mermoz", "Bonjour, je cherche un F3 à Mermoz.")
    fake.add_email("T2", "Modou <modou@x.sn>", "Studio", "<p>Un studio<br>au Plateau &amp; pas cher</p>", html=True)
    raw = gt.lire_demandes_non_traitees()
    assert raw["ok"] and raw["count"] == 2
    emails = parse_inbox(raw)
    assert [e.thread_id for e in emails] == ["T1", "T2"]
    assert emails[0].body == "Bonjour, je cherche un F3 à Mermoz." and emails[0].sender.startswith("Awa")
    assert emails[1].body == "Un studio\nau Plateau & pas cher"


def test_draft_roundtrip_keeps_exact_text_and_threading(g):
    gt, fake = g
    fake.add_email("T1", "awa@x.sn", "F3", "Bonjour")
    d = gt.creer_brouillon("T1", "awa@x.sn", "Re: F3", "Bonjour Awa,\nVoici deux biens.")
    assert d["ok"]
    lu = gt.lire_brouillon(d["draft_id"])
    assert lu["ok"] and lu["body"].strip() == "Bonjour Awa,\nVoici deux biens."
    assert lu["thread_id"] == "T1" and lu["to"] == "awa@x.sn" and lu["subject"] == "Re: F3"
    headers = {h["name"]: h["value"] for h in fake.drafts[d["draft_id"]]["message"]["payload"]["headers"]}
    assert headers["In-Reply-To"].startswith("<m")
    assert gt.envoyer_brouillon(d["draft_id"])["ok"] and len(fake.sent) == 1


def test_label_moves_email_between_tracking_labels(g):
    gt, fake = g
    fake.add_email("T1", "awa@x.sn", "F3", "Bonjour")
    mid = fake.inbox[0]["id"]
    assert gt.changer_libelle(mid, "en_attente_validation")["ok"]
    assert fake.label_names(mid) == {"en attente de validation"}  # « à traiter » retiré, libellé créé
    assert gt.changer_libelle(mid, "repondu")["libelle"] == "répondu"
    assert fake.label_names(mid) == {"répondu"}
    assert sum(l["name"] == "répondu" for l in fake.labels) == 1
    assert gt.changer_libelle(mid, "supprime")["ok"] is False


def test_api_errors_become_ok_false(g, monkeypatch):
    gt, _ = g
    monkeypatch.setattr(gt, "get_calendar_service", lambda: (_ for _ in ()).throw(RuntimeError("token.json introuvable")))
    res = gt.lister_creneaux_libres("2026-11-16", "2026-11-16")
    assert res == {"ok": False, "error": "token.json introuvable"}


# ---------------------------------------------------------------- avec l'agent
async def test_agent_send_shows_real_draft_text_and_waits_for_approval(g, make_agent, settings):
    gt, fake = g
    tools = gt.get_google_tools() + build_local_tools(settings.data_path)
    fake.add_email("t1", "client@example.com", "F3", "Bonjour")
    agent, llm, _ = await make_agent([
        tool_call("creer_brouillon", {"thread_id": "t1", "destinataire": "client@example.com",
                                      "objet": "Re: F3", "corps": "Bonjour, voici un bien."}, "c1"),
        tool_call("envoyer_brouillon", {"draft_id": "d2"}, "c2"),
        say("Réponse envoyée."),
    ], tools=tools)
    res = await agent.handle_email(email())
    assert res.status == "pending_approval" and fake.sent == []
    (req,) = res.pending
    assert "Bonjour, voici un bien." in req.preview
    res = await agent.resume("t1", {"c2": Decision(approved=True)})
    assert res.status == "done" and len(fake.sent) == 1
