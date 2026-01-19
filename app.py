import streamlit as st
import pandas as pd
import holidays
import datetime
import plotly.express as px
import json
import re
import calendar
import os
from collections.abc import Mapping
from openai import OpenAI
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io

# --- GOOGLE DRIVE SETUP ---
SCOPES = ["https://www.googleapis.com/auth/drive"]
MONTH_NAMES = [
    "Januari",
    "Februari",
    "Mars",
    "April",
    "Maj",
    "Juni",
    "Juli",
    "Augusti",
    "September",
    "Oktober",
    "November",
    "December",
]


def _extract_drive_folder_id(value: str) -> str:
    if not value:
        return ""
    value = str(value).strip()
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", value)
    if m:
        return m.group(1)
    return value


def _coerce_service_account_info(value) -> tuple[dict | None, str]:
    """Returnerar (dict, error_message).

    Streamlit Secrets kan ge oss antingen:
    - en dict (när användaren använder [gcp_service_account] ...)
    - en sträng (om man råkat skriva gcp_service_account = "..." eller klistrat in JSON som sträng)
    """
    if isinstance(value, Mapping):
        return dict(value), ""
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None, "gcp_service_account är tomt."
        try:
            parsed = json.loads(s)
        except Exception:
            return None, (
                "gcp_service_account har fel format (ska vara en TOML-sektion eller en JSON-sträng)."
            )
        if not isinstance(parsed, dict):
            return None, "gcp_service_account har fel format (JSON måste vara ett objekt)."
        return parsed, ""
    return None, "gcp_service_account har fel format (ska vara en TOML-sektion)."


def _coerce_oauth_client_info(value) -> tuple[dict | None, str]:
    if isinstance(value, Mapping):
        return dict(value), ""
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None, "gcp_oauth_client är tomt."
        try:
            parsed = json.loads(s)
        except Exception:
            return None, (
                "gcp_oauth_client har fel format (ska vara en TOML-sektion eller en JSON-sträng)."
            )
        if not isinstance(parsed, dict):
            return None, "gcp_oauth_client har fel format (JSON måste vara ett objekt)."
        return parsed, ""
    return None, "gcp_oauth_client har fel format (ska vara en TOML-sektion)."


def _require_secrets():
    missing = []
    if "drive_folder_id" not in st.secrets:
        missing.append("drive_folder_id")
    if "gcp_oauth_client" not in st.secrets and "gcp_service_account" not in st.secrets:
        missing.append("gcp_oauth_client (eller gcp_service_account)")
    if missing:
        st.error(
            "Saknar secrets: " + ", ".join(missing) + "\n\n"
            "Lösning:\n"
            "- Lokalt: skapa .streamlit/secrets.toml (se .streamlit/secrets.toml.example)\n"
            "- Streamlit Cloud: App → Settings → Secrets, klistra in samma innehåll"
        )
        st.stop()


def _validate_service_account(creds_dict) -> tuple[bool, str]:
    creds_dict, err = _coerce_service_account_info(creds_dict)
    if err:
        return False, err
    if not isinstance(creds_dict, dict):
        return False, "gcp_service_account har fel format (ska vara en TOML-sektion)."
    required = ["type", "project_id", "private_key", "client_email"]
    missing = [k for k in required if not str(creds_dict.get(k, "")).strip()]
    if missing:
        return False, "gcp_service_account saknar värden: " + ", ".join(missing)
    return True, ""


def _validate_oauth_client(client_dict) -> tuple[bool, str]:
    client_dict, err = _coerce_oauth_client_info(client_dict)
    if err:
        return False, err
    if not isinstance(client_dict, dict):
        return False, "gcp_oauth_client har fel format (ska vara en TOML-sektion)."
    required = ["client_id", "client_secret", "auth_uri", "token_uri"]
    missing = [k for k in required if not str(client_dict.get(k, "")).strip()]
    if missing:
        return False, "gcp_oauth_client saknar värden: " + ", ".join(missing)
    return True, ""


def _oauth_enabled() -> bool:
    return "gcp_oauth_client" in st.secrets


def _oauth_cache_path() -> str:
    return os.path.join(".streamlit", "oauth_creds.json")


