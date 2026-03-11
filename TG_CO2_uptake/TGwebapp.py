from __future__ import annotations

import re
from io import StringIO, BytesIO
import pandas as pd
import streamlit as st


def extract_sample_mass_g_from_text(text: str) -> float | None:
    header = "\n".join(text.splitlines()[:3000])

    m = re.search(
        r"(?im)^\s*#?\s*SAMPLE\s*MASS\s*(?:/\s*(mg|g))?\s*:\s*,\s*([0-9]+(?:\.[0-9]+)?)\s*$",
        header,
    )
    if m:
        unit = (m.group(1) or "mg").lower()
        val = float(m.group(2))
        return val / 1000.0 if unit == "mg" else val

    m = re.search(
        r"(?im)SAMPLE\s*MASS.*?/\s*(mg|g).*?,\s*([0-9]+(?:\.[0-9]+)?)",
        header,
    )
    if m:
        unit = m.group(1).lower()
        val = float(m.group(2))
        return val / 1000.0 if unit == "mg" else val

    return None



def read_tg_table_from_text(text: str) -> pd.DataFrame:
    lines = text.splitlines()

    header_row_idx = None
    for i, line in enumerate(lines[:5000]):
        low = line.lower()
        if "temp" in low and "mass" in low:
            header_row_idx = i
            break
    if header_row_idx is None:
        raise ValueError("Cannot find the data table header row containing 'Temp' and 'Mass'.")

    table_text = "\n".join(lines[header_row_idx:])

    df = pd.read_csv(
        StringIO(table_text),
        sep=None,
        engine="python",
    )

    df.columns = [str(c).strip() for c in df.columns]

    temp_col = next((c for c in df.columns if "temp" in c.lower()), None)
    if temp_col is None:
        raise ValueError("Temperature column not found (must contain 'Temp').")

    mass_col = None
    for c in df.columns:
        cl = c.lower().replace(" ", "")
        if cl == "mass%" or (("mass" in cl) and ("%" in cl)):
            mass_col = c
            break
    if mass_col is None:
        mass_col = next((c for c in df.columns if "mass" in c.lower()), None)
    if mass_col is None:
        raise ValueError("Mass column not found (must contain 'Mass').")

    out = df[[temp_col, mass_col]].copy()
    out.columns = ["Temp_C", "Mass_pct"]
    out["Temp_C"] = pd.to_numeric(out["Temp_C"], errors="coerce")
    out["Mass_pct"] = pd.to_numeric(out["Mass_pct"], errors="coerce")
    out = out.dropna().sort_values("Temp_C").reset_index(drop=True)

    if out.empty:
        raise ValueError("TG table is empty after cleaning.")
    return out


def nearest_mass(df: pd.DataFrame, target_temp: float) -> tuple[float, float]:
    idx = (df["Temp_C"] - target_temp).abs().idxmin()
    r = df.loc[idx]
    return float(r["Temp_C"]), float(r["Mass_pct"])


def carbon_uptake_eq5_from_text(
    filename: str,
    text: str,
    sample_mass_g: float,
    T_low: float,
    T_high: float,
) -> dict:
    df = read_tg_table_from_text(text)

    t_low_used, m_low = nearest_mass(df, T_low)
    t_high_used, m_high = nearest_mass(df, T_high)

    delta_mass_frac = (m_low - m_high) / 100.0
    C_CO2_g = sample_mass_g * delta_mass_frac

    M_high_g = sample_mass_g * (m_high / 100.0)

    uptake_g_per_g_anhydrous = C_CO2_g / (sample_mass_g * (1 - (m_high - m_low))) if 1 - (m_high - m_low) != 0 else float("nan")

    return {
        "file": filename,
        "sample_mass_g": sample_mass_g,
        "T_low_target": T_low,
        "T_high_target": T_high,
        "T_low_used": t_low_used,
        "T_high_used": t_high_used,
        "Mass_pct_at_T_low": m_low,
        "Mass_pct_at_T_high": m_high,
        "delta_mass_pct": m_low - m_high,
        "C_CO2_g": C_CO2_g,
        "M_T_high_g": M_high_g,
        "CO2_uptake_actual_g_per_g_anhydrous": uptake_g_per_g_anhydrous,
    }


def decode_bytes_best_effort(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1")


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    bio = BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="carbon_uptake")
    return bio.getvalue()


st.set_page_config(page_title="TG Carbon Uptake Calculator", layout="wide")

st.title("TG Carbon Uptake Calculator")
st.write("Upload TG → Choose the temperature range → Export")

with st.sidebar:
    st.header("Parameters")
    T_low = st.number_input("T_low (°C)", value=500.0, step=1.0)
    T_high = st.number_input("T_high (°C)", value=850.0, step=1.0)
    st.caption("Eq.(5): CO₂ uptake = C_CO2 / M_(T_high)")

uploaded_files = st.file_uploader(
    "Upload TG CSV files (one or multiple)",
    type=["csv", "txt"],
    accept_multiple_files=True,
)

if not uploaded_files:
    st.info("Upload TG CSV files to begin processing.")
    st.stop()

results: list[dict] = []
errors: list[dict] = []

for uf in uploaded_files:
    raw = uf.getvalue()
    text = decode_bytes_best_effort(raw)

    mass_g = extract_sample_mass_g_from_text(text)
    if mass_g is None:
        errors.append({"file": uf.name, "error": "Sample mass not found in file header."})
        continue

    try:
        res = carbon_uptake_eq5_from_text(
            filename=uf.name,
            text=text,
            sample_mass_g=mass_g,
            T_low=T_low,
            T_high=T_high,
        )
        results.append(res)
    except Exception as e:
        errors.append({"file": uf.name, "error": str(e)})

df_results = pd.DataFrame(results).sort_values("file") if results else pd.DataFrame()
df_errors = pd.DataFrame(errors) if errors else pd.DataFrame()

st.subheader("Results")
if not df_results.empty:
    st.dataframe(df_results, use_container_width=True)

    xlsx = to_excel_bytes(df_results)
    st.download_button(
        label="Download results as Excel (.xlsx)",
        data=xlsx,
        file_name="carbon_uptake_summary.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
else:
    st.warning("No valid results to display.")

if not df_errors.empty:
    st.subheader("Errors")
    st.dataframe(df_errors, use_container_width=True)
