import streamlit as st
import pandas as pd
import holidays
import datetime
import plotly.express as px
import json
import re
import calendar
from collections.abc import Mapping
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
    if "oauth_creds_json" in st.session_state:
        info = json.loads(st.session_state["oauth_creds_json"])
        creds = Credentials.from_authorized_user_info(info, SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            st.session_state["oauth_creds_json"] = creds.to_json()
        return creds

    params = st.experimental_get_query_params()
    if "code" in params:
        flow, _ = _build_oauth_flow()
        flow.fetch_token(code=params["code"][0])
        creds = flow.credentials
        st.session_state["oauth_creds_json"] = creds.to_json()
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


# --- GRUNDINSTÄLLNINGAR ---
START_DATE = datetime.date(2026, 1, 1)
END_DATE = datetime.date(2027, 10, 15)
TOTAL_BUDGET = 108
DB_FILENAME = "semester_databas.json"

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
        st.write("Förväntade nycklar: drive_folder_id, gcp_oauth_client (eller gcp_service_account)")
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
                    "ExtraLedig": False,
                }
            )
        return pd.DataFrame(data)


engine = VacationEngine()


if "scenarios" not in st.session_state:
    if drive_enabled:
        with st.spinner("Synkar med Google Drive..."):
            drive_data = load_from_drive(DB_FILENAME)

            if drive_data:
                st.session_state["scenarios"] = drive_data
                st.toast("Data laddad från Drive!", icon="☁️")
            else:
                initial_df = engine.get_initial_data()
                st.session_state["scenarios"] = {"Utkast 1": initial_df.to_dict("records")}
                st.toast("Ingen data på Drive, skapade nytt utkast.", icon="🆕")
    else:
        initial_df = engine.get_initial_data()
        st.session_state["scenarios"] = {"Utkast 1": initial_df.to_dict("records")}

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
        success = save_to_drive(DB_FILENAME, st.session_state["scenarios"])
        if success:
            st.toast("Sparat till molnet!", icon="✅")


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
            st.write("Förväntade nycklar: drive_folder_id, gcp_oauth_client (eller gcp_service_account)")
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

current_records = st.session_state["scenarios"][st.session_state["current_scenario"]]
df = pd.DataFrame(current_records)
df["Datum"] = pd.to_datetime(df["Datum"]).dt.date
if "ExtraLedig" not in df.columns:
    df["ExtraLedig"] = False

col1, col2 = st.columns(2)
with col1:
    block_fridays = st.checkbox("Visa spärr varannan fredag", value=True)

if block_fridays:
    mask = df["Datum"].apply(lambda x: x.weekday() == 4 and x.isocalendar()[1] % 2 == 0)
    df.loc[mask, "Typ"] = "Spärrad (Jobb)"

left, right = st.columns([2, 3])

