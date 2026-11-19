import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import requests
import pyotp
from datetime import datetime, timedelta
import pytz

st.set_page_config(page_title="Option Seller Algo Terminal", layout="wide")
st.title("🛡️ ऑप्शन सेलर एल्गो टर्मिनल (Angel One Auto-Live)")

@st.cache_data(ttl=3600)
def get_angel_scrip_master():
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    res = requests.get(url, timeout=15)
    return pd.DataFrame(res.json())

def angel_login(api_key, client_id, mpin, totp_secret):
    try:
        totp = pyotp.TOTP(totp_secret.strip().replace(" ", "")).now()
        url = "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": api_key.strip()
        }
        payload = {
            "clientcode": client_id.strip(),
            "password": mpin.strip(),
            "totp": totp
        }
        res = requests.post(url, json=payload, headers=headers, timeout=10).json()
        if res.get("status") and res.get("data", {}).get("jwtToken"):
            return res["data"]["jwtToken"], None
        return None, res.get("message", "लॉगिन विफल रहा। अपनी डिटेल्स चेक करें।")
    except Exception as e:
        return None, f"लॉगिन एरर: {str(e)}"

if "angel_jwt" not in st.session_state:
    try:
        if "ANGEL_API_KEY" in st.secrets and "ANGEL_CLIENT_ID" in st.secrets:
            jwt, err = angel_login(
                st.secrets["ANGEL_API_KEY"],
                st.secrets["ANGEL_CLIENT_ID"],
                st.secrets["ANGEL_MPIN"],
                st.secrets["ANGEL_TOTP"]
            )
            if jwt:
                st.session_state["angel_jwt"] = jwt
                st.session_state["angel_key"] = st.secrets["ANGEL_API_KEY"]
    except Exception:
        pass

def fetch_angel_live_oc(jwt_token, api_key, selected_idx, s_range):
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00",
        "X-PrivateKey": api_key.strip()
    }
    quote_url = "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/"

    idx_info = {
        "Nifty 50": {"exch": "NSE", "token": "99926000", "name": "NIFTY", "opt_exch": "NFO", "step": 50},
        "Bank Nifty": {"exch": "NSE", "token": "99926009", "name": "BANKNIFTY", "opt_exch": "NFO", "step": 100},
        "Sensex": {"exch": "BSE", "token": "99919000", "name": "SENSEX", "opt_exch": "BFO", "step": 100}
    }[selected_idx]

    spot_payload = {"mode": "LTP", "exchangeTokens": {idx_info["exch"]: [idx_info["token"]]}}
    spot_res = requests.post(quote_url, json=spot_payload, headers=headers, timeout=10).json()
    if not spot_res.get("status"):
        return None, None, None, f"Angel API Error: {spot_res.get('message', 'Token Expired')}"

    spot_price = float(spot_res["data"]["fetched"][0]["ltp"])
    step = idx_info["step"]
    atm_strike = round(spot_price / step) * step

    master_df = get_angel_scrip_master()
    opt_df = master_df[
        (master_df["name"] == idx_info["name"]) &
        (master_df["exch_seg"] == idx_info["opt_exch"]) &
        (master_df["instrumenttype"].isin(["OPTIDX"]))
    ].copy()

    if opt_df.empty:
        return None, None, None, "ऑप्शन कॉन्ट्रैक्ट लिस्ट नहीं मिली।"

    opt_df["expiry_dt"] = pd.to_datetime(opt_df["expiry"], format="%d%b%Y", errors="coerce")
    opt_df = opt_df.dropna(subset=["expiry_dt"])
    today = pd.Timestamp.now().normalize()
    future_expiries = sorted(opt_df[opt_df["expiry_dt"] >= today]["expiry_dt"].unique())
    if not future_expiries:
        return None, None, None, "कोई सक्रिय एक्सपायरी नहीं मिली।"

    nearest_exp = future_expiries[0]
    exp_str = pd.Timestamp(nearest_exp).strftime("%d-%b-%Y")
    exp_df = opt_df[opt_df["expiry_dt"] == nearest_exp].copy()

    exp_df["clean_strike"] = (exp_df["strike"].astype(float) / 100.0).astype(int)
    exp_df["opt_type"] = exp_df["symbol"].str[-2:]

    min_s = atm_strike - (s_range * step)
    max_s = atm_strike + (s_range * step)
    target_df = exp_df[(exp_df["clean_strike"] >= min_s) & (exp_df["clean_strike"] <= max_s)].copy()

    tokens = target_df["token"].astype(str).tolist()
    if not tokens:
        return None, None, None, "चुनी गई स्ट्राइक रेंज के टोकन नहीं मिले।"

    full_payload = {"mode": "FULL", "exchangeTokens": {idx_info["opt_exch"]: tokens[:50]}}
    q_res = requests.post(quote_url, json=full_payload, headers=headers, timeout=10).json()
    if not q_res.get("status"):
        return None, None, None, "ऑप्शन कोट्स लाने में समस्या आई।"

    fetched_map = {str(item["symbolToken"]): item for item in q_res["data"]["fetched"]}
    strikes_list = sorted(target_df["clean_strike"].unique())

    rows = []
    for st_val in strikes_list:
        ce_row = target_df[(target_df["clean_strike"] == st_val) & (target_df["opt_type"] == "CE")]
        pe_row = target_df[(target_df["clean_strike"] == st_val) & (target_df["opt_type"] == "PE")]

        ce_q = fetched_map.get(str(ce_row["token"].values[0]), {}) if not ce_row.empty else {}
        pe_q = fetched_map.get(str(pe_row["token"].values[0]), {}) if not pe_row.empty else {}

        ce_oi = int(ce_q.get("opnInterest", 0))
        ce_chg_oi = int(ce_q.get("netChangeOpnInterest", 0)) if "netChangeOpnInterest" in ce_q else 0

        pe_oi = int(pe_q.get("opnInterest", 0))
        pe_chg_oi = int(pe_q.get("netChangeOpnInterest", 0)) if "netChangeOpnInterest" in pe_q else 0

        rows.append({
            "CALL VOL": int(ce_q.get("tradeVolume", 0)),
            "CALL CHG OI": ce_chg_oi,
            "CALL OI": ce_oi,
            "CALL LTP": float(ce_q.get("ltp", 0.0)),
            "STRIKE": st_val,
            "PUT LTP": float(pe_q.get("ltp", 0.0)),
            "PUT OI": pe_oi,
            "PUT CHG OI": pe_chg_oi,
            "PUT VOL": int(pe_q.get("tradeVolume", 0))
        })

    return pd.DataFrame(rows), spot_price, exp_str, None

