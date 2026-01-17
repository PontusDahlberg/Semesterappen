import streamlit as st
import pandas as pd
import holidays
import datetime
import plotly.express as px
import json
import re
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io

# --- GOOGLE DRIVE SETUP ---
SCOPES = ['https://www.googleapis.com/auth/drive']

def _extract_drive_folder_id(value: str) -> str:
    if not value:
        return ""
    value = str(value).strip()
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", value)
    if m:
        return m.group(1)
    return value

def _require_secrets():
    missing = []
    if "gcp_service_account" not in st.secrets:
        missing.append("gcp_service_account")
    if "drive_folder_id" not in st.secrets:
        missing.append("drive_folder_id")
    if missing:
        st.error(
            "Saknar secrets: " + ", ".join(missing) + "\n\n"
            "Lösning:\n"
            "- Lokalt: skapa .streamlit/secrets.toml (se .streamlit/secrets.toml.example)\n"
            "- Streamlit Cloud: App → Settings → Secrets, klistra in samma innehåll"
        )
        st.stop()

def _drive_enabled() -> tuple[bool, str]:
    """Returnerar (enabled, folder_id)."""
    try:
        if "gcp_service_account" not in st.secrets:
            return False, ""
        if "drive_folder_id" not in st.secrets:
            return False, ""
        folder_id = _extract_drive_folder_id(st.secrets.get("drive_folder_id", ""))
        if not folder_id:
            return False, ""
        return True, folder_id
    except Exception:
        return False, ""

def get_drive_service():
    # Hämtar credentials från st.secrets
    _require_secrets()
    creds_dict = st.secrets["gcp_service_account"]
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=SCOPES
    )
    return build('drive', 'v3', credentials=creds)

def load_from_drive(filename):
    """Försöker ladda JSON-fil från den delade mappen"""
    try:
        service = get_drive_service()
        folder_id = _extract_drive_folder_id(st.secrets["drive_folder_id"])
        if not folder_id:
            st.error("drive_folder_id är tomt. Lägg in en Drive folder ID (eller URL) i secrets.")
            return None
        
        # Sök efter filen i mappen
        query = f"'{folder_id}' in parents and name = '{filename}' and trashed = false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get('files', [])

        if not files:
            return None # Filen finns inte än
        
        # Ladda ner innehållet
        file_id = files[0]['id']
        request = service.files().get_media(fileId=file_id)
        raw = request.execute()
        return json.loads(raw.decode('utf-8'))
        
    except Exception as e:
        st.error(f"Kunde inte ladda från Drive: {e}")
        return None

def save_to_drive(filename, data_dict):
    """Sparar (överskriver) JSON-filen på Drive"""
    try:
        service = get_drive_service()
        folder_id = _extract_drive_folder_id(st.secrets["drive_folder_id"])
        if not folder_id:
            st.error("drive_folder_id är tomt. Lägg in en Drive folder ID (eller URL) i secrets.")
            return False
        
        # Kolla om filen redan finns
        query = f"'{folder_id}' in parents and name = '{filename}' and trashed = false"
        results = service.files().list(q=query, fields="files(id)").execute()
        files = results.get('files', [])
        
        # Konvertera data till JSON-ström
        json_str = json.dumps(data_dict, indent=2)
        media = MediaIoBaseUpload(io.BytesIO(json_str.encode('utf-8')), 
                                  mimetype='application/json',
                                  resumable=True)

        if files:
            # Uppdatera befintlig
            file_id = files[0]['id']
            service.files().update(fileId=file_id, media_body=media).execute()
        else:
            # Skapa ny
            file_metadata = {'name': filename, 'parents': [folder_id]}
            service.files().create(body=file_metadata, media_body=media).execute()
            
        return True
    except Exception as e:
        st.error(f"Kunde inte spara till Drive: {e}")
        return False

# --- GRUNDINSTÄLLNINGAR ---
START_DATE = datetime.date(2026, 1, 1)
END_DATE = datetime.date(2027, 10, 15)
TOTAL_BUDGET = 108
DB_FILENAME = "semester_databas.json"

st.set_page_config(page_title="Semesterplaneraren (Drive Sync)", layout="wide")

drive_enabled, _folder_id = _drive_enabled()

# --- LOGIK-KLASS (Samma som förut) ---
class VacationEngine:
    def __init__(self):
        self.se_holidays = holidays.SE(years=[2026, 2027])
        
    def is_holiday(self, date_obj):
        return date_obj in self.se_holidays or date_obj.weekday() >= 5

    def get_initial_data(self):
        all_dates = pd.date_range(start=START_DATE, end=END_DATE).to_pydatetime()
        data = []
        for dt in all_dates:
            d = dt.date()
            week_num = d.isocalendar()[1]
            day_type = "Arbetsdag"
            details = ""
            if self.is_holiday(d):
                day_type = "Ledig (Helg/Röd)"
                if d in self.se_holidays:
                    details = self.se_holidays.get(d)
            data.append({
                "Datum": str(d),
                "Vecka": week_num,
                "Typ": day_type,
                "Beskrivning": details,
                "Semester": False
            })
        return pd.DataFrame(data)

engine = VacationEngine()

