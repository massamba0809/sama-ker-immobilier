import os.path
import webbrowser
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.compose',
    'https://www.googleapis.com/auth/calendar'
]

# Récupère le dossier où se trouve auth.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(BASE_DIR, 'token.json')
CREDENTIALS_PATH = os.path.join(BASE_DIR, 'credentials.json')

# Configuration Brave
chemin_brave = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
if os.path.exists(chemin_brave):
    webbrowser.register('brave', None, webbrowser.BackgroundBrowser(chemin_brave))

def main():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            flow.redirect_uri = 'http://127.0.0.1:8080/'
            try:
                creds = flow.run_local_server(port=8080, browser='brave')
            except:
                creds = flow.run_local_server(port=8080)
            
        with open(TOKEN_PATH, 'w') as token:
            token.write(creds.to_json())
            
    print("\n✅ Authentification réussie ! Le fichier token.json a été créé.")

if __name__ == '__main__':
    main()