def fetch_angel_historical_spot(jwt_token, api_key, selected_idx, days_count):
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00",
        "X-PrivateKey": api_key.strip()
    }
    hist_url = "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData"

    idx_map = {
        "Nifty 50": {"exch": "NSE", "token": "99926000"},
        "Bank Nifty": {"exch": "NSE", "token": "99926009"},
        "Sensex": {"exch": "BSE", "token": "99919000"}
    }[selected_idx]

    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)
    all_candles = []

    remaining_days = days_count
    end_dt = now_ist

    while remaining_days > 0:
        chunk_days = min(30, remaining_days)
        start_dt = end_dt - timedelta(days=chunk_days)
        payload = {
            "exchange": idx_map["exch"],
            "symboltoken": idx_map["token"],
            "interval": "ONE_MINUTE",
            "fromdate": start_dt.strftime("%Y-%m-%d 09:15"),
            "todate": end_dt.strftime("%Y-%m-%d 15:30")
        }
        try:
            res = requests.post(hist_url, json=payload, headers=headers, timeout=15).json()
            if res.get("status") and res.get("data"):
                all_candles.extend(res["data"])
        except Exception:
            break

        end_dt = start_dt - timedelta(minutes=1)
        remaining_days -= chunk_days

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(all_candles, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    if df["Timestamp"].dt.tz is None:
        df["Timestamp"] = df["Timestamp"].dt.tz_localize("Asia/Kolkata")
    else:
        df["Timestamp"] = df["Timestamp"].dt.tz_convert("Asia/Kolkata")

    df = df.drop_duplicates(subset=["Timestamp"]).sort_values("Timestamp").set_index("Timestamp")
    return df

tab1, tab2 = st.tabs(["⛓️ ऑप्शन सेलर लाइव चेन (Angel One Live)", "📊 10:00 AM Chg OI (1-Min Angel Backtest)"])

with tab1:
    is_connected = "angel_jwt" in st.session_state
    with st.expander("🔑 Angel One SmartAPI कनेक्शन (Auto-Login सेटअप)", expanded=not is_connected):
        if is_connected:
            st.success("✅ Angel One ऑटो-कनेक्टेड है! अब आपको रोज़ डिटेल डालने की ज़रूरत नहीं है।")
        else:
            st.info("💡 रोज़ डिटेल डालने से बचने के लिए Streamlit Settings -> Secrets में अपनी डिटेल सेव करें, या नीचे मैनुअल भरें:")
            a1, a2, a3, a4 = st.columns(4)
            with a1:
                angel_api_key = st.text_input("API Key", type="password", placeholder="SmartAPI Key")
            with a2:
                angel_client_id = st.text_input("Client ID", placeholder="जैसे A123456")
            with a3:
                angel_mpin = st.text_input("MPIN", type="password", placeholder="4-अंकों का PIN")
            with a4:
                angel_totp = st.text_input("TOTP Secret Key", type="password", placeholder="Authenticator Secret")

            if st.button("🔗 Angel One से कनेक्ट करें", use_container_width=True):
                if angel_api_key and angel_client_id and angel_mpin and angel_totp:
                    with st.spinner("Angel One SmartAPI से जुड़ रहा है..."):
                        jwt, err = angel_login(angel_api_key, angel_client_id, angel_mp
