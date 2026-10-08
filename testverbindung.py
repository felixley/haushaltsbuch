import getpass
import time
import datetime
import webbrowser
import tempfile
import os
from fints.utils import minimal_interactive_cli_bootstrap, decode_phototan_image
from fints.client import FinTS3PinTanClient, NeedTANResponse

# .env laden (Werte aus .env.example-Vorlage eintragen)
from dotenv import load_dotenv
load_dotenv()

bank = os.getenv('BANK', 'comdirect')  # Auswahl: dkb oder comdirect (BANK in .env)

# Zugangsdaten für die gewählte Bank aus der .env lesen
# Die PIN wird bewusst NICHT aus der .env gelesen, sondern
# sicher (ohne Echo) per getpass zur Laufzeit abgefragt.
prefix = bank.upper()
credentials = {
    'blz': os.getenv(f'{prefix}_BLZ'),
    'username': os.getenv(f'{prefix}_USERNAME'),
    'hbci': os.getenv(f'{prefix}_HBCI'),
    'tan_mechanism': os.getenv(f'{prefix}_TAN_MECHANISM'),
}

missing = [key for key, value in credentials.items() if not value]
if missing:
    raise SystemExit(
        f"Fehlende Zugangsdaten für '{bank}' in .env: {', '.join(prefix + '_' + m.upper() for m in missing)}. "
        f"Siehe .env.example"
    )

blz = credentials['blz']
username = credentials['username']
hbci_backend = credentials['hbci']
tan_mechanism = credentials['tan_mechanism']

# PIN verdeckt (ohne Bildschirm-Echo) abfragen
password = getpass.getpass(f"PIN für {bank} ({username}): ")

# Nutzen Sie die funktionierende Produkt-ID
product_id = "6151256F3D4F9975B877BD4A2" 

client = FinTS3PinTanClient(blz,
                            username,
                            password,
                            hbci_backend,
                            product_id=product_id)

# VOR dem Login-Dialog: Verfügbare TAN-Medien laden und das entsprechende Verfahren setzen
client.fetch_tan_mechanisms()
client.set_tan_mechanism(tan_mechanism) 

minimal_interactive_cli_bootstrap(client)

def show_image(image_bytes, mime_type="image/png"):
    """Schreibt die Bilddaten in eine Temp-Datei und öffnet sie im Standard-Bildbetrachter."""
    suffix = "." + mime_type.split("/")[-1].replace("jpeg", "jpg") if "/" in mime_type else ".png"
    path = os.path.join(tempfile.gettempdir(), "fints_phototan" + suffix)
    with open(path, "wb") as writer:
        writer.write(image_bytes)
    print(f"-> Bild gespeichert: {path}")
    webbrowser.open("file://" + path)


def handle_decoupled_tan(f, response):
    print("\n--- photoTAN-Grafik angefordert ---")

    # Fall 1: Bank liefert die Matrix direkt als challenge_matrix = [mime_type, binäre Bilddaten]
    if getattr(response, 'challenge_matrix', None):
        mime_type = response.challenge_matrix[0]
        matrix_data = response.challenge_matrix[1]  # Index 1 = die eigentlichen Bilddaten!
        print(f"Grafik gefunden (MIME: {mime_type}). Öffne Bild...")
        try:
            show_image(matrix_data, mime_type)
            print("-> Bitte scannen Sie die obige Grafik mit Ihrer photoTAN-App!")
        except Exception as e:
            print(f"Fehler beim Rendern der Grafik: {e}")
            print("Alternativer Text-Challenge:", response.challenge)
    # Fall 2: comdirect liefert die Challenge oft im challenge_hhduc (muss dekodiert werden)
    elif getattr(response, 'challenge_hhduc', None):
        print("challenge_hhduc gefunden. Dekodiere Challenge...")
        try:
            decoded = decode_phototan_image(response.challenge_hhduc)
            print(f"Dekodiert (MIME: {decoded['mime_type']}). Öffne Bild...")
            show_image(decoded['image'], decoded['mime_type'])
            print("-> Bitte scannen Sie die obige Grafik mit Ihrer photoTAN-App!")
        except Exception as e:
            print(f"Fehler beim Dekodieren der hhduc-Challenge: {e}")
            print("Alternativer Text-Challenge:", response.challenge)
    else:
        # Fallback, falls nur Text geliefert wird
        print(response.challenge)
    
    # TAN vom Benutzer abfragen statt leerem String
    tan_code = input("\nBitte geben Sie die in der photoTAN-App angezeigte TAN ein: ").strip()

    print("Sende Bestätigung an die comdirect...")
    response = f.send_tan(response, tan_code)
    return response


with client:
    # Erster Login-Schritt (SCA / 90-Tage-Freigabe)
    if isinstance(client.init_tan_response, NeedTANResponse):
        client.init_tan_response = handle_decoupled_tan(client, client.init_tan_response)

    # Konten abrufen
    accounts = client.get_sepa_accounts()
    for account in accounts:
        print(f"Lese Umsätze für {account.iban}...")
        result = client.get_transactions(account,
                                         start_date=datetime.datetime.now() - datetime.timedelta(days=100),
                                         end_date=datetime.datetime.now())
        
        # Falls für den Umsatzabruf erneut eine Freigabe nötig ist
        if isinstance(result, NeedTANResponse):
            result = handle_decoupled_tan(client, result)
            
        print(f"Umsätze erfolgreich empfangen: {len(result)} Transaktionen für {account.iban}")

# Ausgabe der Rohdaten der ersten Transaktion im Jupyter Interactive Window
if 'result' in locals() and len(result) > 0:
    print("\nRohdaten der ersten Transaktion:")
    print(result[0].data)
else:
    print("\nKeine Transaktionen gefunden oder Abruf fehlgeschlagen.")