with left:
    st.subheader("Lista")
    year = st.selectbox("År", [2026, 2027], index=0, key="month_year")
    month = st.selectbox(
        "Månad",
        list(range(1, 13)),
        format_func=lambda m: MONTH_NAMES[m - 1],
        key="month_select",
    )

    month_df = df[(pd.to_datetime(df["Datum"]).dt.year == year) & (pd.to_datetime(df["Datum"]).dt.month == month)].copy()
    edited_df = st.data_editor(
        month_df,
        column_config={
            "Datum": st.column_config.DateColumn("Datum", width="small"),
            "Vecka": st.column_config.NumberColumn("Vecka", width="small"),
            "Typ": st.column_config.TextColumn("Typ", width="small"),
            "Semester": st.column_config.CheckboxColumn("Sem", width="small"),
            "ExtraLedig": st.column_config.CheckboxColumn("Ledig", width="small"),
            "Beskrivning": st.column_config.TextColumn("Notering", width="small"),
        },
        disabled=["Datum", "Vecka", "Typ"],
        hide_index=True,
        height=420,
        key=f"editor_{st.session_state['current_scenario']}_{year}_{month}",
    )

    if not edited_df.empty:
        updated = df.copy()
        updated_idx = updated[updated["Datum"].apply(lambda d: d.year == year and d.month == month)].index
        updated.loc[updated_idx, ["Semester", "ExtraLedig", "Beskrivning"]] = edited_df[
            ["Semester", "ExtraLedig", "Beskrivning"]
        ].values
        save_df = updated.copy()
        save_df["Datum"] = save_df["Datum"].astype(str)
        st.session_state["scenarios"][st.session_state["current_scenario"]] = save_df.to_dict("records")
        df = updated
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
            elif is_extra_ledig:
                status = "ledig"
                label += " 🏖️"
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
                "ledig": "#D6EAF8",
                "spärr": "#E5E7E9",
                "text": "#111111",
                "out": "#AAB7B8",
            }
        else:
            colors = {
                "semester": "#1E8449",
                "helg": "#922B21",
                "ledig": "#21618C",
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
                elif status == "ledig":
                    styles.iat[i, j] = f"background-color: {colors['ledig']}; color: {colors['text']};"
                elif status == "spärr":
                    styles.iat[i, j] = f"background-color: {colors['spärr']}; color: {colors['text']};"
                elif status == "out":
                    styles.iat[i, j] = f"color: {colors['out']};"
        return styles

    st.dataframe(cal_df.style.apply(_style_calendar, axis=None), use_container_width=True, height=240)
    st.caption("Legend: 🌴 semester (arbetsdag), 🎉 helg/röd dag, 🏖️ ledig (ej semester), ⛔ spärrad fredag.")

with st.expander("Årsöversikt 2026–2027"):
    df_calc = df.copy()
    df_calc["Year"] = pd.to_datetime(df_calc["Datum"]).dt.year
    df_calc["Month"] = pd.to_datetime(df_calc["Datum"]).dt.month
    vacation_mask = (
        (df_calc["Semester"] == True) & (df_calc["Typ"].isin(["Arbetsdag", "Spärrad (Jobb)"]))
    )
    monthly = df_calc[vacation_mask].groupby(["Year", "Month"]).size().reset_index(name="Planerade dagar")
    month_index = list(range(1, 13))
    month_labels = {i: MONTH_NAMES[i - 1] for i in month_index}
    monthly_pivot = (
        monthly.pivot(index="Month", columns="Year", values="Planerade dagar")
        .reindex(month_index)
        .fillna(0)
        .astype(int)
    )
    monthly_pivot.index = [month_labels[i] for i in month_index]

    st.dataframe(monthly_pivot, use_container_width=True, height=320)
    st.bar_chart(monthly_pivot, height=200)

# Statistik & Graf
vacation_days = df[(df["Semester"] == True) & (df["Typ"].isin(["Arbetsdag", "Spärrad (Jobb)"]))]
count = len(vacation_days)
rem = TOTAL_BUDGET - count

st.markdown("---")
c1, c2, c3 = st.columns(3)
c1.metric("Budget", TOTAL_BUDGET)
c2.metric("Planerat", count)
c3.metric("Kvar", rem)

viz_df = df.copy()
viz_df["Kategori"] = viz_df.apply(
    lambda r: "Semester"
    if r["Semester"]
    else (
        "Ledig (egen)"
        if r.get("ExtraLedig", False)
        else ("Helg" if "Ledig" in r["Typ"] else ("Spärrad" if "Spärrad" in r["Typ"] else "Jobb"))
    ),
    axis=1,
)
events = viz_df[viz_df["Kategori"] != "Jobb"].copy()

if not events.empty:
    fig = px.timeline(
        events,
        x_start="Datum",
        x_end="Datum",
        y="Kategori",
        color="Kategori",
        color_discrete_map={
            "Semester": "#2ECC71",
            "Helg": "#E74C3C",
            "Spärrad": "#95A5A6",
            "Ledig (egen)": "#3498DB",
        },
    )
    fig.update_layout(xaxis_range=[START_DATE, END_DATE], height=300)
    st.plotly_chart(fig, use_container_width=True)