def _load_cached_oauth_creds() -> Credentials | None:
    path = _oauth_cache_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            info = json.load(f)
        creds = Credentials.from_authorized_user_info(info, SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_cached_oauth_creds(creds)
        return creds
    except Exception:
        return None


def _save_cached_oauth_creds(creds: Credentials) -> None:
    try:
        os.makedirs(".streamlit", exist_ok=True)
        with open(_oauth_cache_path(), "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    except Exception:
        return


def _drive_status() -> tuple[bool, str, str]:
    """Returnerar (enabled, folder_id, reason_if_disabled)."""
    try:
        if "drive_folder_id" not in st.secrets:
            return False, "", "Saknar secret: drive_folder_id"

        folder_id = _extract_drive_folder_id(st.secrets.get("drive_folder_id", ""))
        if not folder_id:
            return False, "", "drive_folder_id är tomt."

        if _oauth_enabled():
            ok, reason = _validate_oauth_client(st.secrets.get("gcp_oauth_client"))
            if not ok:
                return False, "", reason
        else:
            if "gcp_service_account" not in st.secrets:
                return False, "", "Saknar secret: gcp_service_account"
            ok, reason = _validate_service_account(st.secrets.get("gcp_service_account"))
            if not ok:
                return False, "", reason

        return True, folder_id, ""
    except Exception as e:
        return False, "", f"Kunde inte läsa secrets: {e}"


def _openai_enabled() -> bool:
    return bool(_get_openai_api_key())


def _get_openai_api_key() -> str:
    key = str(st.secrets.get("openai_api_key", "")).strip()
    if key:
        return key
    key = str(st.secrets.get("OPENAI_API_KEY", "")).strip()
    if key:
        return key
    openai_section = st.secrets.get("openai", {})
    if isinstance(openai_section, Mapping):
        key = str(openai_section.get("api_key", "")).strip()
        if key:
            return key
        key = str(openai_section.get("key", "")).strip()
        if key:
            return key
    key = str(os.environ.get("OPENAI_API_KEY", "")).strip()
    if key:
        return key
    return ""


def _openai_model_name() -> str:
    name = str(st.secrets.get("openai_model", "")).strip()
    return name or "gpt-4o-mini"


def _summarize_plan(df: pd.DataFrame, engine: "VacationEngine") -> str:
    today = datetime.date.today()
    plan_mask = (df["Semester"] == True) & (df["Typ"].isin(["Arbetsdag", "Spärrad (Jobb)"]))
    half_mask = (df.get("Halvdag", False) == True) & (df["Typ"].isin(["Arbetsdag", "Spärrad (Jobb)"]))
    planned = float(plan_mask.sum()) + 0.5 * float(half_mask.sum())
    remaining = float(st.session_state.get("budget_days", TOTAL_BUDGET)) - planned

    upcoming_plan = (
        df.loc[plan_mask & (df["Datum"] >= today)]
        .sort_values("Datum")
        .head(1)
    )
    next_plan = ""
    if not upcoming_plan.empty:
        next_plan = upcoming_plan.iloc[0]["Datum"].strftime("%Y-%m-%d")

    month_df = df.copy()
    month_df["BudgetDays"] = 0.0
    month_df.loc[plan_mask, "BudgetDays"] = 1.0
    month_df.loc[half_mask, "BudgetDays"] = 0.5
    month_counts = (
        month_df.assign(Month=lambda d: pd.to_datetime(d["Datum"]).dt.month)
        .groupby("Month")["BudgetDays"]
        .sum()
        .sort_values(ascending=False)
        .head(3)
    )
    top_months = ", ".join([f"{MONTH_NAMES[m - 1]} ({c:.1f})" for m, c in month_counts.items()])
    if not top_months:
        top_months = "Inga planerade dagar ännu"

    # Kommande röda dagar/helgdagar
    upcoming_holidays = []
    for day in pd.date_range(start=today, end=END_DATE).to_pydatetime():
        d = day.date()
        if d in engine.se_holidays:
            upcoming_holidays.append(f"{d.strftime('%Y-%m-%d')}: {engine.se_holidays.get(d)}")
        if len(upcoming_holidays) >= 5:
            break

    holidays_text = "\n".join(upcoming_holidays) if upcoming_holidays else "Inga kommande helgdagar hittades."

    return (
        f"Budget: {st.session_state.get('budget_days', TOTAL_BUDGET)} dagar\n"
        f"Planerat: {planned:.1f} dagar\n"
        f"Kvar: {remaining:.1f} dagar\n"
        f"Toppmånader: {top_months}\n"
        f"Nästa planerade semesterdag: {next_plan or 'Ingen'}\n"
        f"Kommande helgdagar:\n{holidays_text}"
    )


def _generate_openai_reply(user_message: str, df: pd.DataFrame, engine: "VacationEngine") -> str:
    api_key = _get_openai_api_key()
    if not api_key:
        return (
            "Saknar openai_api_key i secrets. "
            "Säkerställ att den ligger på toppnivå i Streamlit Secrets eller som OPENAI_API_KEY."
        )

    client = OpenAI(api_key=api_key)
    model = _openai_model_name()

    system_prompt = (
        "Du är en svensk semesterplaneringsassistent. "
        "Ge konkreta, korta och praktiska råd. "
        "Fokusera på att maximera sammanhängande ledighet med minimal semesteråtgång. "
        "Om information saknas, ställ en kort följdfråga."
    )
    context = _summarize_plan(df, engine)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"KONTEKST (aktuell plan):\n{context}"},
            {"role": "user", "content": user_message},
        ],
        temperature=0.4,
    )
    text = (response.choices[0].message.content or "").strip()
    return text or "Jag kunde inte generera ett svar just nu."


