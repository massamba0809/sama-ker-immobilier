import os
import base64
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

# --- CONFIGURATION DES CHEMINS ---
# Permet de trouver token.json même si le script est lancé depuis le dossier "agent/"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(BASE_DIR, 'token.json')
CREDENTIALS_PATH = os.path.join(BASE_DIR, 'credentials.json')

SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.compose',
    'https://www.googleapis.com/auth/calendar'
]

def get_credentials():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    return creds

def get_calendar_service():
    return build('calendar', 'v3', credentials=get_credentials())

def get_gmail_service():
    return build('gmail', 'v1', credentials=get_credentials())

def get_label_id(service, label_name):
    labels = service.users().labels().list(userId='me').execute().get('labels', [])
    for label in labels:
        if label['name'] == label_name:
            return label['id']
    return None

# --- SCHÉMAS D'ARGUMENTS POUR L'AGENT (CONFORMES À INTERFACES.MD) ---

class ListerCreneauxInput(BaseModel):
    date_debut: str = Field(description="Date de début au format ISO 8601 (ex: 2026-11-16T09:00:00Z)")
    date_fin: str = Field(description="Date de fin au format ISO 8601")
    conseiller: Optional[str] = Field(None, description="Nom ou email du conseiller (optionnel)")

class CreerVisiteInput(BaseModel):
    bien_id: str = Field(description="L'ID du bien immobilier (ex: MOCK-001)")
    debut: str = Field(description="Date et heure de début au format ISO 8601")
    client_email: str = Field(description="Email du client")
    conseiller: Optional[str] = Field(None, description="Nom du conseiller (optionnel)")

class DeplacerVisiteInput(BaseModel):
    event_id: str = Field(description="ID de l'événement à déplacer")
    nouveau_debut: str = Field(description="Nouvelle date et heure au format ISO 8601")

class AnnulerVisiteInput(BaseModel):
    event_id: str = Field(description="ID de l'événement à annuler")

class CreerBrouillonInput(BaseModel):
    thread_id: str = Field(description="ID du fil de discussion Gmail")
    destinataire: str = Field(description="Adresse email du destinataire")
    objet: str = Field(description="Sujet de l'email")
    corps: str = Field(description="Contenu du message")

class LireBrouillonInput(BaseModel):
    draft_id: str = Field(description="ID du brouillon à lire")

class EnvoyerBrouillonInput(BaseModel):
    draft_id: str = Field(description="ID du brouillon à envoyer")

# --- FONCTIONS RÉELLES (RETOUR OK / ERROR) ---

