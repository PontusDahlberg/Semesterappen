# Semesterappen

Streamlit-app för semesterplanering med lagring i Google Drive (JSON i en delad mapp).

## Lokalt (Windows)

1. Skapa virtuell miljö

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Installera dependencies

```powershell
pip install -r requirements.txt
```

3. Secrets

- Kopiera mallen och fyll i:

```powershell
Copy-Item .streamlit\secrets.toml.example .streamlit\secrets.toml
```

`drive_folder_id` kan vara antingen själva ID:t eller en hel URL (appen klarar båda).

4. Kör

```powershell
streamlit run app.py
```

## Streamlit Cloud + GitHub

1. Pusha repo:t till GitHub (se instruktioner nedan)
2. I Streamlit Cloud: skapa app och välj GitHub-repo + branch
3. I Streamlit Cloud: App → Settings → Secrets
   - Klistra in innehållet (samma format som i `.streamlit/secrets.toml.example`)

### OAuth (rekommenderas för Drive‑lagring)

Service accounts har ingen egen lagringskvota i "My Drive". För att spara i ditt Google Drive‑konto:

1. Skapa en OAuth Client (Web application) i Google Cloud Console
2. Lägg in client‑uppgifterna i `[gcp_oauth_client]` i secrets
3. Lägg till `oauth_redirect_uri` (din Streamlit‑app URL) i secrets
4. I Google Cloud Console: lägg samma redirect URI under **Authorized redirect URIs**

Tips: om Streamlit säger att formatet är fel kan du validera lokalt (utan att skriva ut hemligheter):

```powershell
python validate_secrets.py .streamlit\secrets.toml
```

## Viktigt om hemligheter

- `.streamlit/secrets.toml` är ignorerad via `.gitignore` och ska aldrig committas.
- `.streamlit/secrets.toml.example` är medvetet committad som mall och ska inte innehålla riktiga hemligheter.
- Om en private key redan har läckt (t.ex. i chat/loggar): rotera nyckeln i Google Cloud Console och ta bort den gamla.