def _oauth_redirect_uri(client_dict: dict) -> str:
    redirect_uri = str(st.secrets.get("oauth_redirect_uri", "")).strip()
    if redirect_uri:
        return redirect_uri
    if client_dict.get("oauth_redirect_uri"):
        return str(client_dict.get("oauth_redirect_uri")).strip()
    if client_dict.get("redirect_uri"):
        return str(client_dict.get("redirect_uri")).strip()
    if client_dict.get("redirect_uris"):
        uris = client_dict.get("redirect_uris")
        if isinstance(uris, list) and uris:
            return str(uris[0]).strip()
    return ""


def _build_oauth_flow() -> tuple[Flow, str]:
    client_dict, err = _coerce_oauth_client_info(st.secrets.get("gcp_oauth_client"))
    if err or not client_dict:
        st.error("gcp_oauth_client har fel format i secrets.")
        st.stop()

    redirect_uri = _oauth_redirect_uri(client_dict)
    if not redirect_uri:
        st.error(
            "Saknar oauth_redirect_uri i secrets.\n\n"
            "Lägg till t.ex. oauth_redirect_uri = 'https://DIN-APP.streamlit.app'"
        )
        st.stop()

    client_config = {
        "web": {
            "client_id": client_dict.get("client_id"),
            "client_secret": client_dict.get("client_secret"),
            "auth_uri": client_dict.get("auth_uri"),
            "token_uri": client_dict.get("token_uri"),
            "redirect_uris": [redirect_uri],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = redirect_uri
    return flow, redirect_uri


def _get_oauth_credentials() -> Credentials:
    cached = _load_cached_oauth_creds()
    if cached:
        return cached

    if "oauth_creds_json" in st.session_state:
        info = json.loads(st.session_state["oauth_creds_json"])
        creds = Credentials.from_authorized_user_info(info, SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            st.session_state["oauth_creds_json"] = creds.to_json()
            _save_cached_oauth_creds(creds)
        return creds

    params = st.experimental_get_query_params()
    if "code" in params:
        flow, _ = _build_oauth_flow()
        flow.fetch_token(code=params["code"][0])
        creds = flow.credentials
        st.session_state["oauth_creds_json"] = creds.to_json()
        _save_cached_oauth_creds(creds)
        st.experimental_set_query_params()
        return creds

    flow, _ = _build_oauth_flow()
    auth_url, _state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    st.info("Logga in med Google för att aktivera Drive‑sync (OAuth).")
    st.link_button("Logga in med Google", auth_url)
    st.stop()


def get_drive_service():
    if _oauth_enabled():
        creds = _get_oauth_credentials()
        return build("drive", "v3", credentials=creds)

    _require_secrets()
    if "gcp_service_account" not in st.secrets:
        st.error(
            "Saknar gcp_service_account.\n\n"
            "Om du använder OAuth, lägg in [gcp_oauth_client] i secrets."
        )
        st.stop()
    creds_dict, err = _coerce_service_account_info(st.secrets.get("gcp_service_account"))
    if err or not creds_dict:
        st.error(
            "gcp_service_account har fel format.\n\n"
            "I Streamlit Secrets ska det se ut som en TOML-sektion: [gcp_service_account] ...\n"
            "(alternativt en JSON-sträng)."
        )
        st.stop()
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def load_from_drive(filename):
    """Försöker ladda JSON-fil från den delade mappen"""
    try:
        service = get_drive_service()
        folder_id = _extract_drive_folder_id(st.secrets["drive_folder_id"])
        if not folder_id:
            st.error("drive_folder_id är tomt. Lägg in en Drive folder ID (eller URL) i secrets.")
            return None

        query = f"'{folder_id}' in parents and name = '{filename}' and trashed = false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get("files", [])

        if not files:
            return None

        file_id = files[0]["id"]
        request = service.files().get_media(fileId=file_id)
        raw = request.execute()
        return json.loads(raw.decode("utf-8"))

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

        query = f"'{folder_id}' in parents and name = '{filename}' and trashed = false"
        results = service.files().list(q=query, fields="files(id)").execute()
        files = results.get("files", [])

        json_str = json.dumps(data_dict, indent=2)
        media = MediaIoBaseUpload(
            io.BytesIO(json_str.encode("utf-8")),
            mimetype="application/json",
            resumable=True,
        )

        if files:
            file_id = files[0]["id"]
            service.files().update(fileId=file_id, media_body=media).execute()
        else:
            file_metadata = {"name": filename, "parents": [folder_id]}
            service.files().create(body=file_metadata, media_body=media).execute()

        return True
    except Exception as e:
        st.error(f"Kunde inte spara till Drive: {e}")
        return False


def _extract_drive_payload(drive_data) -> tuple[dict, int]:
    """Returnerar (scenarios, budget_days) från Drive‑data. Hanterar äldre format."""
    if not isinstance(drive_data, dict):
        return {}, TOTAL_BUDGET

    # Nytt format: {"scenarios": {...}, "settings": {"budget_days": 123}}
    if "scenarios" in drive_data:
        scenarios = drive_data.get("scenarios") or {}
        settings = drive_data.get("settings") or {}
        budget_days = int(settings.get("budget_days", TOTAL_BUDGET))
        return scenarios, budget_days

    # Äldre format: {"Scenario A": [...], "Scenario B": [...]}
    return drive_data, TOTAL_BUDGET


# --- GRUNDINSTÄLLNINGAR ---
START_DATE = datetime.date(2026, 1, 1)
END_DATE = datetime.date(2027, 10, 15)
TOTAL_BUDGET = 108
DB_FILENAME = "semester_databas.json"
SETTINGS_FILENAME = "semester_settings.json"

st.set_page_config(page_title="Semesterplaneraren (Drive Sync)", layout="wide")

if "theme_mode" not in st.session_state:
    st.session_state["theme_mode"] = "dark"
theme_mode = st.session_state["theme_mode"]

drive_enabled, _folder_id, drive_disabled_reason = _drive_status()

# Visa status även i huvudvyn (för att underlätta när sidomenyn är stängd)
if drive_enabled:
    st.success("Drive-sync: Aktiv")
else:
    st.warning("Drive-sync: Inaktiv")
    st.caption(f"Orsak: {drive_disabled_reason}")
    with st.expander("Felsök secrets (visar bara nyckelnamn)"):
        st.write(
            "Förväntade nycklar: drive_folder_id, gcp_oauth_client (eller gcp_service_account), openai_api_key"
        )
        try:
            st.code("\n".join(sorted(list(st.secrets.keys()))))
            gcp_val = st.secrets.get("gcp_service_account", None)
            st.write(f"Typ av gcp_service_account: {type(gcp_val).__name__}")
            if isinstance(gcp_val, Mapping):
                st.write("gcp_service_account subkeys:")
                st.code("\n".join(sorted(list(gcp_val.keys()))))
            oauth_val = st.secrets.get("gcp_oauth_client", None)
            if oauth_val is not None:
                st.write(f"Typ av gcp_oauth_client: {type(oauth_val).__name__}")
                if isinstance(oauth_val, Mapping):
                    st.write("gcp_oauth_client subkeys:")
                    st.code("\n".join(sorted(list(oauth_val.keys()))))
            drive_val = st.secrets.get("drive_folder_id", None)
            st.write(f"Typ av drive_folder_id: {type(drive_val).__name__}")
        except Exception as e:
            st.write(f"Kunde inte läsa secrets: {e}")


class VacationEngine:
    def __init__(self):
        try:
            self.se_holidays = holidays.country_holidays("SE", years=[2026, 2027], language="sv")
        except Exception:
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
            data.append(
                {
                    "Datum": str(d),
                    "Vecka": week_num,
                    "Typ": day_type,
                    "Beskrivning": details,
                    "Semester": False,
                    "Halvdag": False,
                    "ExtraLedig": False,
                    "Sjuk": False,
                }
            )
        return pd.DataFrame(data)


engine = VacationEngine()


if "scenarios" not in st.session_state:
    if drive_enabled:
        with st.spinner("Synkar med Google Drive..."):
            drive_data = load_from_drive(DB_FILENAME)
            settings_data = load_from_drive(SETTINGS_FILENAME)

            if drive_data:
                scenarios, budget_days = _extract_drive_payload(drive_data)
                if isinstance(settings_data, dict) and "budget_days" in settings_data:
                    budget_days = int(settings_data.get("budget_days", budget_days))
                if not scenarios:
                    initial_df = engine.get_initial_data()
                    scenarios = {"Utkast 1": initial_df.to_dict("records")}
                st.session_state["scenarios"] = scenarios
                st.session_state["budget_days"] = budget_days
                st.toast("Data laddad från Drive!", icon="☁️")
            else:
                initial_df = engine.get_initial_data()
                st.session_state["scenarios"] = {"Utkast 1": initial_df.to_dict("records")}
                if isinstance(settings_data, dict) and "budget_days" in settings_data:
                    st.session_state["budget_days"] = int(settings_data.get("budget_days", TOTAL_BUDGET))
                else:
                    st.session_state["budget_days"] = TOTAL_BUDGET
                st.toast("Ingen data på Drive, skapade nytt utkast.", icon="🆕")
    else:
        initial_df = engine.get_initial_data()
        st.session_state["scenarios"] = {"Utkast 1": initial_df.to_dict("records")}
        st.session_state["budget_days"] = TOTAL_BUDGET

    first_key = list(st.session_state["scenarios"].keys())[0]
    st.session_state["current_scenario"] = first_key


def create_new_scenario(name):
    current_data = st.session_state["scenarios"][st.session_state["current_scenario"]]
    st.session_state["scenarios"][name] = [row.copy() for row in current_data]
    st.session_state["current_scenario"] = name


def save_all_changes():
    if not drive_enabled:
        st.warning(
            "Drive-sync är inte aktiverad.\n\n"
            f"Orsak: {drive_disabled_reason}\n\n"
            "Lokalt: fyll i .streamlit/secrets.toml (se .streamlit/secrets.toml.example).\n"
            "Streamlit Cloud: App → Settings → Secrets."
        )
        return
    with st.spinner("Sparar till Drive..."):
        payload = {
            "scenarios": st.session_state["scenarios"],
            "settings": {
                "budget_days": int(st.session_state.get("budget_days", TOTAL_BUDGET)),
            },
        }
        success = save_to_drive(DB_FILENAME, payload)
        settings_success = save_to_drive(
            SETTINGS_FILENAME,
            {"budget_days": int(st.session_state.get("budget_days", TOTAL_BUDGET))},
        )
        if success and settings_success:
            st.toast("Sparat till molnet!", icon="✅")
        elif success and not settings_success:
            st.warning("Sparade data men kunde inte spara inställningar.")


def _on_budget_change() -> None:
    if not drive_enabled:
        return
    save_all_changes()


def _shorten_holiday_name(name: str) -> str:
    name = name.strip()
    if not name:
        return ""
    replacements = {
        "Annandag": "Ann.",
        "Dagen": "D.",
        "dagen": "d.",
        "dag": "d.",
        "Helgdag": "Helg.",
        "Söndag": "sön",
        "Sunday": "sön",
    }
    for src, dst in replacements.items():
        name = name.replace(src, dst)
    return name


with st.sidebar:
    st.header("📂 Versioner")

    use_light = st.toggle("Ljust tema", value=(theme_mode == "light"))
    st.session_state["theme_mode"] = "light" if use_light else "dark"
    theme_mode = st.session_state["theme_mode"]

    if theme_mode == "light":
        st.markdown(
            """
            <style>
            .stApp { background-color: #F7F9FB; color: #111111; }
            .stTextInput label, .stSelectbox label, .stToggle label { color: #111111; }
            </style>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """
            <style>
            .stApp { background-color: #0E1117; color: #EAECEE; }
            </style>
            """,
            unsafe_allow_html=True,
        )

    if drive_enabled:
        st.success("Drive-sync: Aktiv")
    else:
        st.warning("Drive-sync: Inaktiv")
        st.caption(f"Orsak: {drive_disabled_reason}")
        with st.expander("Felsök secrets (visar bara nyckelnamn)"):
            st.write(
                "Förväntade nycklar: drive_folder_id, gcp_oauth_client (eller gcp_service_account), gemini_api_key"
            )
            st.write("Nycklar som Streamlit ser:")
            try:
                st.code("\n".join(sorted(list(st.secrets.keys()))))
                gcp_val = st.secrets.get("gcp_service_account", None)
                st.write(f"Typ av gcp_service_account: {type(gcp_val).__name__}")
                drive_val = st.secrets.get("drive_folder_id", None)
                st.write(f"Typ av drive_folder_id: {type(drive_val).__name__}")
            except Exception as e:
                st.write(f"Kunde inte lista nycklar: {e}")

    if st.button("☁️ Spara nu", type="primary", disabled=not drive_enabled):
        save_all_changes()

    st.markdown("---")

    scenario_names = list(st.session_state["scenarios"].keys())
    selected_scenario = st.selectbox(
        "Välj version:",
        scenario_names,
        index=scenario_names.index(st.session_state["current_scenario"]),
    )

    if selected_scenario != st.session_state["current_scenario"]:
        st.session_state["current_scenario"] = selected_scenario
        st.rerun()

    new_name = st.text_input("Namn på ny kopia:", placeholder="T.ex. Plan B")
    if st.button("Kopiera version"):
        if new_name and new_name not in st.session_state["scenarios"]:
            create_new_scenario(new_name)
            save_all_changes()
            st.rerun()
        elif new_name:
            st.error("Namnet finns redan")


st.title(f"Planerar: {st.session_state['current_scenario']}")

if "budget_days" not in st.session_state:
    st.session_state["budget_days"] = TOTAL_BUDGET
st.number_input(
    "Semesterbudget (dagar)",
    min_value=0,
    max_value=365,
    step=1,
    key="budget_days",
    on_change=_on_budget_change,
)

current_records = st.session_state["scenarios"][st.session_state["current_scenario"]]
if "data_store" not in st.session_state:
    st.session_state["data_store"] = {}

scenario_key = st.session_state["current_scenario"]
if scenario_key not in st.session_state["data_store"]:
    df_init = pd.DataFrame(current_records)
    df_init["Datum"] = pd.to_datetime(df_init["Datum"]).dt.date
    if "ExtraLedig" not in df_init.columns:
        df_init["ExtraLedig"] = False
    if "Sjuk" not in df_init.columns:
        df_init["Sjuk"] = False
    if "Halvdag" not in df_init.columns:
        df_init["Halvdag"] = False
    st.session_state["data_store"][scenario_key] = df_init

df = st.session_state["data_store"][scenario_key]
if "Halvdag" not in df.columns:
    df["Halvdag"] = False
    st.session_state["data_store"][scenario_key] = df

if "ai_chat" not in st.session_state:
    st.session_state["ai_chat"] = [
        {
            "role": "assistant",
            "content": "Hej! Fråga mig om bästa sättet att lägga semesterdagar för lång ledighet.",
        }
    ]

with st.popover("💬 AI‑agent (OpenAI)", help="Kort, flytande chatt för semesterplanering"):
    st.caption("Tips: Fråga om långhelger, klämledighet eller förslag per månad.")
    if not _openai_enabled():
        st.info("Lägg in openai_api_key i secrets för att aktivera chatten.")
        st.caption("Nyckel hittad: nej")
    else:
        st.caption("Nyckel hittad: ja")

    for msg in st.session_state["ai_chat"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    with st.form("ai_form", clear_on_submit=True):
        user_prompt = st.text_area("Skriv din fråga", key="ai_prompt", height=80)
        send_clicked = st.form_submit_button("Skicka")

    if send_clicked and user_prompt.strip():
        st.session_state["ai_chat"].append({"role": "user", "content": user_prompt.strip()})
        try:
            reply = _generate_openai_reply(user_prompt.strip(), df, engine)
        except Exception as e:
            reply = f"Det uppstod ett fel: {e}"
        st.session_state["ai_chat"].append({"role": "assistant", "content": reply})
        st.rerun()

def _sync_df_to_scenarios(updated_df: pd.DataFrame) -> None:
    save_df = updated_df.copy()
    save_df["Datum"] = save_df["Datum"].astype(str)
    st.session_state["scenarios"][scenario_key] = save_df.to_dict("records")
    st.session_state["data_store"][scenario_key] = updated_df

def _apply_month_edits(editor_key: str, year: int, month: int) -> None:
    edited = st.session_state.get(editor_key)
    if edited is None:
        return
    updated = st.session_state["data_store"][scenario_key].copy()
    mask = updated["Datum"].apply(lambda d: d.year == year and d.month == month)
    month_df = updated.loc[mask].reset_index(drop=True)

    if isinstance(edited, dict) and "edited_rows" in edited:
        edited_rows = edited.get("edited_rows", {})
        for row_idx, changes in edited_rows.items():
            for col, val in changes.items():
                month_df.at[int(row_idx), col] = val

        added_rows = edited.get("added_rows", [])
        if added_rows:
            month_df = pd.concat([month_df, pd.DataFrame(added_rows)], ignore_index=True)

        deleted_rows = edited.get("deleted_rows", [])
        if deleted_rows:
            month_df = month_df.drop(index=[int(i) for i in deleted_rows]).reset_index(drop=True)

    elif isinstance(edited, pd.DataFrame):
        if edited.empty:
            return
        month_df = edited.copy()
    else:
        return

    month_df["Datum"] = pd.to_datetime(month_df["Datum"]).dt.date
    month_df.loc[month_df["ExtraLedig"] == True, "Semester"] = False
    month_df.loc[month_df["Sjuk"] == True, "Semester"] = False
    month_df.loc[month_df["Halvdag"] == True, "Semester"] = False
    month_df.loc[month_df["Halvdag"] == True, "ExtraLedig"] = False
    month_df.loc[month_df["Halvdag"] == True, "Sjuk"] = False
    month_df.loc[month_df["Semester"] == True, "Halvdag"] = False
    updated.loc[mask, ["Semester", "ExtraLedig", "Sjuk", "Beskrivning"]] = month_df[
        ["Semester", "ExtraLedig", "Sjuk", "Beskrivning"]
    ].values
    updated.loc[mask, ["Halvdag"]] = month_df[["Halvdag"]].values
    _sync_df_to_scenarios(updated)

col1, col2 = st.columns(2)
with col1:
    block_fridays = st.checkbox("Visa spärr varannan fredag", value=True)

if block_fridays:
    mask = df["Datum"].apply(lambda x: x.weekday() == 4 and x.isocalendar()[1] % 2 == 0)
    df.loc[mask, "Typ"] = "Spärrad (Jobb)"

left, right = st.columns([2, 3])

with left:
    st.subheader("Lista")
    today = datetime.date.today()
    if st.button("Idag", key="jump_today"):
        st.session_state["month_year"] = 2026 if today.year < 2026 else (2027 if today.year > 2027 else today.year)
        st.session_state["month_select"] = today.month
        st.rerun()
    year = st.selectbox("År", [2026, 2027], index=0, key="month_year")
    month = st.selectbox(
        "Månad",
        list(range(1, 13)),
        format_func=lambda m: MONTH_NAMES[m - 1],
        key="month_select",
    )

    st.markdown("**Semesterperiod**")
    period_col1, period_col2 = st.columns(2)
    with period_col1:
        start_date = st.date_input("Från och med", value=datetime.date(year, month, 1), key="period_start")
    with period_col2:
        default_end = start_date + datetime.timedelta(days=1)
        end_date = st.date_input("Till och med", value=default_end, key="period_end")
    period_action = st.selectbox(
        "Åtgärd",
        ["Markera semester", "Ta bort semesterperiod"],
        key="period_action",
    )
    if st.button("Applicera period", key="apply_period"):
        if start_date > end_date:
            st.error("Startdatum måste vara före slutdatum.")
        else:
            updated = df.copy()
            mask = (updated["Datum"] >= start_date) & (updated["Datum"] <= end_date)
            workday_mask = updated["Typ"].isin(["Arbetsdag", "Spärrad (Jobb)"])
            if period_action == "Markera semester":
                updated.loc[
                    mask & workday_mask & (updated["ExtraLedig"] != True) & (updated["Sjuk"] != True),
                    "Semester",
                ] = True
                updated.loc[mask & workday_mask, "Halvdag"] = False
            else:
                updated.loc[mask & workday_mask, "Semester"] = False
            updated.loc[mask & (updated["ExtraLedig"] == True), "Semester"] = False
            updated.loc[mask & (updated["Sjuk"] == True), "Semester"] = False
            _sync_df_to_scenarios(updated)
            df = updated

    month_df = df[(pd.to_datetime(df["Datum"]).dt.year == year) & (pd.to_datetime(df["Datum"]).dt.month == month)].copy()
    editor_key = f"editor_{st.session_state['current_scenario']}_{year}_{month}"
    edited_df = st.data_editor(
        month_df,
        column_config={
            "Datum": st.column_config.DateColumn("Datum", width="small"),
            "Vecka": st.column_config.NumberColumn("Vecka", width="small"),
            "Typ": st.column_config.TextColumn("Typ", width="small"),
            "Semester": st.column_config.CheckboxColumn("Sem", width="small"),
            "Halvdag": st.column_config.CheckboxColumn("Halv", width="small"),
            "ExtraLedig": st.column_config.CheckboxColumn("Ledig", width="small"),
            "Sjuk": st.column_config.CheckboxColumn("Sjuk", width="small"),
            "Beskrivning": st.column_config.TextColumn("Notering", width="small"),
        },
        disabled=["Datum", "Vecka", "Typ"],
        hide_index=True,
        height=420,
        key=editor_key,
        on_change=_apply_month_edits,
        args=(editor_key, year, month),
    )

    if isinstance(edited_df, pd.DataFrame) and not edited_df.empty:
        month_df = edited_df.copy()

with right:
    st.subheader("Månadsvy")
    month_map = {row["Datum"]: row for _, row in month_df.iterrows()}
    cal = calendar.Calendar(firstweekday=0)
    weeks = cal.monthdatescalendar(year, month)
    columns = ["Mån", "Tis", "Ons", "Tor", "Fre", "Lör", "Sön"]
    grid = []
    status_grid = []

    for week in weeks:
        row = []
        status_row = []
        for day in week:
            if day.month != month:
                row.append("")
                status_row.append("out")
                continue

            info = month_map.get(day)
            typ = str(info["Typ"]) if info is not None else ""
            is_semester = bool(info["Semester"]) if info is not None else False
            is_extra_ledig = bool(info.get("ExtraLedig", False)) if info is not None else False
            is_sick = bool(info.get("Sjuk", False)) if info is not None else False
            is_halfday = bool(info.get("Halvdag", False)) if info is not None else False
            holiday_name = str(info.get("Beskrivning", "")).strip() if info is not None else ""
            if day in engine.se_holidays:
                holiday_name = str(engine.se_holidays.get(day)).strip()

            label = f"{day.day}"
            status = "jobb"

            if "Ledig" in typ:
                status = "helg"
                label += " 🎉"
                if holiday_name:
                    short_name = _shorten_holiday_name(holiday_name)
                    label += f" {short_name}"
            elif is_sick:
                status = "sjuk"
                label += " 🤒"
            elif is_extra_ledig:
                status = "ledig"
                label += " 🏖️"
            elif is_halfday:
                status = "halvdag"
                label += " 🌓"
            if "Spärrad" in typ and status == "jobb":
                status = "spärr"
                label += " ⛔"
            if is_semester and typ in ["Arbetsdag", "Spärrad (Jobb)"]:
                status = "semester"
                label += " 🌴"

            row.append(label)
            status_row.append(status)

        grid.append(row)
        status_grid.append(status_row)

    cal_df = pd.DataFrame(grid, columns=columns)
    status_df = pd.DataFrame(status_grid, columns=columns)

    def _style_calendar(_):
        styles = pd.DataFrame("", index=cal_df.index, columns=cal_df.columns)
        if theme_mode == "light":
            colors = {
                "semester": "#D5F5E3",
                "helg": "#FADBD8",
                "sjuk": "#F9E79F",
                "ledig": "#D6EAF8",
                "halvdag": "#E8DAEF",
                "spärr": "#E5E7E9",
                "text": "#111111",
                "out": "#AAB7B8",
            }
        else:
            colors = {
                "semester": "#1E8449",
                "helg": "#922B21",
                "sjuk": "#7D6608",
                "ledig": "#21618C",
                "halvdag": "#6C3483",
                "spärr": "#424949",
                "text": "#F2F4F4",
                "out": "#566573",
            }
        for i in range(cal_df.shape[0]):
            for j in range(cal_df.shape[1]):
                status = status_df.iat[i, j]
                if status == "semester":
                    styles.iat[i, j] = f"background-color: {colors['semester']}; color: {colors['text']}; font-weight: 600;"
                elif status == "helg":
                    styles.iat[i, j] = f"background-color: {colors['helg']}; color: {colors['text']};"
                elif status == "sjuk":
                    styles.iat[i, j] = f"background-color: {colors['sjuk']}; color: {colors['text']};"
                elif status == "ledig":
                    styles.iat[i, j] = f"background-color: {colors['ledig']}; color: {colors['text']};"
                elif status == "halvdag":
                    styles.iat[i, j] = f"background-color: {colors['halvdag']}; color: {colors['text']};"
                elif status == "spärr":
                    styles.iat[i, j] = f"background-color: {colors['spärr']}; color: {colors['text']};"
                elif status == "out":
                    styles.iat[i, j] = f"color: {colors['out']};"
        return styles

    st.dataframe(cal_df.style.apply(_style_calendar, axis=None), use_container_width=True, height=240)
    st.caption("Legend: 🌴 semester (arbetsdag), 🌓 halvdag, 🎉 helg/röd dag, 🤒 sjuk, 🏖️ ledig (ej semester), ⛔ spärrad fredag.")

with st.expander("Årsöversikt 2026–2027"):
    df_calc = df.copy()
    df_calc["Year"] = pd.to_datetime(df_calc["Datum"]).dt.year
    df_calc["Month"] = pd.to_datetime(df_calc["Datum"]).dt.month
    full_mask = (df_calc["Semester"] == True) & (df_calc["Typ"].isin(["Arbetsdag", "Spärrad (Jobb)"]))
    half_mask = (df_calc.get("Halvdag", False) == True) & (
        df_calc["Typ"].isin(["Arbetsdag", "Spärrad (Jobb)"])
    )
    df_calc["BudgetDays"] = 0.0
    df_calc.loc[full_mask, "BudgetDays"] = 1.0
    df_calc.loc[half_mask, "BudgetDays"] = 0.5
    monthly = (
        df_calc.groupby(["Year", "Month"])["BudgetDays"]
        .sum()
        .reset_index(name="Planerade dagar")
    )
    month_index = list(range(1, 13))
    month_labels = {i: MONTH_NAMES[i - 1] for i in month_index}
    monthly_pivot = (
        monthly.pivot(index="Month", columns="Year", values="Planerade dagar")
        .reindex(month_index)
        .fillna(0)
        .round(1)
    )
    monthly_pivot.index = [month_labels[i] for i in month_index]

    st.dataframe(monthly_pivot, use_container_width=True, height=320)
    st.bar_chart(monthly_pivot, height=200)

# Statistik & Graf
full_mask = (df["Semester"] == True) & (df["Typ"].isin(["Arbetsdag", "Spärrad (Jobb)"]))
half_mask = (df.get("Halvdag", False) == True) & (df["Typ"].isin(["Arbetsdag", "Spärrad (Jobb)"]))
count = float(full_mask.sum()) + 0.5 * float(half_mask.sum())
rem = float(st.session_state["budget_days"]) - count

st.markdown("---")
c1, c2, c3 = st.columns(3)
c1.metric("Budget", st.session_state["budget_days"])
c2.metric("Planerat", f"{count:.1f}")
c3.metric("Kvar", f"{rem:.1f}")

with st.expander("Tidslinje (översikt)"):
    viz_df = df.copy()
    viz_df["Kategori"] = viz_df.apply(
        lambda r: "Semester"
        if r["Semester"]
        else (
            "Halvdag"
            if r.get("Halvdag", False)
            else (
            "Sjuk"
            if r.get("Sjuk", False)
            else (
                "Ledig (egen)"
                if r.get("ExtraLedig", False)
                else ("Helg" if "Ledig" in r["Typ"] else ("Spärrad" if "Spärrad" in r["Typ"] else "Jobb"))
            )
        )),
        axis=1,
    )
    events = viz_df[viz_df["Kategori"] != "Jobb"].copy()

    if not events.empty:
        events["Start"] = pd.to_datetime(events["Datum"])
        events["End"] = events["Start"] + pd.Timedelta(days=1)
        half_mask = events["Kategori"] == "Halvdag"
        events.loc[half_mask, "End"] = events.loc[half_mask, "Start"] + pd.Timedelta(hours=12)
        fig = px.timeline(
            events,
            x_start="Start",
            x_end="End",
            y="Kategori",
            color="Kategori",
            color_discrete_map={
                "Semester": "#2ECC71",
                "Halvdag": "#9B59B6",
                "Helg": "#E74C3C",
                "Spärrad": "#95A5A6",
                "Ledig (egen)": "#3498DB",
                "Sjuk": "#F1C40F",
            },
        )
        fig.update_layout(xaxis_range=[START_DATE, END_DATE], height=300)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Inga händelser att visa ännu. Markera semester, ledigt eller sjukdagar.")