def lister_creneaux_libres(date_debut: str, date_fin: str, conseiller: str = None) -> dict:
    service = get_calendar_service()
    try:
        debut_dt = datetime.fromisoformat(date_debut.replace('Z', '+00:00'))
        fin_dt = datetime.fromisoformat(date_fin.replace('Z', '+00:00'))
        
        events_result = service.events().list(
            calendarId='primary',
            timeMin=debut_dt.isoformat(),
            timeMax=fin_dt.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        occupes = []
        for ev in events:
            start = ev['start'].get('dateTime', ev['start'].get('date'))
            end = ev['end'].get('dateTime', ev['end'].get('date'))
            occupes.append((datetime.fromisoformat(start.replace('Z', '+00:00')), 
                            datetime.fromisoformat(end.replace('Z', '+00:00'))))
            
        creneaux = []
        heure_actuelle = debut_dt
        duree = timedelta(minutes=45)
        
        while heure_actuelle + duree <= fin_dt:
            fin_creneau = heure_actuelle + duree
            est_libre = all(not (heure_actuelle < fin_occupe and fin_creneau > debut_occupe) for debut_occupe, fin_occupe in occupes)
            
            # Règle métier : Lundi-Samedi (weekday < 6), 9h-18h
            if est_libre and heure_actuelle.weekday() < 6 and 9 <= heure_actuelle.hour < 18:
                creneaux.append({
                    "debut": heure_actuelle.isoformat(),
                    "fin": fin_creneau.isoformat(),
                    "conseiller": conseiller or "Par défaut"
                })
            heure_actuelle = fin_creneau
            
        return {"ok": True, "creneaux": creneaux, "count": len(creneaux)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def creer_visite(bien_id: str, debut: str, client_email: str, conseiller: str = None) -> dict:
    service = get_calendar_service()
    try:
        debut_dt = datetime.fromisoformat(debut.replace('Z', '+00:00'))
        fin_dt = debut_dt + timedelta(minutes=45)
        
        event_body = {
            'summary': f"Visite - Bien {bien_id} (Conseiller : {conseiller or 'Non assigné'})",
            'location': f"Adresse du bien {bien_id}",
            'description': f"Client : {client_email}",
            'start': {'dateTime': debut_dt.isoformat(), 'timeZone': 'Africa/Dakar'},
            'end': {'dateTime': fin_dt.isoformat(), 'timeZone': 'Africa/Dakar'},
        }
        
        event = service.events().insert(calendarId='primary', body=event_body).execute()
        return {"ok": True, "event_id": event.get('id')}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def deplacer_visite(event_id: str, nouveau_debut: str) -> dict:
    service = get_calendar_service()
    try:
        event = service.events().get(calendarId='primary', eventId=event_id).execute()
        debut_dt = datetime.fromisoformat(nouveau_debut.replace('Z', '+00:00'))
        fin_dt = debut_dt + timedelta(minutes=45)
        
        event['start'] = {'dateTime': debut_dt.isoformat(), 'timeZone': 'Africa/Dakar'}
        event['end'] = {'dateTime': fin_dt.isoformat(), 'timeZone': 'Africa/Dakar'}
        
        updated_event = service.events().update(calendarId='primary', eventId=event_id, body=event).execute()
        return {"ok": True, "event_id": updated_event.get('id')}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def annuler_visite(event_id: str) -> dict:
    service = get_calendar_service()
    try:
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        return {"ok": True, "event_id": event_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def lire_demandes_non_traitees() -> list:
    service = get_gmail_service()
    label_id = get_label_id(service, "à traiter")
    if not label_id:
        return [{"error": "Libellé introuvable"}]
        
    try:
        results = service.users().messages().list(userId='me', labelIds=[label_id]).execute()
        messages = results.get('messages', [])
        
        demandes = []
        for m in messages:
            msg = service.users().messages().get(userId='me', id=m['id'], format='full').execute()
            headers = msg['payload']['headers']
            demandes.append({
                "thread_id": msg['threadId'],
                "message_id": msg['id'],
                "expediteur": next((h['value'] for h in headers if h['name'] == 'From'), ""),
                "objet": next((h['value'] for h in headers if h['name'] == 'Subject'), ""),
                "corps": "Donnée non fiable"
            })
        return demandes
    except Exception as e:
        return [{"error": str(e)}]

def creer_brouillon(thread_id: str, destinataire: str, objet: str, corps: str) -> dict:
    service = get_gmail_service()
    message = EmailMessage()
    message.set_content(corps)
    message['To'] = destinataire
    message['Subject'] = objet
    
    encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    create_message = {'message': {'raw': encoded_message, 'threadId': thread_id}}
    
    try:
        draft = service.users().drafts().create(userId='me', body=create_message).execute()
        return {"ok": True, "draft_id": draft['id']}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def lire_brouillon(draft_id: str) -> dict:
    service = get_gmail_service()
    try:
        draft = service.users().drafts().get(userId='me', id=draft_id, format='full').execute()
        msg = draft['message']
        headers = msg['payload']['headers']
        
        corps = ""
        if 'parts' in msg['payload']:
            for part in msg['payload']['parts']:
                if part['mimeType'] == 'text/plain':
                    corps = base64.urlsafe_b64decode(part['body'].get('data', '')).decode('utf-8')
        
        return {
            "ok": True,
            "thread_id": msg['threadId'],
            "to": next((h['value'] for h in headers if h['name'] == 'To'), ""),
            "subject": next((h['value'] for h in headers if h['name'] == 'Subject'), ""),
            "body": corps
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

def envoyer_brouillon(draft_id: str) -> dict:
    service = get_gmail_service()
    try:
        sent = service.users().drafts().send(userId='me', body={'id': draft_id}).execute()
        return {"ok": True, "message_id": sent['id']}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# --- EXPOSITION POUR L'AGENT ---

def get_google_tools() -> list:
    return [
        StructuredTool.from_function(
            func=lister_creneaux_libres, name="lister_creneaux_libres",
            description="Liste les créneaux libres de 45 min.", args_schema=ListerCreneauxInput
        ),
        StructuredTool.from_function(
            func=creer_visite, name="creer_visite",
            description="Crée un événement de visite.", args_schema=CreerVisiteInput
        ),
        StructuredTool.from_function(
            func=deplacer_visite, name="deplacer_visite",
            description="Déplace une visite.", args_schema=DeplacerVisiteInput
        ),
        StructuredTool.from_function(
            func=annuler_visite, name="annuler_visite",
            description="Annule une visite.", args_schema=AnnulerVisiteInput
        ),
        StructuredTool.from_function(
            func=lire_demandes_non_traitees, name="lire_demandes_non_traitees",
            description="Lit les emails 'à traiter'."
        ),
        StructuredTool.from_function(
            func=creer_brouillon, name="creer_brouillon",
            description="Crée un brouillon.", args_schema=CreerBrouillonInput
        ),
        StructuredTool.from_function(
            func=lire_brouillon, name="lire_brouillon",
            description="Lit un brouillon.", args_schema=LireBrouillonInput
        ),
        StructuredTool.from_function(
            func=envoyer_brouillon, name="envoyer_brouillon",
            description="Envoie un brouillon.", args_schema=EnvoyerBrouillonInput
        )
    ]