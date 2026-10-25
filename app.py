"""Stabilisierte Tagesplanung für Bayern und Österreich.

Starten mit: streamlit run Routenplaner_Madame_Update_mit_Tagesbericht_TXT.py
Konfiguration des Login-Passworts siehe README.txt.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
import unicodedata
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Any

import folium
import pandas as pd
import requests
import streamlit as st
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from streamlit_folium import st_folium


# ---------------------------------------------------------------------------
# Grundkonfiguration
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Routenplaner Bayern & Österreich", layout="wide")

GEOCODING_URL = "https://api.heigit.org/pelias/v1/search"
MATRIX_URL = "https://api.heigit.org/openrouteservice/v2/matrix/driving-car"
DIRECTIONS_URL = "https://api.heigit.org/openrouteservice/v2/directions/driving-car/geojson"
MUNICH_CENTER = [11.5754, 48.1374]  # Nur Karten-Zoom-Zentrum, niemals Adress-Fallback.
UNREACHABLE_SECONDS = 10_000_000
UNREACHABLE_METERS = 10_000_000
AREA_PENALTY_MINUTES = 20  # Kostenmalus, in derselben Einheit wie Fahr-/Servicezeit.
UNSERVED_CUSTOMER_PENALTY = 100_000
MAX_MATRIX_PAIRS = 3_500  # Öffentliche HeiGIT/ORS-Matrixbeschränkung.
MAX_DIRECTIONS_POINTS = 50  # Obergrenze der Routenwegpunkte für die Kartenlinie.


# ---------------------------------------------------------------------------
# Passwortschutz: Passwort nicht im Quellcode ablegen.
# APP_PASSWORD kann über Streamlit Secrets oder eine Umgebungsvariable gesetzt werden.
# ---------------------------------------------------------------------------
def configured_app_password() -> str:
    password = ""
    try:
        password = str(st.secrets.get("APP_PASSWORD", "") or "").strip()
    except Exception:
        # Es ist völlig okay, wenn lokal noch keine secrets.toml existiert.
        pass
    if not password:
        password = os.environ.get("APP_PASSWORD", "").strip()
    return password


APP_PASSWORD = configured_app_password()
if not APP_PASSWORD:
    st.error("Das Login-Passwort ist noch nicht konfiguriert.")
    st.markdown(
        "Lege eine Datei **`.streamlit/secrets.toml`** neben dem Projekt an und trage ein:\n\n"
        "```toml\nAPP_PASSWORD = \"DEIN_EIGENES_PASSWORT\"\n```\n\n"
        "Alternativ kann die Umgebungsvariable `APP_PASSWORD` gesetzt werden. "
        "Eine ausführliche Kurz-Anleitung steht in `README_Routenplaner.txt`."
    )
    st.stop()

if "authentifiziert" not in st.session_state:
    st.session_state.authentifiziert = False

if not st.session_state.authentifiziert:
    st.warning("🔒 Diese App ist geschützt.")
    passwort = st.text_input("Passwort eingeben", type="password")
    if st.button("Einloggen"):
        if passwort == APP_PASSWORD:
            st.session_state.authentifiziert = True
            st.rerun()
        else:
            st.error("Falsches Passwort!")
    st.stop()


# ---------------------------------------------------------------------------
# Allgemeine Hilfsfunktionen
# ---------------------------------------------------------------------------
def normalized_header(value: Any) -> str:
    text = str(value).strip().lower()
    text = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def find_column(df: pd.DataFrame, patterns: tuple[str, ...]) -> str | None:
    """Sucht eine Spalte anhand normalisierter Wörter/Begriffe."""
    for col in df.columns:
        name = normalized_header(col)
        if any(pattern in name for pattern in patterns):
            return col
    return None


def load_file(uploaded_file: Any) -> pd.DataFrame:
    """Liest CSV (Trennzeichen automatisch) oder Excel; wirft klare Fehler."""
    if uploaded_file is None:
        raise ValueError("Es wurde keine Datei ausgewählt.")

    suffix = Path(uploaded_file.name).suffix.lower()
    raw = uploaded_file.getvalue()
    if not raw:
        raise ValueError("Die ausgewählte Datei ist leer.")

    if suffix == ".csv":
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                from io import BytesIO
                return pd.read_csv(BytesIO(raw), sep=None, engine="python", dtype=object, encoding=encoding)
            except (UnicodeDecodeError, pd.errors.ParserError) as exc:
                last_error = exc
        raise ValueError(f"CSV-Datei konnte nicht eingelesen werden: {last_error}")
    if suffix in (".xlsx", ".xls"):
        from io import BytesIO
        try:
            return pd.read_excel(BytesIO(raw), dtype=object)
        except Exception as exc:
            raise ValueError(f"Excel-Datei konnte nicht eingelesen werden: {exc}") from exc
    raise ValueError("Bitte eine CSV- oder Excel-Datei (.xlsx/.xls) auswählen.")


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return not str(value).strip()


def parse_time(value: Any, default_min: int) -> int:
    """Liest Uhrzeiten als HH:MM, Python-Zeit, Timestamp oder Excel-Tagesbruch.

    Leere Zellen verwenden default_min. Ungültige, nichtleere Werte werfen einen
    Fehler, statt stillschweigend eine andere Uhrzeit zu verwenden.
    """
    if is_blank(value):
        return int(default_min)

    if isinstance(value, pd.Timestamp):
        return int(value.hour * 60 + value.minute)
    if isinstance(value, datetime):
        return int(value.hour * 60 + value.minute)
    if isinstance(value, dt_time):
        return int(value.hour * 60 + value.minute)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        # Excel speichert Uhrzeiten oft als Anteil eines Tages.
        if 0 <= number <= 1:
            return min(1440, int(round(number * 1440)))
        raise ValueError(f"Uhrzeit '{value}' ist keine gültige Uhrzeit. Bitte HH:MM verwenden.")

    text = str(value).strip()
    match = re.fullmatch(r"(\d{1,2})\s*[:.]\s*(\d{2})(?:\s*:\s*\d{2})?", text)
    if not match:
        raise ValueError(f"Uhrzeit '{text}' ist ungültig. Bitte HH:MM verwenden.")
    hour, minute = int(match.group(1)), int(match.group(2))
    if minute > 59 or hour > 24 or (hour == 24 and minute != 0):
        raise ValueError(f"Uhrzeit '{text}' liegt außerhalb des gültigen Bereichs.")
    return hour * 60 + minute


def format_time(minutes: int) -> str:
    minutes = max(0, min(int(minutes), 1440))
    if minutes == 1440:
        return "24:00"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def encode_readable_txt(content: str) -> bytes:
    """Erzeugt eine lesbare Windows-Textdatei mit UTF-8-BOM und Windows-Zeilenumbrüchen."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\n", "\r\n")
    return ("\ufeff" + normalized).encode("utf-8")


def match_vehicle_name(
    value: Any,
    vehicle_list: list[str],
    vehicle_aliases: dict[str, int] | None = None,
) -> int | None:
    """Ordnet Fahrzeugnamen fehlertolerant zu, auch wenn Dateien andere Bezeichnungen verwenden."""
    if is_blank(value):
        return None
    normalized_value = normalized_header(value).replace(" ", "")

    if vehicle_aliases:
        found_idx = vehicle_aliases.get(normalized_value)
        if found_idx is not None and 0 <= found_idx < len(vehicle_list):
            return found_idx

    for idx, vehicle_name in enumerate(vehicle_list):
        normalized_vehicle = normalized_header(vehicle_name).replace(" ", "")
        if normalized_value == normalized_vehicle:
            return idx

    # Tolerant gegenüber "Auto 1", "Fahrer 1", "1" und "Fahrzeug 01".
    value_nums = re.findall(r"\d+", str(value))
    if value_nums:
        wanted_number = int(value_nums[0])
        for idx, vehicle_name in enumerate(vehicle_list):
            vehicle_nums = re.findall(r"\d+", vehicle_name)
            if vehicle_nums and int(vehicle_nums[0]) == wanted_number:
                return idx
    return None


