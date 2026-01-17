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

## Viktigt om hemligheter

- `.streamlit/secrets.toml` är ignorerad via `.gitignore` och ska aldrig committas.
- Om en private key redan har läckt (t.ex. i chat/loggar): rotera nyckeln i Google Cloud Console och ta bort den gamla.