# --- INITIALISERING VID START ---
if 'scenarios' not in st.session_state:
    if drive_enabled:
        with st.spinner('Synkar med Google Drive...'):
            drive_data = load_from_drive(DB_FILENAME)

            if drive_data:
                st.session_state['scenarios'] = drive_data
                st.toast("Data laddad från Drive!", icon="☁️")
            else:
                # Skapa nytt om inget finns på Drive
                initial_df = engine.get_initial_data()
                st.session_state['scenarios'] = {"Utkast 1": initial_df.to_dict('records')}
                st.toast("Ingen data på Drive, skapade nytt utkast.", icon="🆕")
    else:
        # Lokalt läge (ingen Drive-sync)
        initial_df = engine.get_initial_data()
        st.session_state['scenarios'] = {"Utkast 1": initial_df.to_dict('records')}
            
    # Sätt default scenario
    first_key = list(st.session_state['scenarios'].keys())[0]
    st.session_state['current_scenario'] = first_key

# --- FUNKTIONER ---
def create_new_scenario(name):
    current_data = st.session_state['scenarios'][st.session_state['current_scenario']]
    st.session_state['scenarios'][name] = [row.copy() for row in current_data]
    st.session_state['current_scenario'] = name

def save_all_changes():
    """Wrapper för att spara och visa status"""
    if not drive_enabled:
        st.warning(
            "Drive-sync är inte aktiverad (saknar secrets). \n\n"
            "Lokalt: fyll i .streamlit/secrets.toml (se .streamlit/secrets.toml.example).\n"
            "Streamlit Cloud: App → Settings → Secrets."
        )
        return
    with st.spinner('Sparar till Drive...'):
        success = save_to_drive(DB_FILENAME, st.session_state['scenarios'])
        if success:
            st.toast("Sparat till molnet!", icon="✅")

# --- SIDEBAR ---
with st.sidebar:
    st.header("📂 Versioner")

    if drive_enabled:
        st.success("Drive-sync: Aktiv")
    else:
        st.warning("Drive-sync: Inaktiv (saknar secrets)")
    
    # Spara-knapp
    if st.button("☁️ Spara nu", type="primary", disabled=not drive_enabled):
        save_all_changes()

    st.markdown("---")

    scenario_names = list(st.session_state['scenarios'].keys())
    selected_scenario = st.selectbox(
        "Välj version:", 
        scenario_names, 
        index=scenario_names.index(st.session_state['current_scenario'])
    )
    
    if selected_scenario != st.session_state['current_scenario']:
        st.session_state['current_scenario'] = selected_scenario
        st.rerun()

    new_name = st.text_input("Namn på ny kopia:", placeholder="T.ex. Plan B")
    if st.button("Kopiera version"):
        if new_name and new_name not in st.session_state['scenarios']:
            create_new_scenario(new_name)
            save_all_changes() # Spara direkt när vi skapar ny
            st.rerun()
        elif new_name:
            st.error("Namnet finns redan")

# --- HUVUDVY (Samma logik som förut) ---
st.title(f"Planerar: {st.session_state['current_scenario']}")

current_records = st.session_state['scenarios'][st.session_state['current_scenario']]
df = pd.DataFrame(current_records)
df["Datum"] = pd.to_datetime(df["Datum"]).dt.date

# Inställningar för vyn
col1, col2 = st.columns(2)
with col1:
    block_fridays = st.checkbox("Visa spärr varannan fredag", value=True)

if block_fridays:
    mask = df["Datum"].apply(lambda x: x.weekday() == 4 and x.isocalendar()[1] % 2 == 0)
    df.loc[mask, "Typ"] = "Spärrad (Jobb)"

# Editor
st.subheader("Kalender")
edited_df = st.data_editor(
    df,
    column_config={
        "Semester": st.column_config.CheckboxColumn("Semester", width="small"),
        "Beskrivning": st.column_config.TextColumn("Notering", width="large")
    },
    disabled=["Datum", "Vecka", "Typ"],
    hide_index=True,
    height=500,
    key=f"editor_{st.session_state['current_scenario']}"
)

# Auto-save logic (Uppdatera session state)
save_df = edited_df.copy()
save_df["Datum"] = save_df["Datum"].astype(str)
st.session_state['scenarios'][st.session_state['current_scenario']] = save_df.to_dict('records')

# Statistik & Graf
vacation_days = edited_df[
    (edited_df["Semester"] == True) & 
    (edited_df["Typ"].isin(["Arbetsdag", "Spärrad (Jobb)"]))
]
count = len(vacation_days)
rem = TOTAL_BUDGET - count

st.markdown("---")
c1, c2, c3 = st.columns(3)
c1.metric("Budget", TOTAL_BUDGET)
c2.metric("Planerat", count)
c3.metric("Kvar", rem)

# Graf
viz_df = edited_df.copy()
viz_df["Kategori"] = viz_df.apply(lambda r: "Semester" if r["Semester"] else ("Helg" if "Ledig" in r["Typ"] else ("Spärrad" if "Spärrad" in r["Typ"] else "Jobb")), axis=1)
events = viz_df[viz_df["Kategori"] != "Jobb"].copy()

if not events.empty:
    fig = px.timeline(events, x_start="Datum", x_end="Datum", y="Kategori", color="Kategori",
                      color_discrete_map={"Semester": "#2ECC71", "Helg": "#E74C3C", "Spärrad": "#95A5A6"})
    fig.update_layout(xaxis_range=[START_DATE, END_DATE], height=300)
    st.plotly_chart(fig, use_container_width=True)