def clean_address(value: Any) -> str:
    if is_blank(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def discover_vehicle_names(
    num_vehicles: int,
    sources: list[tuple[str, Any]],
) -> tuple[list[str], dict[str, int], list[str]]:
    """Übernimmt Fahrzeugnamen aus Zusatzdateien und erstellt robuste Datei-Aliase.

    Nummerierte Namen (z. B. "Fahrzeug 3") bleiben auf ihrem passenden Fahrzeugplatz.
    Namen ohne Nummer werden in der Reihenfolge der Dateien den freien Plätzen zugeordnet.
    """
    slots: list[str | None] = [None] * num_vehicles
    aliases: dict[str, int] = {}
    unnumbered_names: list[str] = []
    warnings: list[str] = []
    out_of_range: set[str] = set()

    for source_label, uploaded_file in sources:
        if uploaded_file is None:
            continue
        try:
            frame = load_file(uploaded_file)
        except Exception as exc:
            warnings.append(f"Fahrzeugnamen konnten aus {source_label} nicht gelesen werden: {exc}")
            continue

        vehicle_col = find_column(frame, ("fahrzeug", "auto", "fahrer", "vehicle", "driver"))
        if vehicle_col is None:
            continue

        for raw_name in frame[vehicle_col].tolist():
            if is_blank(raw_name):
                continue
            name = clean_address(raw_name)
            key = normalized_header(name).replace(" ", "")
            if not key:
                continue

            numbers = re.findall(r"\d+", name)
            if numbers:
                number = int(numbers[0])
                if 1 <= number <= num_vehicles:
                    idx = number - 1
                    if slots[idx] is None:
                        slots[idx] = name
                    aliases.setdefault(key, idx)
                else:
                    out_of_range.add(name)
                continue

            # Dedupe cross-file repeats before assigning any name-only entries.
            if key not in aliases and all(normalized_header(existing).replace(" ", "") != key for existing in unnumbered_names):
                unnumbered_names.append(name)

    # Fill still-empty slots with human-readable names that have no vehicle number.
    for name in unnumbered_names:
        key = normalized_header(name).replace(" ", "")
        if key in aliases:
            continue
        try:
            idx = slots.index(None)
        except ValueError:
            warnings.append(
                f"Der Fahrzeugname '{name}' konnte keinem freien Fahrzeugplatz zugeordnet werden. "
                "Bitte die Bezeichnung in den Zusatzdateien angleichen."
            )
            continue
        slots[idx] = name
        aliases[key] = idx

    # Default labels fill gaps; all common numeric aliases continue to match those slots.
    vehicle_names: list[str] = []
    for idx, value in enumerate(slots):
        default_name = f"Fahrzeug {idx + 1}"
        vehicle_names.append(value or default_name)
        aliases[normalized_header(default_name).replace(" ", "")] = idx
        aliases[normalized_header(f"Auto {idx + 1}").replace(" ", "")] = idx
        aliases[normalized_header(f"Fahrer {idx + 1}").replace(" ", "")] = idx
        aliases[normalized_header(f"Vehicle {idx + 1}").replace(" ", "")] = idx
        aliases[str(idx + 1)] = idx

    if out_of_range:
        warnings.append(
            "Diese nummerierten Fahrzeuge liegen außerhalb der eingestellten Fahrzeuganzahl "
            f"(1–{num_vehicles}) und können nicht verwendet werden: " + ", ".join(sorted(out_of_range))
        )
    return vehicle_names, aliases, warnings


def parse_overnight_overrides(
    uploaded_file: Any,
    planungs_datum: date,
    vehicle_list: list[str],
    vehicle_aliases: dict[str, int] | None = None,
) -> tuple[dict[int, str], list[str], bool]:
    """Liest die Übernachtungsdatei und liefert Start-/Endadressen für den gewählten Tag.

    Rückgabe: Fahrzeugindex -> Übernachtungsadresse, Warnungen, ob es Einträge
    für den gewählten Planungstag gab.
    """
    if uploaded_file is None:
        return {}, [], False

    df = load_file(uploaded_file)
    c_vehicle = find_column(df, ("fahrzeug", "auto", "fahrer"))
    c_day = find_column(df, ("datum", "tag"))
    c_address = find_column(df, ("adresse", "hotel", "unterkunft", "ort", "start", "strasse"))
    if not (c_vehicle and c_day and c_address):
        raise ValueError(
            "Es müssen Spalten für Fahrzeug, Datum und Übernachtungsadresse vorhanden sein. "
            f"Erkannt wurden: {', '.join(map(str, df.columns))}"
        )

    df = df.copy()
    df["_datum_geparst"] = pd.to_datetime(df[c_day], errors="coerce").dt.normalize()
    # Wenn Datumswerte im deutschen Format vorliegen (z. B. 25.10.2026),
    # versuche nur die noch nicht erkannten Werte zusätzlich mit dayfirst=True.
    missing_dates = df["_datum_geparst"].isna()
    if missing_dates.any():
        df.loc[missing_dates, "_datum_geparst"] = pd.to_datetime(
            df.loc[missing_dates, c_day], errors="coerce", dayfirst=True
        ).dt.normalize()
    chosen_date = pd.Timestamp(planungs_datum).normalize()
    overrides: dict[int, str] = {}
    warnings: list[str] = []
    unknown_vehicles: set[str] = set()
    invalid_date_rows = 0
    matching_date_rows = 0
    empty_address_rows = 0
    duplicate_vehicles: set[str] = set()

    for _, row in df.iterrows():
        row_date = row["_datum_geparst"]
        if pd.isna(row_date):
            invalid_date_rows += 1
            continue
        if row_date != chosen_date:
            continue
        matching_date_rows += 1
        vehicle_value = row[c_vehicle]
        vehicle_idx = match_vehicle_name(vehicle_value, vehicle_list, vehicle_aliases)
        if vehicle_idx is None:
            if not is_blank(vehicle_value):
                unknown_vehicles.add(str(vehicle_value).strip())
            continue
        address = clean_address(row[c_address])
        if not address:
            empty_address_rows += 1
            continue
        if vehicle_idx in overrides:
            duplicate_vehicles.add(vehicle_list[vehicle_idx])
        overrides[vehicle_idx] = address

    if unknown_vehicles:
        warnings.append(
            "Diese Fahrzeugnamen aus der Übernachtungsdatei wurden nicht zugeordnet: "
            + ", ".join(sorted(unknown_vehicles))
        )
    if invalid_date_rows:
        warnings.append(
            f"{invalid_date_rows} Zeile(n) in der Übernachtungsdatei hatten ein ungültiges Datum und wurden ignoriert."
        )
    if empty_address_rows:
        warnings.append(
            f"{empty_address_rows} Eintrag/Einträge für den gewählten Tag hatten keine Adresse und wurden ignoriert."
        )
    if duplicate_vehicles:
        warnings.append(
            "Für diese Fahrzeuge gibt es mehrere Übernachtungsadressen am selben Tag; verwendet wird jeweils der letzte Eintrag: "
            + ", ".join(sorted(duplicate_vehicles))
        )
    return overrides, warnings, matching_date_rows > 0


def prepare_customer_table(raw: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Ordnet übliche Spalten zu und baut bei Bedarf eine volle Straßenadresse."""
    if raw.empty or len(raw.columns) == 0:
        raise ValueError("Die Kundendatei enthält keine Daten oder keine Spaltenüberschriften.")

    df = raw.copy()
    df.columns = [str(col).strip().lower() for col in df.columns]
    warnings: list[str] = []

    col_name = find_column(df, ("kundenname", "name", "kunde", "kontakt"))
    if col_name is None:
        if len(df.columns) < 2:
            raise ValueError("In der Kundendatei wurde keine Namensspalte gefunden und es gibt weniger als zwei Spalten.")
        col_name = df.columns[0]
        warnings.append(f"Keine eindeutige Namensspalte erkannt; vorläufig wurde '{col_name}' verwendet.")

    col_address = find_column(df, ("adresse", "anschrift", "lieferadresse", "address", "street", "strasse"))
    col_street = find_column(df, ("strasse", "street", "hausanschrift"))
    col_postcode = find_column(df, ("plz", "postleitzahl", "postal code", "postcode", "zip"))
    col_city = find_column(df, ("ort", "stadt", "city", "gemeinde"))

    if col_address is not None:
        address_series = df[col_address].map(clean_address)
        # Falls eine Straße gewählt wurde und PLZ/Ort separat vorliegen, werden sie ergänzt.
        if col_street == col_address and (col_postcode or col_city):
            def join_address(row: pd.Series) -> str:
                parts = [clean_address(row.get(col_address, ""))]
                if col_postcode:
                    parts.append(clean_address(row.get(col_postcode, "")))
                if col_city:
                    parts.append(clean_address(row.get(col_city, "")))
                return ", ".join(part for part in parts if part)
            address_series = df.apply(join_address, axis=1)
    elif col_street is not None:
        def join_address(row: pd.Series) -> str:
            parts = [clean_address(row.get(col_street, ""))]
            if col_postcode:
                parts.append(clean_address(row.get(col_postcode, "")))
            if col_city:
                parts.append(clean_address(row.get(col_city, "")))
            return ", ".join(part for part in parts if part)
        address_series = df.apply(join_address, axis=1)
    elif col_city is not None:
        address_series = df[col_city].map(clean_address)
        warnings.append("Es wurde nur eine Orts-/Stadtspalte gefunden, keine vollständige Straßenadresse.")
    elif len(df.columns) >= 2:
        col_address = df.columns[1]
        address_series = df[col_address].map(clean_address)
        warnings.append(f"Keine eindeutige Adressspalte erkannt; vorläufig wurde '{col_address}' verwendet.")
    else:
        raise ValueError("In der Kundendatei wurde keine Adressspalte gefunden.")

    col_von = find_column(df, ("zeit von", "von", "fruehestens", "startzeit", "beginn"))
    col_bis = find_column(df, ("zeit bis", "bis", "spaetestens", "endzeit", "ende"))
    col_gebiet = find_column(df, ("gebiet", "zone", "bereich", "region"))

    result = pd.DataFrame()
    result["Name"] = df[col_name]
    result["Adresse"] = address_series
    result["Zeit_von"] = df[col_von] if col_von else "08:00"
    result["Zeit_bis"] = df[col_bis] if col_bis else "17:00"
    result["Gebiet"] = df[col_gebiet] if col_gebiet else ""
    return result, warnings


# ---------------------------------------------------------------------------
# HeiGIT / OpenRouteService API
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=60 * 60)
def get_koordinaten(api_key: str, adresse: str) -> tuple[list[float] | None, str, str]:
    """Liefert Koordinaten, Label und Fehlermeldung; erfindet nie Ersatzkoordinaten."""
    address = clean_address(adresse)
    if not address:
        return None, "", "Adresse ist leer."

    headers = {"Authorization": api_key, "Accept": "application/json"}
    attempts = [address]
    simplified = re.sub(r"\b\d{5}\b", "", address).strip()
    simplified = re.sub(r"\s+", " ", simplified)
    if simplified and simplified.casefold() != address.casefold():
        attempts.append(simplified)

    last_message = "Keine passenden Suchergebnisse gefunden."
    for query in attempts:
        try:
            response = requests.get(
                GEOCODING_URL,
                headers=headers,
                params={"text": query, "size": 1},
                timeout=12,
            )
        except requests.RequestException as exc:
            last_message = f"Verbindungsfehler bei der Adresssuche: {exc}"
            continue

        if response.status_code in (401, 403):
            return None, "", "HeiGIT hat den Zugriff abgelehnt (401/403). API-Key und Berechtigungen prüfen."
        if response.status_code == 429:
            return None, "", "HeiGIT-API-Limit erreicht (HTTP 429). Bitte später erneut versuchen."
        if response.status_code != 200:
            last_message = f"Adresssuche fehlgeschlagen (HTTP {response.status_code})."
            continue

        try:
            data = response.json()
            features = data.get("features", [])
            if not features:
                last_message = "Keine passenden Suchergebnisse gefunden."
                continue
            feature = features[0]
            coords = feature.get("geometry", {}).get("coordinates", [])
            if not isinstance(coords, (list, tuple)) or len(coords) < 2:
                last_message = "Der Geocoder hat ungültige Koordinaten geliefert."
                continue
            lon, lat = float(coords[0]), float(coords[1])
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                last_message = "Der Geocoder hat Koordinaten außerhalb des gültigen Bereichs geliefert."
                continue
            label = str(feature.get("properties", {}).get("label", "Treffer ohne Adresslabel"))
            return [lon, lat], label, ""
        except (ValueError, TypeError, KeyError) as exc:
            last_message = f"Antwort des Geocoders konnte nicht gelesen werden: {exc}"

    return None, "", last_message


@st.cache_data(show_spinner=False, ttl=30 * 60)
def get_distanzmatrix(api_key: str, koordinaten_tuple: tuple[tuple[float, float], ...]) -> tuple[list[list[int]], list[list[int]]]:
    headers = {"Authorization": api_key, "Content-Type": "application/json"}
    body = {"locations": [list(point) for point in koordinaten_tuple], "metrics": ["duration", "distance"]}

    try:
        response = requests.post(MATRIX_URL, json=body, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise RuntimeError(f"Verbindungsfehler bei der Distanzmatrix: {exc}") from exc

    if response.status_code != 200:
        detail = response.text[:500].replace("\n", " ")
        if response.status_code in (401, 403):
            raise RuntimeError("HeiGIT hat den Zugriff auf die Distanzmatrix abgelehnt (401/403). API-Key und Berechtigungen prüfen.")
        if response.status_code == 429:
            raise RuntimeError("Das HeiGIT-API-Limit wurde erreicht (HTTP 429). Bitte später erneut versuchen.")
        raise RuntimeError(f"Matrix-API-Fehler (HTTP {response.status_code}): {detail}")

    try:
        result = response.json()
        raw_durations = result["durations"]
        raw_distances = result["distances"]
        expected = len(koordinaten_tuple)
        if len(raw_durations) != expected or len(raw_distances) != expected:
            raise ValueError("Die Matrix hat eine unerwartete Zeilenanzahl.")
        if any(len(row) != expected for row in raw_durations) or any(len(row) != expected for row in raw_distances):
            raise ValueError("Die Matrix hat eine unerwartete Spaltenanzahl.")
        durations = [
            [UNREACHABLE_SECONDS if val is None else max(0, int(math.ceil(float(val)))) for val in row]
            for row in raw_durations
        ]
        distances = [
            [UNREACHABLE_METERS if val is None else max(0, int(math.ceil(float(val)))) for val in row]
            for row in raw_distances
        ]
        return durations, distances
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Die Antwort der Matrix-API ist unvollständig oder ungültig: {exc}") from exc


@st.cache_data(show_spinner=False, ttl=30 * 60)
def get_route_geometry(api_key: str, coords_tuple: tuple[tuple[float, float], ...]) -> tuple[dict[str, Any] | None, str]:
    headers = {"Authorization": api_key, "Content-Type": "application/json"}
    body = {"coordinates": [list(point) for point in coords_tuple]}
    try:
        response = requests.post(DIRECTIONS_URL, json=body, headers=headers, timeout=25)
    except requests.RequestException as exc:
        return None, f"Verbindungsfehler beim Abrufen der Kartenroute: {exc}"
    if response.status_code != 200:
        detail = response.text[:300].replace("\n", " ")
        return None, f"Die Kartenlinie konnte nicht geladen werden (HTTP {response.status_code}): {detail}"
    try:
        data = response.json()
        if not isinstance(data, dict) or not data.get("features"):
            return None, "Die Directions-API lieferte keine Routen-Geometrie."
        return data, ""
    except ValueError:
        return None, "Die Directions-API lieferte keine gültige JSON-Antwort."


# ---------------------------------------------------------------------------
# Benutzeroberfläche
# ---------------------------------------------------------------------------
st.title("🚚 Automatischer Routenplaner (Tagesplanung mit Live-Editor)")
st.caption("Tagesplanung · Dateiübernahme für Fahrzeuge · editierbare Start-/Endadressen · Karten und übersichtliche Textberichte")

st.sidebar.header("Einstellungen")
api_key = st.sidebar.text_input("HeiGIT API-Key", type="password").strip()
planungs_datum = st.sidebar.date_input("Planungs-Datum wählen", value=date.today())
num_vehicles = int(st.sidebar.number_input("Anzahl der Fahrzeuge", min_value=1, max_value=10, value=5, step=1))
service_time_mins = int(st.sidebar.number_input("Servicezeit pro Kunde (Minuten)", min_value=0, max_value=180, value=60, step=5, key="service_time_mins_default60"))
max_km_pro_tag = int(st.sidebar.number_input("Max. Kilometer pro Fahrzeug / Tag", min_value=50, max_value=1000, value=300, step=10))

zeit_optionen = [f"{h:02d}:{m:02d}" for h in range(6, 23) for m in (0, 30)]

# Uploads are shown before the vehicle fields so the overnight addresses and names
# can be reflected in the vehicle tabs immediately after selecting a file.
st.subheader("📁 1. Kundendaten & Zusatzdateien")
col1, col2 = st.columns(2)
with col1:
    file_kunden = st.file_uploader("1. Kunden & Adressen (Pflicht – CSV/Excel)", type=["csv", "xlsx", "xls"])
    file_gebiete = st.file_uploader("2. Sinnvolle Gebiete (optional)", type=["csv", "xlsx", "xls"])
with col2:
    file_urlaub = st.file_uploader("3. Urlaubsplanung (optional)", type=["csv", "xlsx", "xls"])
    file_uebernacht = st.file_uploader("4. Abweichender Start / Übernachtung (optional)", type=["csv", "xlsx", "xls"])

fahrzeug_namen, vehicle_aliases, vehicle_name_warnings = discover_vehicle_names(
    num_vehicles,
    [
        ("Gebietsdatei", file_gebiete),
        ("Urlaubsdatei", file_urlaub),
        ("Übernachtungsdatei", file_uebernacht),
    ],
)
for warning in vehicle_name_warnings:
    st.warning(warning)

# Read the overnight file before drawing the vehicle controls. The resulting addresses
# become the actual editable defaults shown in the vehicle section below.
uebernacht_overrides: dict[int, str] = {}
uebernacht_warnungen: list[str] = []
uebernacht_datum_hat_eintraege = False
if file_uebernacht is not None:
    try:
        uebernacht_overrides, uebernacht_warnungen, uebernacht_datum_hat_eintraege = parse_overnight_overrides(
            file_uebernacht, planungs_datum, fahrzeug_namen, vehicle_aliases
        )
        for warning in uebernacht_warnungen:
            st.warning(warning)
        if uebernacht_overrides:
            preview_rows = [
                {
                    "Fahrzeug": fahrzeug_namen[idx],
                    "Übernommener Start": address,
                    "Übernommenes Ende": address,
                    "Planungsdatum": planungs_datum.strftime("%d.%m.%Y"),
                }
                for idx, address in sorted(uebernacht_overrides.items())
            ]
            st.success(
                f"Übernachtungsdatei erkannt: {len(uebernacht_overrides)} Fahrzeug(e) werden für "
                f"den {planungs_datum.strftime('%d.%m.%Y')} vorbelegt. "
                "Die Adressen stehen unten direkt in den Fahrzeugfeldern und bleiben dort bearbeitbar."
            )
            st.dataframe(pd.DataFrame(preview_rows), hide_index=True, use_container_width=True)
        elif not uebernacht_datum_hat_eintraege:
            st.info(
                f"In der Übernachtungsdatei gibt es keinen Eintrag für den {planungs_datum.strftime('%d.%m.%Y')}. "
                "Die regulären Fahrzeugadressen werden angezeigt."
            )
        else:
            st.warning(
                "Es gibt Einträge für das gewählte Datum, aber keine gültige Fahrzeug-/Adresszuordnung. "
                "Bitte die Fahrzeugnamen in den Zusatzdateien angleichen."
            )
    except Exception as exc:
        st.warning(f"Übernachtungsdatei konnte nicht verarbeitet werden: {exc}. Die regulären Start-/Endadressen bleiben aktiv.")

st.subheader("🚐 2. Fahrzeuge (Start-/Endadressen & Schichtzeiten)")
tabs = st.tabs(fahrzeug_namen)
starts: list[str] = []
ends: list[str] = []
schicht_starts: list[str] = []
schicht_ends: list[str] = []

# Keep regular depot edits when changing dates, but use a date/file-specific widget
# for imported overnight addresses so they refresh correctly when the source changes.
overnight_digest = (
    hashlib.sha256(file_uebernacht.getvalue()).hexdigest()[:10]
    if file_uebernacht is not None else "no-overnight-file"
)
for i, tab in enumerate(tabs):
    with tab:
        col_a, col_b = st.columns(2)
        has_override = i in uebernacht_overrides
        address_override = uebernacht_overrides.get(i, "")
        if has_override:
            override_signature = hashlib.sha256(
                f"{planungs_datum}|{overnight_digest}|{address_override}".encode("utf-8")
            ).hexdigest()[:10]
            start_key = f"start_overnight_{i}_{override_signature}"
            end_key = f"end_overnight_{i}_{override_signature}"
            start_label = "Startadresse (aus Übernachtungsdatei)"
            end_label = "Endadresse (aus Übernachtungsdatei)"
        else:
            start_key = f"start_regular_{i}"
            end_key = f"end_regular_{i}"
            start_label = "Regulärer Start"
            end_label = "Reguläres Ende"

        with col_a:
            start = st.text_input(
                start_label,
                value=address_override if has_override else "Marienplatz 1, 80331 München",
                key=start_key,
            )
            shift_start = st.selectbox(
                "Schichtbeginn", options=zeit_optionen,
                index=zeit_optionen.index("08:00"), key=f"svon_{i}",
            )
        with col_b:
            end = st.text_input(
                end_label,
                value=address_override if has_override else "Marienplatz 1, 80331 München",
                key=end_key,
            )
            shift_end = st.selectbox(
                "Schichtende", options=zeit_optionen,
                index=zeit_optionen.index("17:00"), key=f"sbis_{i}",
            )
        if has_override:
            st.caption(
                "📍 Für das gewählte Datum aus der Übernachtungsdatei übernommen. "
                "Du kannst Start und Ende hier direkt anpassen."
            )
        starts.append(clean_address(start))
        ends.append(clean_address(end))
        schicht_starts.append(shift_start)
        schicht_ends.append(shift_end)

df_kunden: pd.DataFrame | None = None
if file_kunden is not None:
    try:
        customer_raw = load_file(file_kunden)
        df_default, customer_warnings = prepare_customer_table(customer_raw)
        for warning in customer_warnings:
            st.warning(warning)
        editor_key = "kunden_editor_" + hashlib.sha256(file_kunden.getvalue()).hexdigest()[:12]
        st.markdown("✏️ **Kundendaten vor der Planung prüfen, korrigieren oder ergänzen:**")
        df_kunden = st.data_editor(
            df_default,
            num_rows="dynamic",
            key=editor_key,
            use_container_width=True,
            column_config={
                "Zeit_von": st.column_config.TextColumn("Zeit von", help="Früheste Ankunft, z. B. 08:00"),
                "Zeit_bis": st.column_config.TextColumn("Zeit bis", help="Späteste Ankunft, z. B. 17:00"),
                "Adresse": st.column_config.TextColumn("Adresse", help="Straße, Hausnummer, PLZ und Ort prüfen"),
            },
        )
    except Exception as exc:
        st.error(f"Fehler beim Lesen der Kundendatei: {exc}")

if "berechnen_geklickt" not in st.session_state:
    st.session_state.berechnen_geklickt = False
if st.button("Routen berechnen", type="primary"):
    st.session_state.berechnen_geklickt = True


# ---------------------------------------------------------------------------
# Tagesplanung
# ---------------------------------------------------------------------------
if st.session_state.berechnen_geklickt:
    try:
        if not api_key:
            st.error("Bitte zuerst einen HeiGIT-API-Key in der Seitenleiste eintragen.")
            st.stop()
        if file_kunden is None or df_kunden is None:
            st.error("Bitte eine gültige Kunden-Datei hochladen und die Tabelle prüfen.")
            st.stop()

        df_clean = df_kunden.copy()
        required_columns = {"Name", "Adresse", "Zeit_von", "Zeit_bis", "Gebiet"}
        missing_columns = required_columns - set(df_clean.columns)
        if missing_columns:
            st.error("Im Live-Editor fehlen erwartete Spalten: " + ", ".join(sorted(missing_columns)))
            st.stop()

        df_clean["Name"] = df_clean["Name"].apply(lambda value: "" if is_blank(value) else str(value).strip())
        df_clean["Adresse"] = df_clean["Adresse"].map(clean_address)
        df_clean["Gebiet"] = df_clean["Gebiet"].apply(lambda value: "" if is_blank(value) else str(value).strip())
        df_clean = df_clean[df_clean["Adresse"] != ""].reset_index(drop=True)
        if df_clean.empty:
            st.error("Es wurden keine Kunden mit einer gültigen Adresse gefunden.")
            st.stop()
        for idx in df_clean.index:
            if not df_clean.at[idx, "Name"]:
                df_clean.at[idx, "Name"] = f"Kunde {idx + 1}"

        # Zeitfenster vor dem Solver einmal zentral validieren.
        customer_earliest: list[int] = []
        customer_latest: list[int] = []
        time_errors: list[str] = []
        for idx, row in df_clean.iterrows():
            try:
                earliest = parse_time(row.get("Zeit_von", ""), 8 * 60)
                latest = parse_time(row.get("Zeit_bis", ""), 17 * 60)
                if earliest > latest:
                    raise ValueError(f"Zeit von {format_time(earliest)} liegt nach Zeit bis {format_time(latest)}.")
                customer_earliest.append(earliest)
                customer_latest.append(latest)
            except ValueError as exc:
                time_errors.append(f"Zeile {idx + 1} – {row['Name']}: {exc}")
        if time_errors:
            st.error("Bitte diese Kunden-Zeitfenster korrigieren:\n\n" + "\n".join(time_errors[:20]))
            if len(time_errors) > 20:
                st.caption(f"Weitere ungültige Zeitfenster: {len(time_errors) - 20}")
            st.stop()

        # Prüfe Schichtfenster, bevor eine API-Anfrage gestartet wird.
        shift_ranges: list[tuple[int, int]] = []
        for idx in range(num_vehicles):
            shift_start = parse_time(schicht_starts[idx], 8 * 60)
            shift_end = parse_time(schicht_ends[idx], 17 * 60)
            if shift_start > shift_end:
                st.error(f"Bei {fahrzeug_namen[idx]} liegt der Schichtbeginn nach dem Schichtende.")
                st.stop()
            shift_ranges.append((shift_start, shift_end))

        # Urlaubsplanung: wenn eine hochgeladene Datei nicht interpretierbar ist,
        # wird sichtbar gewarnt statt stillschweigend die Datei als leer zu behandeln.
        on_vacation = [False] * num_vehicles
        if file_urlaub is not None:
            try:
                df_u = load_file(file_urlaub)
                df_u.columns = [str(c).strip().lower() for c in df_u.columns]
                c_vehicle = find_column(df_u, ("fahrzeug", "auto", "fahrer"))
                c_from = find_column(df_u, ("von", "start", "beginn"))
                c_to = find_column(df_u, ("bis", "ende"))
                if not (c_vehicle and c_from and c_to):
                    st.warning("Urlaubsdatei ignoriert: Es müssen Spalten für Fahrzeug, Von/Start und Bis/Ende vorhanden sein.")
                else:
                    df_u["_von"] = pd.to_datetime(df_u[c_from], errors="coerce").dt.normalize()
                    df_u["_bis"] = pd.to_datetime(df_u[c_to], errors="coerce").dt.normalize()
                    chosen_date = pd.Timestamp(planungs_datum).normalize()
                    unknown_vehicles = set()
                    for _, row in df_u.iterrows():
                        vehicle_idx = match_vehicle_name(row[c_vehicle], fahrzeug_namen, vehicle_aliases)
                        if vehicle_idx is None and not is_blank(row[c_vehicle]):
                            unknown_vehicles.add(str(row[c_vehicle]))
                            continue
                        from_date, to_date = row["_von"], row["_bis"]
                        if vehicle_idx is not None and pd.notna(from_date) and pd.notna(to_date):
                            if from_date <= chosen_date <= to_date:
                                on_vacation[vehicle_idx] = True
                    if unknown_vehicles:
                        st.warning("Diese Fahrzeugnamen aus der Urlaubsdatei wurden nicht zugeordnet: " + ", ".join(sorted(unknown_vehicles)))
            except Exception as exc:
                st.warning(f"Urlaubsdatei konnte nicht sauber verarbeitet werden: {exc}. Bitte vor Verwendung der Tour prüfen.")

        # Start- und Endadressen kommen direkt aus den oben sichtbaren Fahrzeugfeldern.
        # Übernachtungsadressen sind dort bereits vorbelegt; manuelle Korrekturen bleiben erhalten.
        final_starts = list(starts)
        final_ends = list(ends)

        active_vehicles = [idx for idx in range(num_vehicles) if not on_vacation[idx]]
        vacation_vehicles = [idx for idx in range(num_vehicles) if on_vacation[idx]]
        for vehicle_idx in vacation_vehicles:
            st.info(f"🏖️ {fahrzeug_namen[vehicle_idx]} ist an diesem Tag im Urlaub.")
        if not active_vehicles:
            st.warning("An diesem Tag ist kein Fahrzeug im Einsatz (alle Fahrzeuge sind im Urlaub).")
            st.stop()

        active_starts = [final_starts[idx] for idx in active_vehicles]
        active_ends = [final_ends[idx] for idx in active_vehicles]
        for pos, vehicle_idx in enumerate(active_vehicles):
            if not active_starts[pos] or not active_ends[pos]:
                st.error(f"{fahrzeug_namen[vehicle_idx]} ist im Einsatz, hat aber keine vollständige Start-/Endadresse.")
                st.stop()

        # Gebiet -> bevorzugtes Fahrzeug. Unbekannte/unbesetzte Zuordnungen werden nicht erzwungen.
        area_mapping: dict[str, int] = {}
        if file_gebiete is not None:
            try:
                df_g = load_file(file_gebiete)
                df_g.columns = [str(c).strip().lower() for c in df_g.columns]
                c_area = find_column(df_g, ("gebiet", "zone", "bereich", "region"))
                c_vehicle = find_column(df_g, ("fahrzeug", "auto", "fahrer"))
                if not (c_area and c_vehicle):
                    st.warning("Gebietsdatei ignoriert: Spalten für Gebiet/Zone und Fahrzeug fehlen.")
                else:
                    unknown_areas = set()
                    for _, row in df_g.iterrows():
                        area_name = "" if is_blank(row[c_area]) else str(row[c_area]).strip()
                        vehicle_idx = match_vehicle_name(row[c_vehicle], fahrzeug_namen, vehicle_aliases)
                        if area_name and vehicle_idx is None and not is_blank(row[c_vehicle]):
                            unknown_areas.add(str(row[c_vehicle]))
                        if area_name and vehicle_idx is not None and vehicle_idx in active_vehicles:
                            area_mapping[area_name] = vehicle_idx
                    if unknown_areas:
                        st.warning("Diese Fahrzeugnamen aus der Gebietsdatei wurden nicht zugeordnet: " + ", ".join(sorted(unknown_areas)))
            except Exception as exc:
                st.warning(f"Gebietsdatei konnte nicht sauber verarbeitet werden: {exc}. Die Gebietspräferenzen werden ignoriert.")

        # HeiGIT: alle eindeutigen Start-, End- und Kundenadressen geokodieren.
        st.info("🔄 Prüfe Adressen über HeiGIT und berechne die Fahrzeit-/Distanzmatrix …")
        all_addresses = active_starts + active_ends + df_clean["Adresse"].tolist()
        unique_addresses = list(dict.fromkeys(all_addresses))
        geocoded: dict[str, tuple[list[float], str]] = {}
        geocode_errors: list[dict[str, str]] = []
        progress = st.progress(0.0, text="Adresssuche läuft …")
        for i, address in enumerate(unique_addresses):
            coords, label, error = get_koordinaten(api_key, address)
            if coords is None:
                geocode_errors.append({"Eingegebene Adresse": address, "Fehler": error or "Kein Treffer"})
            else:
                geocoded[address] = (coords, label)
            progress.progress((i + 1) / max(1, len(unique_addresses)), text=f"Adresse {i + 1} von {len(unique_addresses)} geprüft")
            time.sleep(0.05)
        progress.empty()

        if geocode_errors:
            st.error("Mindestens eine Adresse konnte nicht sicher aufgelöst werden. Es wurden bewusst keine Ersatzkoordinaten eingesetzt, damit keine falsche Route entsteht.")
            st.dataframe(pd.DataFrame(geocode_errors), use_container_width=True, hide_index=True)
            st.stop()

        # Ergebnislabels sichtbar machen; falsche Treffer sollten vor Einsatz korrigiert werden.
        labels_rows = []
        for address in unique_addresses:
            labels_rows.append({"Eingegebene Adresse": address, "HeiGIT-Treffer (bitte prüfen)": geocoded[address][1]})
        with st.expander("🔎 Geokodierte Adressen prüfen", expanded=False):
            st.caption("Ein erfolgreicher Geocoder-Treffer ist keine Garantie, dass Straße und Hausnummer exakt stimmen. Bitte auffällige Treffer vor der Verwendung prüfen.")
            st.dataframe(pd.DataFrame(labels_rows), use_container_width=True, hide_index=True)

        # Die öffentliche Matrix hat ein Limit für die Anzahl der Quell-Ziel-Paare.
        coordinates_ordered = (
            active_starts
            + active_ends
            + df_clean["Adresse"].tolist()
        )
        koordinaten = [geocoded[address][0] for address in coordinates_ordered]
        point_count = len(koordinaten)
        if point_count * point_count > MAX_MATRIX_PAIRS:
            st.error(
                f"Zu viele Punkte für eine einzelne HeiGIT-Matrix-Anfrage: {point_count} Punkte ergeben "
                f"{point_count * point_count:,} Quell-Ziel-Paare; aktuell werden höchstens {MAX_MATRIX_PAIRS:,} unterstützt. "
                "Bitte die Tagesplanung in kleinere Teilmengen aufteilen. Die App plant nicht mit einer unvollständigen Matrix."
            )
            st.stop()

        for idx, vehicle_idx in enumerate(active_vehicles):
            shift_start_min, shift_end_min = shift_ranges[vehicle_idx]
            if shift_start_min > shift_end_min:
                st.error(f"Ungültige Schichtzeiten für {fahrzeug_namen[vehicle_idx]}.")
                st.stop()

        duration_matrix, distance_matrix = get_distanzmatrix(
            api_key, tuple(tuple(float(v) for v in point) for point in koordinaten)
        )

        st.markdown("---")
        st.subheader(f"📅 Planungsergebnis für {planungs_datum.strftime('%d.%m.%Y')}")
        num_active = len(active_vehicles)
        customer_base = 2 * num_active
        starts_nodes = list(range(num_active))
        ends_nodes = list(range(num_active, 2 * num_active))
        manager = pywrapcp.RoutingIndexManager(len(duration_matrix), num_active, starts_nodes, ends_nodes)
        routing = pywrapcp.RoutingModel(manager)

        def make_cost_callback(original_vehicle_idx: int):
            def cost_cb(from_index: int, to_index: int) -> int:
                from_node = manager.IndexToNode(from_index)
                to_node = manager.IndexToNode(to_index)
                travel_minutes = int(math.ceil(duration_matrix[from_node][to_node] / 60))
                service_minutes = service_time_mins if from_node >= customer_base else 0
                area_penalty = 0
                if to_node >= customer_base:
                    customer_idx = to_node - customer_base
                    customer_area = str(df_clean.iloc[customer_idx].get("Gebiet", "")).strip()
                    preferred_vehicle = area_mapping.get(customer_area)
                    if preferred_vehicle is not None and preferred_vehicle != original_vehicle_idx:
                        area_penalty = AREA_PENALTY_MINUTES
                return travel_minutes + service_minutes + area_penalty
            return cost_cb

        for solver_vehicle_idx, original_vehicle_idx in enumerate(active_vehicles):
            callback_idx = routing.RegisterTransitCallback(make_cost_callback(original_vehicle_idx))
            routing.SetArcCostEvaluatorOfVehicle(callback_idx, solver_vehicle_idx)

        def raw_time_callback(from_index: int, to_index: int) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            travel_minutes = int(math.ceil(duration_matrix[from_node][to_node] / 60))
            service_minutes = service_time_mins if from_node >= customer_base else 0
            return travel_minutes + service_minutes

        time_callback_idx = routing.RegisterTransitCallback(raw_time_callback)
        routing.AddDimension(time_callback_idx, 1440, 1440, False, "Time")
        time_dimension = routing.GetDimensionOrDie("Time")

        def distance_callback(from_index: int, to_index: int) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            return int(distance_matrix[from_node][to_node])

        distance_callback_idx = routing.RegisterTransitCallback(distance_callback)
        max_meters = max_km_pro_tag * 1000
        routing.AddDimensionWithVehicleCapacity(
            distance_callback_idx,
            0,
            [max_meters] * num_active,
            True,
            "Distance",
        )

        for solver_vehicle_idx, original_vehicle_idx in enumerate(active_vehicles):
            shift_start_min, shift_end_min = shift_ranges[original_vehicle_idx]
            time_dimension.CumulVar(routing.Start(solver_vehicle_idx)).SetRange(shift_start_min, shift_end_min)
            time_dimension.CumulVar(routing.End(solver_vehicle_idx)).SetRange(shift_start_min, shift_end_min)

        # Kunden dürfen ausgelassen werden, falls sie realistisch nicht in die Tagesplanung passen.
        # Die Ausgabe nennt sie danach explizit, statt einen scheinbar vollständigen Plan vorzutäuschen.
        for customer_idx in range(len(df_clean)):
            node = customer_base + customer_idx
            customer_index = manager.NodeToIndex(node)
            routing.AddDisjunction([customer_index], UNSERVED_CUSTOMER_PENALTY)
            time_dimension.CumulVar(customer_index).SetRange(
                customer_earliest[customer_idx], customer_latest[customer_idx]
            )

        # Der Solver soll einen möglichst frühen, nachvollziehbaren Zeitplan ausgeben,
        # statt nur breite Zeitbereiche an den Zeitvariablen zu lassen.
        for solver_vehicle_idx in range(num_active):
            routing.AddVariableMinimizedByFinalizer(time_dimension.CumulVar(routing.Start(solver_vehicle_idx)))
            routing.AddVariableMinimizedByFinalizer(time_dimension.CumulVar(routing.End(solver_vehicle_idx)))
        for customer_idx in range(len(df_clean)):
            routing.AddVariableMinimizedByFinalizer(
                time_dimension.CumulVar(manager.NodeToIndex(customer_base + customer_idx))
            )

        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        search_parameters.time_limit.seconds = 10
        solution = routing.SolveWithParameters(search_parameters)

        if solution is None:
            st.error("Für diesen Tag konnte keine gültige Lösung gefunden werden. Bitte Adressen, Schichtzeiten, Zeitfenster und Kilometerlimit prüfen.")
            st.stop()

        # Die tatsächlich vom Solver zugewiesenen Kunden einsammeln, damit die
        # Kundenübersicht Fahrer, Reihenfolge und berechnete Ankunftszeit anzeigen kann.
        customer_assignments: dict[int, dict[str, Any]] = {}
        for solver_vehicle_idx, original_vehicle_idx in enumerate(active_vehicles):
            route_index = routing.Start(solver_vehicle_idx)
            stop_number = 0
            while not routing.IsEnd(route_index):
                route_node = manager.IndexToNode(route_index)
                if route_node >= customer_base:
                    customer_idx = route_node - customer_base
                    stop_number += 1
                    arrival_min = int(solution.Min(time_dimension.CumulVar(route_index)))
                    customer_assignments[customer_idx] = {
                        "Fahrer / Fahrzeug": fahrzeug_namen[original_vehicle_idx],
                        "Stopp Nr.": stop_number,
                        "Geplante Ankunft": format_time(arrival_min),
                        "Status": "In Tour eingeplant",
                    }
                route_index = solution.Value(routing.NextVar(route_index))

        # Liste der nicht eingeplanten Kunden mit konkreten Daten. Der Solver kann
        # nicht immer genau einen Grund bestimmen; deshalb keinen Grund erfinden.
        unserved_rows: list[dict[str, str]] = []
        for customer_idx in range(len(df_clean)):
            node = customer_base + customer_idx
            index = manager.NodeToIndex(node)
            if solution.Value(routing.NextVar(index)) == index:
                customer = df_clean.iloc[customer_idx]
                earliest = customer_earliest[customer_idx]
                latest = customer_latest[customer_idx]
                unserved_rows.append({
                    "Kundenname": str(customer["Name"]),
                    "Adresse": str(customer["Adresse"]),
                    "Zeitfenster": f"{format_time(earliest)}–{format_time(latest)}",
                    "Gebiet": "" if is_blank(customer.get("Gebiet", "")) else str(customer.get("Gebiet", "")),
                    "Status": "Nicht in Tagesroute eingeplant",
                    "Hinweis": "Zeitfenster, Schicht, Kilometerlimit und Tourenplanung prüfen.",
                })

        if unserved_rows:
            st.warning(
                f"⚠️ {len(unserved_rows)} Kunde(n) wurden heute nicht in eine Tour aufgenommen. "
                "Bitte die Liste prüfen und gegebenenfalls für einen anderen Tag einplanen."
            )

        # Kundenübersicht nach erfolgter Berechnung: die Zuordnung ist erst jetzt bekannt.
        assignment_rows: list[dict[str, str]] = []
        for customer_idx, customer in df_clean.iterrows():
            assignment = customer_assignments.get(customer_idx)
            if assignment:
                driver = str(assignment["Fahrer / Fahrzeug"])
                stop_number = str(assignment["Stopp Nr."])
                arrival = str(assignment["Geplante Ankunft"])
                status = str(assignment["Status"])
            else:
                driver = "Nicht zugewiesen"
                stop_number = "—"
                arrival = "—"
                status = "Nicht eingeplant"
            assignment_rows.append({
                "Kundenname": str(customer["Name"]),
                "Adresse": str(customer["Adresse"]),
                "Fahrer / Fahrzeug": driver,
                "Stopp Nr.": stop_number,
                "Geplante Ankunft": arrival,
                "Zeitfenster": f"{format_time(customer_earliest[customer_idx])}–{format_time(customer_latest[customer_idx])}",
                "Gebiet": "" if is_blank(customer.get("Gebiet", "")) else str(customer.get("Gebiet", "")),
                "Status": status,
            })

        st.subheader("📋 Kundenübersicht mit Fahrerzuordnung")
        st.caption("Diese Übersicht zeigt die vom Solver berechnete Zuordnung. Nicht eingeplante Kunden stehen ohne Fahrer in der Liste.")
        df_assignment = pd.DataFrame(assignment_rows)
        st.dataframe(df_assignment, use_container_width=True, hide_index=True)
        # Gut lesbare Kundenübersicht als normale TXT-Datei (direkt in Windows-Editor lesbar).
        assignment_report_lines = [
            "ROUTENPLANER - KUNDENÜBERSICHT",
            f"Planungsdatum: {planungs_datum.strftime('%d.%m.%Y')}",
            "=" * 72,
            "",
            f"Kunden insgesamt: {len(assignment_rows)}",
            f"In Tagesrouten eingeplant: {len(customer_assignments)}",
            f"Nicht in Tagesrouten eingeplant: {len(unserved_rows)}",
            "",
            "HINWEIS: Die Ankunftszeiten sind Planwerte. Bitte vor der Fahrt die Adressen und Zeiten kontrollieren.",
            "",
            "KUNDENLISTE",
            "=" * 72,
            "",
        ]
        for row_number, item in enumerate(assignment_rows, start=1):
            assignment_report_lines.extend([
                f"{row_number:02d}. {item['Kundenname']}",
                f"    Adresse: {item['Adresse']}",
                f"    Fahrer / Fahrzeug: {item['Fahrer / Fahrzeug']}",
                f"    Stoppnummer: {item['Stopp Nr.']}",
                f"    Geplante Ankunft: {item['Geplante Ankunft']}",
                f"    Vereinbartes Zeitfenster: {item['Zeitfenster']}",
                f"    Gebiet: {item['Gebiet'] or 'Keine Angabe'}",
                f"    Status: {item['Status']}",
                "",
                "-" * 72,
                "",
            ])
        assignment_txt = encode_readable_txt("\n".join(assignment_report_lines))
        st.download_button(
            label="📄 Kundenliste mit Fahrerzuordnung als TXT herunterladen",
            data=assignment_txt,
            file_name=f"kunden_fahrerzuordnung_{planungs_datum.strftime('%Y-%m-%d')}.txt",
            mime="text/plain; charset=utf-8",
            key="download_customer_assignments_txt",
        )

        # Eine eigene, lesbare TXT-Liste für Kunden, die nicht in eine Tagesroute passten.
        unserved_report_lines = [
            "ROUTENPLANER - NICHT IN TAGESROUTEN EINGEPLANTE KUNDEN",
            f"Planungsdatum: {planungs_datum.strftime('%d.%m.%Y')}",
            "=" * 72,
            "",
        ]
        if unserved_rows:
            unserved_report_lines.extend([
                f"Anzahl nicht eingeplanter Kunden: {len(unserved_rows)}",
                "Diese Kunden wurden vom Optimierer nicht in die berechneten Tagesrouten aufgenommen.",
                "Der genaue Grund ist nicht immer eindeutig. Bitte Zeitfenster, Schichtzeiten, Kilometerlimit und Adressen prüfen.",
                "",
            ])
            for row_number, item in enumerate(unserved_rows, start=1):
                unserved_report_lines.extend([
                    f"{row_number:02d}. {item['Kundenname']}",
                    f"    Adresse: {item['Adresse']}",
                    f"    Gewünschtes Zeitfenster: {item['Zeitfenster']}",
                    f"    Gebiet: {item['Gebiet'] or 'Keine Angabe'}",
                    f"    Status: {item['Status']}",
                    f"    Bitte prüfen: {item['Hinweis']}",
                    "",
                    "-" * 72,
                    "",
                ])
        else:
            unserved_report_lines.extend([
                "GUTE NACHRICHT: Alle Kunden wurden in die berechneten Tagesrouten aufgenommen.",
                "Es gibt keine Kunden in dieser Liste.",
                "",
            ])
        unserved_txt = encode_readable_txt("\n".join(unserved_report_lines))

        with st.expander(f"📋 Liste der heute nicht in eine Tour eingeplanten Kunden ({len(unserved_rows)})", expanded=False):
            if unserved_rows:
                st.caption(
                    "Diese Kunden wurden vom Optimierer nicht in die Tagesrouten aufgenommen. "
                    "Der genaue Grund ist nicht immer eindeutig; Zeitfenster, Schichtzeiten, Kilometerlimit und Adressen prüfen."
                )
                df_unserved = pd.DataFrame(unserved_rows)
                st.dataframe(df_unserved, use_container_width=True, hide_index=True)
            else:
                st.success("Alle Kunden wurden in die berechneten Tagesrouten aufgenommen.")
            st.download_button(
                label="📄 Liste der nicht eingeplanten Kunden als TXT herunterladen",
                data=unserved_txt,
                file_name=f"nicht_eingeplante_kunden_{planungs_datum.strftime('%Y-%m-%d')}.txt",
                mime="text/plain; charset=utf-8",
                key="download_unserved_customers_txt",
            )

        colors = ["blue", "red", "green", "purple", "orange", "darkred", "cadetblue", "darkgreen", "pink", "black"]
        route_report_sections: list[str] = []

        for solver_vehicle_idx, original_vehicle_idx in enumerate(active_vehicles):
            st.markdown(f"### 🚚 {fahrzeug_namen[original_vehicle_idx]}")
            col_route, col_map = st.columns([1, 1])
            index = routing.Start(solver_vehicle_idx)
            route_text_lines: list[str] = []
            route_plain_lines: list[str] = [
                f"FAHRER / FAHRZEUG: {fahrzeug_namen[original_vehicle_idx]}",
                "-" * 72,
                "",
            ]
            route_stop_number = 0
            route_distance_m = 0
            route_nodes: list[int] = []

            while not routing.IsEnd(index):
                node = manager.IndexToNode(index)
                route_nodes.append(node)
                arrival_min = int(solution.Min(time_dimension.CumulVar(index)))
                if node < num_active:
                    route_text_lines.append(
                        f"- **Start:** {active_starts[solver_vehicle_idx]} (Abfahrt: {format_time(arrival_min)})"
                    )
                    route_plain_lines.extend([
                        "START",
                        f"Adresse: {active_starts[solver_vehicle_idx]}",
                        f"Abfahrt: {format_time(arrival_min)}",
                        "",
                        "KUNDENSTOPPS",
                        "=" * 72,
                        "",
                    ])
                elif node >= customer_base:
                    customer_idx = node - customer_base
                    row = df_clean.iloc[customer_idx]
                    route_stop_number += 1
                    route_text_lines.append(
                        f"- **Kunde:** {row['Name']} — *{row['Adresse']}* (Ankunft: {format_time(arrival_min)})"
                    )
                    route_plain_lines.extend([
                        f"STOPP {route_stop_number:02d}: {row['Name']}",
                        f"    Adresse: {row['Adresse']}",
                        f"    Geplante Ankunft: {format_time(arrival_min)}",
                        f"    Vereinbartes Zeitfenster: {format_time(customer_earliest[customer_idx])} bis {format_time(customer_latest[customer_idx])}",
                        f"    Servicezeit vor Ort: {service_time_mins} Minuten",
                        "",
                    ])

                next_index = solution.Value(routing.NextVar(index))
                next_node = manager.IndexToNode(next_index)
                leg_distance = distance_matrix[node][next_node]
                if leg_distance >= UNREACHABLE_METERS:
                    raise RuntimeError("Die berechnete Route enthält eine nicht erreichbare Verbindung. Bitte Adressen prüfen.")
                route_distance_m += leg_distance
                index = next_index

            end_node = manager.IndexToNode(index)
            route_nodes.append(end_node)
            end_arrival_min = int(solution.Min(time_dimension.CumulVar(index)))
            route_text_lines.append(
                f"- **Ende:** {active_ends[solver_vehicle_idx]} (Ankunft: {format_time(end_arrival_min)})"
            )
            route_text_lines.append(
                f"🏁 **Gesamtstrecke:** {route_distance_m / 1000:.1f} km (Limit: {max_km_pro_tag} km)"
            )
            route_text = "\n".join(route_text_lines)
            route_plain_lines.extend([
                "RÜCKKEHR",
                f"Adresse: {active_ends[solver_vehicle_idx]}",
                f"Ankunft: {format_time(end_arrival_min)}",
                "",
                "TOURENÜBERSICHT",
                f"Anzahl Kundenstopps: {route_stop_number}",
                f"Gesamtstrecke: {route_distance_m / 1000:.1f} km",
                f"Kilometerlimit für dieses Fahrzeug: {max_km_pro_tag} km",
                "",
                "",
            ])
            route_report_sections.append("\n".join(route_plain_lines))

            with col_route:
                st.markdown(route_text)

            with col_map:
                if route_nodes:
                    first_coords = koordinaten[route_nodes[0]]
                    map_obj = folium.Map(location=[first_coords[1], first_coords[0]], zoom_start=11)
                    route_color = colors[original_vehicle_idx % len(colors)]

                    if len(route_nodes) <= MAX_DIRECTIONS_POINTS:
                        route_coords = tuple(tuple(koordinaten[node]) for node in route_nodes)
                        geometry, map_error = get_route_geometry(api_key, route_coords)
                        if geometry:
                            folium.GeoJson(
                                geometry,
                                style_function=lambda _feature, color=route_color: {"color": color, "weight": 4, "opacity": 0.85},
                            ).add_to(map_obj)
                        elif map_error:
                            st.warning(map_error)
                    else:
                        st.warning(
                            f"Die Kartenlinie wurde ausgelassen, weil diese Tour {len(route_nodes)} Wegpunkte enthält. "
                            f"Die Stopps werden trotzdem markiert."
                        )

                    for route_pos, node in enumerate(route_nodes):
                        lon, lat = koordinaten[node]
                        if route_pos == 0:
                            popup = f"Start: {active_starts[solver_vehicle_idx]}"
                            marker_color = "green"
                        elif route_pos == len(route_nodes) - 1:
                            popup = f"Ende: {active_ends[solver_vehicle_idx]}"
                            marker_color = "red"
                        else:
                            customer_idx = node - customer_base
                            if 0 <= customer_idx < len(df_clean):
                                customer = df_clean.iloc[customer_idx]
                                popup = f"{customer['Name']} — {customer['Adresse']}"
                            else:
                                popup = "Kundenstopp"
                            marker_color = route_color
                        folium.Marker(
                            [lat, lon],
                            popup=popup,
                            tooltip=popup,
                            icon=folium.Icon(color=marker_color),
                        ).add_to(map_obj)

                    st_folium(map_obj, width=500, height=400, returned_objects=[], key=f"map_{original_vehicle_idx}")
            st.divider()

        if route_report_sections:
            day_report_lines = [
                "ROUTENPLANER - TAGESBERICHT",
                f"Planungsdatum: {planungs_datum.strftime('%d.%m.%Y')}",
                "=" * 72,
                "",
                "KURZÜBERSICHT",
                f"Fahrzeuge im Einsatz: {len(active_vehicles)}",
                f"Kunden insgesamt: {len(df_clean)}",
                f"In Touren eingeplant: {len(customer_assignments)}",
                f"Nicht in Touren eingeplant: {len(unserved_rows)}",
                f"Servicezeit pro Kunde: {service_time_mins} Minuten",
                f"Maximale Strecke pro Fahrzeug: {max_km_pro_tag} km",
                "",
                "BITTE BEACHTEN",
                "Die Zeiten und Strecken sind berechnete Planwerte. Bitte Adressen, Zeitfenster und nicht eingeplante Kunden vor dem Einsatz kontrollieren.",
                "",
                "=" * 72,
                "TOUREN NACH FAHRZEUG",
                "=" * 72,
                "",
            ]
            day_report_lines.extend(route_report_sections)
            day_report_lines.extend([
                "",
                "=" * 72,
                "NICHT IN TAGESROUTEN EINGEPLANTE KUNDEN",
                "=" * 72,
                "",
            ])
            if unserved_rows:
                for row_number, item in enumerate(unserved_rows, start=1):
                    day_report_lines.extend([
                        f"{row_number:02d}. {item['Kundenname']}",
                        f"    Adresse: {item['Adresse']}",
                        f"    Zeitfenster: {item['Zeitfenster']}",
                        f"    Gebiet: {item['Gebiet'] or 'Keine Angabe'}",
                        f"    Hinweis: {item['Hinweis']}",
                        "",
                    ])
            else:
                day_report_lines.append("Alle Kunden wurden in die berechneten Tagesrouten aufgenommen.")
                day_report_lines.append("")

            day_report_txt = encode_readable_txt("\n".join(day_report_lines))
            st.success("🎉 Die Tagesplanung wurde berechnet. Du kannst die Berichte als normale TXT-Dateien herunterladen und mit dem Windows-Editor öffnen.")
            st.download_button(
                label="📄 Gesamten Tagesbericht als TXT herunterladen",
                data=day_report_txt,
                file_name=f"tagesbericht_{planungs_datum.strftime('%Y-%m-%d')}.txt",
                mime="text/plain; charset=utf-8",
                type="primary",
                key="download_day_report_txt",
            )

    except Exception as exc:
        st.error(f"Bei der Berechnung ist ein Fehler aufgetreten: {exc}")
        with st.expander("Technische Details für die Fehlersuche"):
            st.exception(exc)
