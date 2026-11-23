import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import requests
import pyotp
import time
import os
import math
import base64
import json
from io import StringIO
from datetime import datetime, timedelta
import pytz

# ==========================================
# पेज सेटिंग्स (सीधा खोलने पर भी सुरक्षित चले)
# ==========================================
try:
    st.set_page_config(page_title="16 EMA + OI 40-Delta Algo (Pro Filters)", layout="wide")
except Exception:
    pass

st.title("📈 16 EMA Pullback + ATM±3 OI एल्गो (40 Delta Spread)")
st.caption("9:45–12:00 PM (दिन में 1 ट्रेड) | 3m/5m 16-EMA Slope + Fresh 2-3 Candle Breakout | ATM±3 OI (15% Buffer) | 40Δ Sell + Hedge | SL ₹1,700 | TSL +₹200 (@₹1,200) | OI Flip Exit | 2:59 PM")

EMA_HISTORY_FILE = "ema16_oi_trades_history.csv"
EMA_ACTIVE_FILE = "ema16_oi_active_trade.json"
EMA_BASE_OI_FILE = "daily_base_oi.json"
DEFAULT_GH_REPO = "Sarveshbaghel05/algo-trading-app"

# ==========================================
# GitHub पर स्थायी डेटा सेव करने के फंक्शन्स
# ==========================================
def _ema_gh_config():
    try:
        token = st.secrets.get("GITHUB_TOKEN", "").strip()
        repo = st.secrets.get("GITHUB_REPO", DEFAULT_GH_REPO).strip()
        return token, repo
    except Exception:
        return "", DEFAULT_GH_REPO

def ema_gh_read(filepath):
    token, repo = _ema_gh_config()
    if not token:
        return None, None, "GITHUB_TOKEN नहीं मिला"
    url = f"https://api.github.com/repos/{repo}/contents/{filepath}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"}
    try:
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            data = res.json()
            raw_text = base64.b64decode(data["content"]).decode("utf-8")
            return raw_text, data["sha"], "OK"
        elif res.status_code == 404:
            return None, None, "NEW_FILE"
        return None, None, f"GitHub Error {res.status_code}"
    except Exception as e:
        return None, None, str(e)

def ema_gh_write(filepath, text_content, commit_msg):
    token, repo = _ema_gh_config()
    if not token:
        return False, "GITHUB_TOKEN Missing"
    _, sha, _ = ema_gh_read(filepath)
    url = f"https://api.github.com/repos/{repo}/contents/{filepath}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"}
    payload = {
        "message": commit_msg,
        "content": base64.b64encode(text_content.encode("utf-8")).decode("utf-8")
    }
    if sha:
        payload["sha"] = sha
    try:
        res = requests.put(url, json=payload, headers=headers, timeout=12)
        if res.status_code in [200, 201]:
            return True, "Saved ✅"
        return False, f"Save Err {res.status_code}"
    except Exception as e:
        return False, str(e)

def ema_load_base_oi():
    ist = pytz.timezone("Asia/Kolkata")
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    if st.session_state.get("shared_base_oi_date") != today_str:
        st.session_state["shared_base_oi_date"] = today_str
        st.session_state["shared_base_oi_map"] = {}
        raw_json, _, _ = ema_gh_read(EMA_BASE_OI_FILE)
        if raw_json:
            try:
                data = json.loads(raw_json)
                if data.get("date") == today_str and isinstance(data.get("oi_map"), dict):
                    st.session_state["shared_base_oi_map"] = {str(k): int(v) for k, v in data["oi_map"].items()}
            except Exception:
                pass
    return st.session_state["shared_base_oi_map"], today_str

def ema_save_base_oi(oi_map, today_str):
    ema_gh_write(EMA_BASE_OI_FILE, json.dumps({"date": today_str, "oi_map": oi_map}), f"Save base OI {today_str}")

def ema_load_history():
    raw_csv, _, _ = ema_gh_read(EMA_HISTORY_FILE)
    if raw_csv is not None:
        try:
            df = pd.read_csv(StringIO(raw_csv))
            df.to_csv(EMA_HISTORY_FILE, index=False)
            return df.to_dict("records")
        except Exception:
            return []
    if os.path.exists(EMA_HISTORY_FILE):
        try:
            return pd.read_csv(EMA_HISTORY_FILE).to_dict("records")
        except Exception:
            return []
    return []

def ema_save_history(hist_list):
    df = pd.DataFrame(hist_list)
    try:
        df.to_csv(EMA_HISTORY_FILE, index=False)
    except Exception:
        pass
    csv_text = df.to_csv(index=False) if not df.empty else "तारीख,मोड,इंडेक्स,टाइमफ्रेम,दिशा,स्प्रेड,लॉट,एंट्री समय,एग्जिट समय,कारण,लाभ / हानि (₹)\n"
    ema_gh_write(EMA_HISTORY_FILE, csv_text, "Update 16EMA OI trade history")

def ema_load_active():
    raw_json, _, _ = ema_gh_read(EMA_ACTIVE_FILE)
    if raw_json:
        try:
            data = json.loads(raw_json)
            if isinstance(data, dict) and data.get("legs"):
                return data
        except Exception:
            pass
    return None

def ema_save_active(tr_dict):
    ema_gh_write(EMA_ACTIVE_FILE, json.dumps(tr_dict if tr_dict else {}, ensure_ascii=False), "Update 16EMA active trade")

# ==========================================
# Session State सेटअप
# ==========================================
if "ema_trade_history" not in st.session_state:
    st.session_state["ema_trade_history"] = ema_load_history()
if "ema_paper_capital" not in st.session_state:
    pnl_sum = sum(float(x.get("लाभ / हानि (₹)", 0)) for x in st.session_state["ema_trade_history"] if x.get("मोड") == "Paper")
    st.session_state["ema_paper_capital"] = 200000.0 + pnl_sum
if "ema_active_trade" not in st.session_state:
    st.session_state["ema_active_trade"] = ema_load_active()
if "ema_traded_date" not in st.session_state:
    st.session_state["ema_traded_date"] = ""
if "ema_last_tg_id" not in st.session_state:
    st.session_state["ema_last_tg_id"] = 0

# ==========================================
# Telegram Alert & Remote Control
# ==========================================
def ema_send_tg(msg_text):
    try:
        token = st.secrets.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = str(st.secrets.get("TELEGRAM_CHAT_ID", "")).strip()
        if not token or not chat_id:
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        res = requests.post(url, json={"chat_id": chat_id, "text": msg_text, "parse_mode": "HTML"}, timeout=5).json()
        return bool(res.get("ok"))
    except Exception:
        return False

def ema_check_tg_cmd():
    try:
        token = st.secrets.get("TELEGRAM_BOT_TOKEN", "").strip()
        allowed_id = str(st.secrets.get("TELEGRAM_CHAT_ID", "")).strip()
        if not token or not allowed_id:
            return None
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        res = requests.get(url, params={"offset": st.session_state["ema_last_tg_id"] + 1, "timeout": 1}, timeout=3).json()
        if not res.get("ok"):
            return None
        cmd = None
        for upd in res.get("result", []):
            uid = upd.get("update_id", 0)
            if uid > st.session_state["ema_last_tg_id"]:
                st.session_state["ema_last_tg_id"] = uid
            m = upd.get("message", {})
            if str(m.get("chat", {}).get("id", "")) == allowed_id:
                txt = m.get("text", "").strip().upper()
                if txt in ["/EXIT", "EXIT", "STOP", "CLOSE"]:
                    cmd = "EXIT"
                elif txt in ["/STATUS", "STATUS", "MTM", "PNL"]:
                    cmd = "STATUS"
        return cmd
    except Exception:
        return None

# ==========================================
# Angel One SmartAPI कनेक्शन और डेटा फंक्शन्स
# ==========================================
@st.cache_data(ttl=3600)
def ema_get_scrip_master():
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    return pd.DataFrame(requests.get(url, timeout=15).json())

def ema_angel_login(api_key, client_id, mpin, totp_secret):
    try:
        totp = pyotp.TOTP(totp_secret.strip().replace(" ", "")).now()
        url = "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword"
        headers = {
            "Content-Type": "application/json", "Accept": "application/json",
            "X-UserType": "USER", "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": api_key.strip()
        }
        payload = {"clientcode": client_id.strip(), "password": mpin.strip(), "totp": totp}
        res = requests.post(url, json=payload, headers=headers, timeout=10).json()
        if res.get("status") and res.get("data", {}).get("jwtToken"):
            return res["data"]["jwtToken"], None
        return None, res.get("message", "लॉगिन विफल")
    except Exception as e:
        return None, str(e)

if "angel_jwt" not in st.session_state:
    try:
        if "ANGEL_API_KEY" in st.secrets and "ANGEL_CLIENT_ID" in st.secrets:
            jwt, _ = ema_angel_login(
                st.secrets["ANGEL_API_KEY"], st.secrets["ANGEL_CLIENT_ID"],
                st.secrets["ANGEL_MPIN"], st.secrets["ANGEL_TOTP"]
            )
            if jwt:
                st.session_state["angel_jwt"] = jwt
                st.session_state["angel_key"] = st.secrets["ANGEL_API_KEY"]
    except Exception:
        pass

def ema_place_order(jwt, api_key, exch, symbol, token, side_act, qty):
    url = "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/placeOrder"
    headers = {
        "Authorization": f"Bearer {jwt}", "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": api_key.strip()
    }
    payload = {
        "variety": "NORMAL", "tradingsymbol": symbol, "symboltoken": str(token),
        "transactiontype": side_act, "exchange": exch, "ordertype": "MARKET",
        "producttype": "INTRADAY", "duration": "DAY", "price": "0",
        "squareoff": "0", "stoploss": "0", "quantity": str(quantity := qty)
    }
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10).json()
        if res.get("status"):
            return True, res.get("data", {}).get("orderid", "Placed")
        return False, res.get("message", "Rejected")
    except Exception as e:
        return False, str(e)

@st.cache_data(ttl=5)
def ema_get_live_ltp(jwt, api_key, exch, tokens_tup):
    url = "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/"
    headers = {
        "Authorization": f"Bearer {jwt}", "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": api_key.strip()
    }
    valid = [str(t) for t in tokens_tup if str(t) and str(t) != "0"]
    if not valid:
        return {}
    try:
        res = requests.post(url, json={"mode": "LTP", "exchangeTokens": {exch: valid}}, headers=headers, timeout=5).json()
        if res.get("status"):
            return {str(x["symbolToken"]): float(x["ltp"]) for x in res["data"]["fetched"]}
    except Exception:
        pass
    return {}

@st.cache_data(ttl=60)
def ema_fetch_intraday_candles(jwt, api_key, selected_idx, tf_mins, days_back=4):
    """
    Angel One से 1 महीने से 5 साल तक का डेटा लाता है
    और उसे 3-Min या 5-Min कैंडल + 16 EMA में बदलता है।
    """
    idx_map = {
        "Nifty 50": {"exch": "NSE", "token": "99926000", "yf": "^NSEI"},
        "Sensex": {"exch": "BSE", "token": "99919000", "yf": "^BSESN"}
    }[selected_idx]

    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    df_raw = pd.DataFrame()

    if jwt and api_key:
        headers = {
            "Authorization": f"Bearer {jwt}", "Content-Type": "application/json", "Accept": "application/json",
            "X-UserType": "USER", "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": api_key.strip()
        }
        hist_url = "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData"

        if days_back <= 90:
            api_interval = "ONE_MINUTE"
            chunk_limit = 30
        else:
            api_interval = "THREE_MINUTE" if tf_mins == 3 else "FIVE_MINUTE"
            chunk_limit = 90

        all_candles = []
        remaining = days_back
        end_dt = now_ist

        while remaining > 0:
            step_days = min(chunk_limit, remaining)
            start_dt = end_dt - timedelta(days=step_days)
            payload = {
                "exchange": idx_map["exch"],
                "symboltoken": idx_map["token"],
                "interval": api_interval,
                "fromdate": start_dt.strftime("%Y-%m-%d 09:15"),
                "todate": end_dt.strftime("%Y-%m-%d 15:30")
            }
            try:
                res = requests.post(hist_url, json=payload, headers=headers, timeout=15).json()
                if res.get("status") and res.get("data"):
                    all_candles.extend(res["data"])
                time.sleep(0.18)
            except Exception:
                break
            end_dt = start_dt - timedelta(minutes=1)
            remaining -= step_days

        if all_candles:
            df_raw = pd.DataFrame(all_candles, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
            df_raw["Timestamp"] = pd.to_datetime(df_raw["Timestamp"])
            if df_raw["Timestamp"].dt.tz is None:
                df_raw["Timestamp"] = df_raw["Timestamp"].dt.tz_localize("Asia/Kolkata")
            else:
                df_raw["Timestamp"] = df_raw["Timestamp"].dt.tz_convert("Asia/Kolkata")
            df_raw = df_raw.drop_duplicates(subset=["Timestamp"]).sort_values("Timestamp").set_index("Timestamp")

    if df_raw.empty:
        yf_int = "5m" if tf_mins == 5 else "2m"
        yf_per = "60d" if tf_mins == 5 else "30d"
        yf_df = yf.download(idx_map["yf"], period=yf_per, interval=yf_int, progress=False)
        if isinstance(yf_df.columns, pd.MultiIndex):
            yf_df.columns = yf_df.columns.get_level_values(0)
        if not yf_df.empty:
            if yf_df.index.tz is None:
                yf_df.index = yf_df.index.tz_localize("UTC").tz_convert("Asia/Kolkata")
            else:
                yf_df.index = yf_df.index.tz_convert("Asia/Kolkata")
            df_raw = yf_df[["Open", "High", "Low", "Close", "Volume"]].copy()

    if df_raw.empty:
        return pd.DataFrame()

    rule_str = f"{tf_mins}min"
    resampled = df_raw.resample(rule_str, offset="15min").agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum"
    }).dropna(subset=["Open", "Close"])

    resampled["EMA16"] = resampled["Close"].ewm(span=16, adjust=False).mean()
    return resampled

@st.cache_data(ttl=8)
def ema_fetch_live_oc(jwt, api_key, selected_idx):
    """Nifty (500pt hedge) या Sensex (1500pt hedge) के लिए पूरी लाइव Option Chain लाता है"""
    idx_info = {
        "Nifty 50": {"exch": "NSE", "token": "99926000", "name": "NIFTY", "opt_exch": "NFO", "step": 50, "hedge": 500},
        "Sensex": {"exch": "BSE", "token": "99919000", "name": "SENSEX", "opt_exch": "BFO", "step": 100, "hedge": 1500}
    }[selected_idx]

    headers = {
        "Authorization": f"Bearer {jwt}", "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": api_key.strip()
    }
    quote_url = "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/"

    try:
        sp_res = requests.post(quote_url, json={"mode": "LTP", "exchangeTokens": {idx_info["exch"]: [idx_info["token"]]}}, headers=headers, timeout=5).json()
        if not sp_res.get("status"):
            return None, None, None, {}, "Spot Error"
        spot_price = float(sp_res["data"]["fetched"][0]["ltp"])
    except Exception as e:
        return None, None, None, {}, str(e)

    step = idx_info["step"]
    atm = round(spot_price / step) * step

    master_df = ema_get_scrip_master()
    opt_df = master_df[
        (master_df["name"] == idx_info["name"]) &
        (master_df["exch_seg"] == idx_info["opt_exch"]) &
        (master_df["instrumenttype"] == "OPTIDX")
    ].copy()
    if opt_df.empty:
        return None, None, None, {}, "No Contracts"

    opt_df["expiry_dt"] = pd.to_datetime(opt_df["expiry"], format="%d%b%Y", errors="coerce")
    opt_df = opt_df.dropna(subset=["expiry_dt"])
    today = pd.Timestamp.now().normalize()
    future_exps = sorted(opt_df[opt_df["expiry_dt"] >= today]["expiry_dt"].unique())
    if not future_exps:
        return None, None, None, {}, "No Expiry"

    nearest_exp = future_exps[0]
    exp_str = pd.Timestamp(nearest_exp).strftime("%d-%b-%Y")
    dte_days = max(0.05, (pd.Timestamp(nearest_exp) - today).days + 0.5)
    exp_df = opt_df[opt_df["expiry_dt"] == nearest_exp].copy()
    exp_df["clean_strike"] = (exp_df["strike"].astype(float) / 100.0).astype(int)
    exp_df["opt_type"] = exp_df["symbol"].str[-2:]

    span = idx_info["hedge"] + (6 * step)
    target_df = exp_df[(exp_df["clean_strike"] >= atm - span) & (exp_df["clean_strike"] <= atm + span)].copy()
    tokens = target_df["token"].astype(str).tolist()

    contract_map = {}
    for _, r in target_df.iterrows():
        contract_map[(int(r["clean_strike"]), r["opt_type"])] = {
            "symbol": r["symbol"], "token": str(r["token"]), "exch": idx_info["opt_exch"]
        }

    fetched_map = {}
    for i in range(0, len(tokens), 50):
        batch = tokens[i:i+50]
        try:
            q_res = requests.post(quote_url, json={"mode": "FULL", "exchangeTokens": {idx_info["opt_exch"]: batch}}, headers=headers, timeout=5).json()
            if q_res.get("status") and q_res.get("data", {}).get("fetched"):
                for item in q_res["data"]["fetched"]:
                    fetched_map[str(item["symbolToken"])] = item
        except Exception:
            pass

    if not fetched_map:
        return None, None, None, {}, "Quotes Error"

    base_map, today_str = ema_load_base_oi()
    updated_base = False
    rows = []
    for k in sorted(target_df["clean_strike"].unique()):
        ce_tok = contract_map.get((k, "CE"), {}).get("token", "")
        pe_tok = contract_map.get((k, "PE"), {}).get("token", "")
        ce_q = fetched_map.get(ce_tok, {})
        pe_q = fetched_map.get(pe_tok, {})

        ce_oi = int(float(ce_q.get("opnInterest", 0) or 0))
        pe_oi = int(float(pe_q.get("opnInterest", 0) or 0))
        if ce_tok and ce_oi > 0 and ce_tok not in base_map:
            base_map[ce_tok] = ce_oi
            updated_base = True
        if pe_tok and pe_oi > 0 and pe_tok not in base_map:
            base_map[pe_tok] = pe_oi
            updated_base = True

        ce_chg = (ce_oi - base_map[ce_tok]) if (ce_tok in base_map and ce_oi > 0) else 0
        pe_chg = (pe_oi - base_map[pe_tok]) if (pe_tok in base_map and pe_oi > 0) else 0

        rows.append({
            "STRIKE": k,
            "CALL LTP": float(ce_q.get("ltp", 0.0) or 0.0),
            "PUT LTP": float(pe_q.get("ltp", 0.0) or 0.0),
            "CALL OI": ce_oi,
            "PUT OI": pe_oi,
            "CALL CHG OI": ce_chg,
            "PUT CHG OI": pe_chg,
            "DTE": dte_days
        })

    if updated_base:
        st.session_state["shared_base_oi_map"] = base_map
        ema_save_base_oi(base_map, today_str)

    return pd.DataFrame(rows), spot_price, exp_str, contract_map, None

# ==========================================
# Black-Scholes Delta, Option Pricing & Charges
# ==========================================
def calc_bs_delta(spot, strike, t_years, iv, opt_type="CE"):
    if t_years <= 1e-6:
        if opt_type == "CE":
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    r = 0.065
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    nd1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    return nd1 if opt_type == "CE" else (nd1 - 1.0)

def calc_bs_price(spot, strike, t_years, iv, opt_type="CE"):
    if t_years <= 1e-6:
        return max(0.05, spot - strike) if opt_type == "CE" else max(0.05, strike - spot)
    r = 0.065
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    d2 = d1 - iv * math.sqrt(t_years)
    nd1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    nd2 = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
    if opt_type == "CE":
        px = spot * nd1 - strike * math.exp(-r * t_years) * nd2
    else:
        px = strike * math.exp(-r * t_years) * (1.0 - nd2) - spot * (1.0 - nd1)
    return max(0.5, px)

def calc_spread_charges(sell_turnover, buy_turnover):
    total_to = sell_turnover + buy_turnover
    brokerage = 20.0 * 4  # 2 लेग Entry + 2 लेग Exit (₹20 per order)
    stt = sell_turnover * 0.0015
    exch = total_to * 0.0003553
    sebi = total_to * 0.000001
    stamp = buy_turnover * 0.00003
    gst = (brokerage + exch + sebi) * 0.18
    return round(brokerage + stt + exch + sebi + stamp + gst, 2)

def pick_40_delta_spread(direction, selected_idx, spot_price, full_df):
    """
    चुनी हुई दिशा में ~40 Delta वाला स्ट्राइक Sell करता है और:
    - Nifty में 500 पॉइंट दूर हेज Buy करता है
    - Sensex में 1500 पॉइंट दूर हेज Buy करता है
    """
    step = 50 if selected_idx == "Nifty 50" else 100
    hedge_pts = 500 if selected_idx == "Nifty 50" else 1500
    dte_days = float(full_df["DTE"].iloc[0]) if ("DTE" in full_df.columns and not full_df.empty) else 2.0
    t_years = max(0.05, dte_days) / 365.0
    iv = 0.14 if selected_idx == "Nifty 50" else 0.15

    side = "PE" if direction == "BULLISH" else "CE"
    col_ltp = "PUT LTP" if side == "PE" else "CALL LTP"

    best_strike = round(spot_price / step) * step
    min_diff = 999.0
    best_delta = 0.40

    for k in full_df["STRIKE"].unique():
        if side == "PE" and k > spot_price + step:
            continue
        if side == "CE" and k < spot_price - step:
            continue
        d_val = abs(calc_bs_delta(spot_price, k, t_years, iv, side))
        diff = abs(d_val - 0.40)
        if diff < min_diff:
            min_diff = diff
            best_strike = int(k)
            best_delta = round(d_val, 2)

    hedge_strike = (best_strike - hedge_pts) if side == "PE" else (best_strike + hedge_pts)

    def _px(k_val):
        m = full_df[full_df["STRIKE"] == k_val]
        if not m.empty and float(m[col_ltp].iloc[0]) > 0:
            return float(m[col_ltp].iloc[0])
        return round(calc_bs_price(spot_price, k_val, t_years, iv, side), 1)

    sell_ltp = _px(best_strike)
    buy_ltp = _px(hedge_strike)

    legs = [
        {"action": "BUY", "strike": hedge_strike, "side": side, "entry_ltp": buy_ltp},
        {"action": "SELL", "strike": best_strike, "side": side, "entry_ltp": sell_ltp}
    ]
    strat_name = f"{'Bull Put' if side == 'PE' else 'Bear Call'} (~{int(best_delta*100)}Δ)"
    summary = f"Sell {best_strike} {side} (Δ~{best_delta}) / Buy {hedge_strike} {side}"
    return strat_name, summary, legs, round(sell_ltp - buy_ltp, 2)

# ==========================================
# उन्नत 16 EMA Pullback + 15% OI Buffer + Slope + Fresh 2-3 Candle Breakout इंजन
# ==========================================
def evaluate_ema16_oi_setup(candle_df, spot_price, full_oc_df, selected_idx, tf_mins, oi_buffer_pct=15.0):
    step = 50 if selected_idx == "Nifty 50" else 100
    atm = round(spot_price / step) * step

    up_3 = full_oc_df[(full_oc_df["STRIKE"] > atm) & (full_oc_df["STRIKE"] <= atm + 3 * step)]
    dn_3 = full_oc_df[(full_oc_df["STRIKE"] < atm) & (full_oc_df["STRIKE"] >= atm - 3 * step)]
    call_chg_3 = int(up_3["CALL CHG OI"].sum()) if not up_3.empty else 0
    put_chg_3 = int(dn_3["PUT CHG OI"].sum()) if not dn_3.empty else 0

    mult = 1.0 + (oi_buffer_pct / 100.0)
    # 1) Minimum OI Buffer Filter: एक तरफ का Change in OI दूसरी तरफ से कम से कम 15% ज़्यादा हो
    if put_chg_3 > 0 and put_chg_3 > max(call_chg_3 * mult, call_chg_3 + 10000):
        oi_bias = "BULLISH"
    elif call_chg_3 > 0 and call_chg_3 > max(put_chg_3 * mult, put_chg_3 + 10000):
        oi_bias = "BEARISH"
    else:
        oi_bias = "NEUTRAL"

    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    closed_df = candle_df.copy()
    if len(closed_df) > 1:
        last_ts = closed_df.index[-1]
        if (now_ist - last_ts).total_seconds() < (tf_mins * 60 - 5):
            closed_df = closed_df.iloc[:-1]

    today_df = closed_df[closed_df.index.date == now_ist.date()]
    if len(today_df) < 5:
        return {
            "signal": "WAIT",
            "oi_bias": oi_bias,
            "ema_slope": "FLAT",
            "call_chg_3": call_chg_3,
            "put_chg_3": put_chg_3,
            "reason": "पर्याप्त बंद कैंडल्स का इंतज़ार हो रहा है...",
            "touch_info": None
        }

    # 2) 16 EMA Slope Filter (झुकाव की जाँच — पिछली 3 कैंडल्स से तुलना)
    ema_now = float(today_df["EMA16"].iloc[-1])
    ema_prev3 = float(today_df["EMA16"].iloc[-4])
    if ema_now > ema_prev3:
        ema_slope = "UP ↗️"
    elif ema_now < ema_prev3:
        ema_slope = "DOWN ↘️"
    else:
        ema_slope = "FLAT ➡️"

    latest_bar = today_df.iloc[-1]
    latest_time = today_df.index[-1].strftime("%H:%M")

    # 3) Fresh 2-3 Candle Breakout Filter: टच होने के बाद केवल अगली 2 से 3 कैंडल्स के अंदर ही ब्रेकआउट मान्य
    lookback = today_df.iloc[-4:-1]

    if oi_bias == "BULLISH" and ema_now > ema_prev3:
        for idx_t in range(len(lookback) - 1, -1, -1):
            t_bar = lookback.iloc[idx_t]
            t_time = lookback.index[idx_t].strftime("%H:%M")
            touched_ema = (t_bar["Low"] <= t_bar["EMA16"]) and (t_bar["High"] > t_bar["EMA16"])
            if touched_ema and latest_bar["Close"] > t_bar["High"]:
                return {
                    "signal": "BULLISH",
                    "oi_bias": oi_bias,
                    "ema_slope": ema_slope,
                    "call_chg_3": call_chg_3,
                    "put_chg_3": put_chg_3,
                    "reason": f"✅ BULLISH ENTRY! {t_time} कैंडल ने 16 EMA ({t_bar['EMA16']:.1f}) टच किया (High: {t_bar['High']:.1f}), और {latest_time} कैंडल ऊपर Close ({latest_bar['Close']:.1f}) हुई!",
                    "touch_info": {"touch_time": t_time, "touch_high": t_bar["High"], "close_px": latest_bar["Close"]}
                }

    if oi_bias == "BEARISH" and ema_now < ema_prev3:
        for idx_t in range(len(lookback) - 1, -1, -1):
            t_bar = lookback.iloc[idx_t]
            t_time = lookback.index[idx_t].strftime("%H:%M")
            touched_ema = (t_bar["High"] >= t_bar["EMA16"]) and (t_bar["Low"] < t_bar["EMA16"])
            if touched_ema and latest_bar["Close"] < t_bar["Low"]:
                return {
                    "signal": "BEARISH",
                    "oi_bias": oi_bias,
                    "ema_slope": ema_slope,
                    "call_chg_3": call_chg_3,
                    "put_chg_3": put_chg_3,
                    "reason": f"🔴 BEARISH ENTRY! {t_time} कैंडल ने 16 EMA ({t_bar['EMA16']:.1f}) टच किया (Low: {t_bar['Low']:.1f}), और {latest_time} कैंडल नीचे Close ({latest_bar['Close']:.1f}) हुई!",
                    "touch_info": {"touch_time": t_time, "touch_low": t_bar["Low"], "close_px": latest_bar["Close"]}
                }

    return {
        "signal": "WAIT",
        "oi_bias": oi_bias,
        "ema_slope": ema_slope,
        "call_chg_3": call_chg_3,
        "put_chg_3": put_chg_3,
        "reason": f"इंतज़ार: OI = {oi_bias} (15% Buffer) | 16 EMA Slope = {ema_slope} | ताज़ा टच + क्लोज़ की प्रतीक्षा है।",
        "touch_info": None
    }

# ==========================================
# ट्रेड एंट्री और एग्जिट (SL ₹1,700 | TSL +₹200 @ ₹1,200 | OI Flip | 2:59 PM)
# ==========================================
def ema_execute_entry(mode, selected_idx, tf_mins, direction, strat_name, summary, legs, lots, lot_qty, contract_map, trigger_src):
    ist = pytz.timezone("Asia/Kolkata")
    now_str = datetime.now(ist).strftime("%I:%M:%S %p")
    total_qty = lots * lot_qty
    exch = "BFO" if selected_idx == "Sensex" else "NFO"

    enriched = []
    for lg in legs:
        c_info = contract_map.get((lg["strike"], lg["side"]), {"symbol": f"{selected_idx}_{lg['strike']}_{lg['side']}", "token": "0", "exch": exch})
        enriched.append({
            "action": lg["action"], "strike": lg["strike"], "side": lg["side"],
            "entry_ltp": lg["entry_ltp"], "symbol": c_info["symbol"],
            "token": c_info["token"], "exch": c_info["exch"]
        })
    ordered = [x for x in enriched if x["action"] == "BUY"] + [x for x in enriched if x["action"] == "SELL"]

    if "LIVE ALGO" in mode:
        if "angel_jwt" not in st.session_state:
            return False, "Angel One लॉगिन नहीं है!"
        jwt = st.session_state["angel_jwt"]
        akey = st.session_state["angel_key"]
        done = []
        for lg in ordered:
            ok, msg = ema_place_order(jwt, akey, lg["exch"], lg["symbol"], lg["token"], lg["action"], total_qty)
            if not ok:
                for prev in reversed(done):
                    ema_place_order(jwt, akey, prev["exch"], prev["symbol"], prev["token"], "SELL" if prev["action"] == "BUY" else "BUY", total_qty)
                return False, f"ऑर्डर विफल ({lg['symbol']}): {msg}"
            done.append(lg)
            time.sleep(0.35)

    new_tr = {
        "mode": mode,
        "index": selected_idx,
        "tf": f"{tf_mins}m",
        "direction": direction,
        "strategy": strat_name,
        "summary": summary,
        "lots": lots,
        "qty": total_qty,
        "exch": exch,
        "legs": ordered,
        "entry_time": now_str,
        "sl_rs": -1700.0 * lots,
        "tsl_active": False
    }
    st.session_state["ema_active_trade"] = new_tr
    ema_save_active(new_tr)

    legs_txt = "\n".join([f"• <b>{l['action']}</b> {l['strike']} {l['side']} @ ₹{l['entry_ltp']}" for l in ordered])
    ema_send_tg(
        f"🚀 <b>16 EMA + OI ALGO ENTRY ({trigger_src})</b>\n"
        f"📊 <b>Index:</b> {selected_idx} ({tf_mins}-Min)\n"
        f"🧭 <b>Direction:</b> {direction} ({strat_name})\n"
        f"{legs_txt}\n"
        f"📦 <b>Qty:</b> {lots} Lot ({total_qty})\n"
        f"🛑 <b>Initial SL:</b> -₹{int(1700*lots):,} | 🎯 <b>TSL:</b> +₹{int(200*lots):,} (at +₹{int(1200*lots):,})\n"
        f"⏰ <b>Time:</b> {now_str}"
    )
    return True, f"{strat_name} एंट्री सफल!"

def ema_close_trade(curr_ltp_map, reason):
    tr = st.session_state["ema_active_trade"]
    if not tr:
        return
    ist = pytz.timezone("Asia/Kolkata")
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    exit_time = datetime.now(ist).strftime("%I:%M:%S %p")
    legs = tr["legs"]

    if "LIVE ALGO" in tr["mode"] and "angel_jwt" in st.session_state:
        jwt = st.session_state["angel_jwt"]
        akey = st.session_state["angel_key"]
        for lg in legs:
            if lg["action"] == "SELL":
                ema_place_order(jwt, akey, lg["exch"], lg["symbol"], lg["token"], "BUY", tr["qty"])
                time.sleep(0.35)
        for lg in legs:
            if lg["action"] == "BUY":
                ema_place_order(jwt, akey, lg["exch"], lg["symbol"], lg["token"], "SELL", tr["qty"])
                time.sleep(0.35)

    pnl_pts = 0.0
    for i, lg in enumerate(legs):
        px = curr_ltp_map.get(i, lg["entry_ltp"])
        pnl_pts += (lg["entry_ltp"] - px) if lg["action"] == "SELL" else (px - lg["entry_ltp"])
    pnl_rs = round(pnl_pts * tr["qty"], 2)

    if "PAPER" in tr["mode"]:
        st.session_state["ema_paper_capital"] += pnl_rs

    mode_short = "Paper" if "PAPER" in tr["mode"] else "LIVE"
    st.session_state["ema_trade_history"].insert(0, {
        "तारीख": today_str,
        "मोड": mode_short,
        "इंडेक्स": tr["index"],
        "टाइमफ्रेम": tr.get("tf", "5m"),
        "दिशा": tr.get("direction", "-"),
        "स्प्रेड": tr["summary"],
        "लॉट": tr["lots"],
        "एंट्री समय": tr["entry_time"],
        "एग्जिट समय": exit_time,
        "कारण": reason,
        "लाभ / हानि (₹)": pnl_rs
    })
    ema_save_history(st.session_state["ema_trade_history"])
    ema_save_active(None)

    ema_send_tg(
        f"🏁 <b>16 EMA + OI TRADE CLOSED ({mode_short})</b>\n"
        f"📊 <b>Index:</b> {tr['index']} | 📌 {tr['summary']}\n"
        f"📝 <b>Reason:</b> {reason}\n"
        f"💵 <b>Final P&L:</b> <b>₹{pnl_rs:+,.0f}</b>\n"
        f"⏰ <b>Time:</b> {exit_time}"
    )
    st.session_state["ema_active_trade"] = None

# ==========================================
# UI कंट्रोल्स और टैब्स
# ==========================================
tab_live, tab_bt = st.tabs(["🤖 Live / Paper ऑटो एल्गो + 16 EMA चार्ट", "📊 16 EMA + OI बैकटेस्ट (1M से 5Y + DTE)"])

with tab_live:
    c1, c2, c3, c4 = st.columns([2, 1.2, 1.2, 1])
    with c1:
        ema_mode = st.radio(
            "ट्रेडिंग मोड:",
            ["🟡 PAPER TRADING (वर्चुअल प्रैक्टिस)", "🔴 LIVE ALGO (असली पैसा)"],
            horizontal=True,
            key="ema_mode_sel"
        )
    with c2:
        ema_idx = st.selectbox("इंडेक्स चुनें", ["Nifty 50", "Sensex"], key="ema_idx_sel")
    with c3:
        ema_tf_label = st.selectbox("कैंडल टाइमफ्रेम", ["5 Minute", "3 Minute"], key="ema_tf_sel")
    with c4:
        ema_lots = st.number_input("लॉट (Lots)", min_value=1, max_value=25, value=1, key="ema_lots_sel")

    tf_mins = 5 if "5" in ema_tf_label else 3

    ema_live_ok = True
    if "LIVE ALGO" in ema_mode:
        st.warning("⚠️ LIVE ALGO मोड चालू है! असली पैसे से ऑर्डर लगेंगे।")
        ema_live_ok = st.checkbox("✅ मैं पुष्टि करता हूँ कि असली पैसे से ऑर्डर लगें", key="ema_live_confirm")

    t_auto1, t_auto2 = st.columns([1.5, 2.5])
    with t_auto1:
        auto_arm = st.toggle("🤖 ऑटो-एंट्री चालू रखें (9:45 AM – 12:00 PM | दिन में 1 ट्रेड)", value=False, key="ema_auto_arm")
    with t_auto2:
        st.info(f"💰 **पेपर बैलेंस:** `₹{st.session_state['ema_paper_capital']:,.0f}` | 🛑 **SL:** `-₹{1700*ema_lots:,}` | 🎯 **TSL:** `+₹{200*ema_lots:,}` (@ `+₹{1200*ema_lots:,}`) | ⏰ **Exit:** `2:59 PM`")

    @st.fragment(run_every=10)
    def render_ema16_live(mode_sel, selected_idx, tf_m, lots_cnt, is_unlocked, is_armed):
        ist = pytz.timezone("Asia/Kolkata")
        now_ist = datetime.now(ist)
        now_str = now_ist.strftime("%I:%M:%S %p")
        today_str = now_ist.strftime("%Y-%m-%d")
        curr_mins = now_ist.hour * 60 + now_ist.minute

        tg_cmd = ema_check_tg_cmd()

        lot_qty = 75 if selected_idx == "Nifty 50" else 20
        step = 50 if selected_idx == "Nifty 50" else 100
        hedge_pts = 500 if selected_idx == "Nifty 50" else 1500

        jwt = st.session_state.get("angel_jwt", "")
        akey = st.session_state.get("angel_key", "")

        candle_df = ema_fetch_intraday_candles(jwt, akey, selected_idx, tf_m, days_back=4)

        is_live_oc = False
        full_oc_df = None
        spot_price = float(candle_df["Close"].iloc[-1]) if not candle_df.empty else (24250.0 if selected_idx == "Nifty 50" else 79800.0)
        contract_map = {}

        if jwt and akey:
            oc_df, sp_live, _, c_map, _ = ema_fetch_live_oc(jwt, akey, selected_idx)
            if oc_df is not None and not oc_df.empty:
                full_oc_df = oc_df
                spot_price = sp_live
                contract_map = c_map
                is_live_oc = True

        if full_oc_df is None:
            atm_t = round(spot_price / step) * step
            np.random.seed(int(now_ist.strftime("%H%M")) + int(atm_t))
            strikes_t = [atm_t + (i * step) for i in range(-18, 19)]
            full_oc_df = pd.DataFrame({
                "STRIKE": strikes_t,
                "CALL LTP": [max(5.0, round(max(0, spot_price - k) + 80, 1)) for k in strikes_t],
                "PUT LTP": [max(5.0, round(max(0, k - spot_price) + 80, 1)) for k in strikes_t],
                "CALL OI": [100000] * len(strikes_t),
                "PUT OI": [100000] * len(strikes_t),
                "CALL CHG OI": [int(np.random.randint(-5, 25) * 1000) for _ in strikes_t],
                "PUT CHG OI": [int(np.random.randint(-5, 25) * 1000) for _ in strikes_t],
                "DTE": [2.0] * len(strikes_t)
            })

        setup = evaluate_ema16_oi_setup(candle_df, spot_price, full_oc_df, selected_idx, tf_m, oi_buffer_pct=15.0)
        oi_bias = setup["oi_bias"]
        ema_slope = setup.get("ema_slope", "FLAT")
        call_chg_3 = setup["call_chg_3"]
        put_chg_3 = setup["put_chg_3"]

        active_tr = st.session_state["ema_active_trade"]
        if active_tr is not None:
            st.markdown("### 🔥 चालू पोजीशन (Live MTM, Trailing SL & OI Reversal Guard)")
            legs = active_tr["legs"]
            tokens_tup = tuple(l["token"] for l in legs if l.get("token") and l["token"] != "0")
            ltp_map = ema_get_live_ltp(jwt, akey, active_tr["exch"], tokens_tup) if (is_live_oc and tokens_tup) else {}

            curr_px_map = {}
            live_pnl_pts = 0.0
            for i, lg in enumerate(legs):
                px = ltp_map.get(lg["token"], max(0.5, round(lg["entry_ltp"] + np.random.uniform(-2.5, 2.5), 1)))
                curr_px_map[i] = px
                live_pnl_pts += (lg["entry_ltp"] - px) if lg["action"] == "SELL" else (px - lg["entry_ltp"])

            live_pnl_rs = round(live_pnl_pts * active_tr["qty"], 2)
            lots_tr = active_tr["lots"]

            tsl_trigger_rs = 1200.0 * lots_tr
            tsl_lock_rs = 200.0 * lots_tr
            if not active_tr.get("tsl_active", False) and live_pnl_rs >= tsl_trigger_rs:
                active_tr["tsl_active"] = True
                active_tr["sl_rs"] = tsl_lock_rs
                st.session_state["ema_active_trade"] = active_tr
                ema_save_active(active_tr)
                ema_send_tg(f"🔒 <b>TRAILING SL LOCKED!</b>\nप्रॉफिट ₹{live_pnl_rs:,.0f} पार हो गया है। अब SL ट्रेल होकर <b>+₹{int(tsl_lock_rs):,}</b> पर सुरक्षित लॉक हो गया है!")

            cur_sl_rs = float(active_tr.get("sl_rs", -1700.0 * lots_tr))
            tr_dir = active_tr.get("direction", "BULLISH")
            # केवल 15% OI Buffer पार करके विपरीत सिग्नल बनने पर ही रिवर्सल एग्जिट होगा
            oi_flipped = (tr_dir == "BULLISH" and oi_bias == "BEARISH") or (tr_dir == "BEARISH" and oi_bias == "BULLISH")

            if tg_cmd == "STATUS":
                ema_send_tg(f"📊 <b>16 EMA ALGO STATUS</b>\n📌 {active_tr['summary']}\n💵 MTM: <b>₹{live_pnl_rs:+,.0f}</b>\n🛑 Active SL: <b>₹{cur_sl_rs:+,.0f}</b>\n📊 ATM±3 OI: {oi_bias}")

            if tg_cmd == "EXIT":
                ema_close_trade(curr_px_map, "Telegram /exit Command 📲")
                st.rerun()
            elif live_pnl_rs <= cur_sl_rs:
                exit_lbl = f"Trailing SL Hit (+₹{int(cur_sl_rs):,}) 🔒" if active_tr.get("tsl_active") else f"Initial SL Hit (-₹{int(abs(cur_sl_rs)):,}) 🛑"
                ema_close_trade(curr_px_map, exit_lbl)
                st.rerun()
            elif oi_flipped:
                ema_close_trade(curr_px_map, f"ATM±3 OI Reversed ({tr_dir} ➔ {oi_bias}) 🔄")
                st.rerun()
            elif curr_mins >= (14 * 60 + 59):
                ema_close_trade(curr_px_map, "2:59 PM Auto Square-Off ⏰")
                st.rerun()

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("इंडेक्स & दिशा", f"{active_tr['index']} ({tr_dir})", active_tr["strategy"])
            m2.metric("लाइव MTM लाभ / हानि", f"₹{live_pnl_rs:+,.0f}", f"Qty: {active_tr['qty']}")
            m3.metric("सक्रिय Stop-Loss", f"₹{cur_sl_rs:+,.0f}", "TSL Locked 🔒" if active_tr.get("tsl_active") else "Initial SL (-₹1700/lot)")
            m4.metric("ATM±3 OI (15% Buffer)", oi_bias, f"Put {put_chg_3:+,} | Call {call_chg_3:+,}")

            lcols = st.columns(len(legs))
            for i, lg in enumerate(legs):
                cpx = curr_px_map[i]
                lpnl = round(((lg["entry_ltp"] - cpx) if lg["action"] == "SELL" else (cpx - lg["entry_ltp"])) * active_tr["qty"], 0)
                lcols[i].metric(f"{lg['action']} {lg['strike']} {lg['side']}", f"₹{cpx}", f"Entry ₹{lg['entry_ltp']} | ₹{lpnl:+,.0f}")

            if st.button("🚨 अभी पोजीशन बंद करें (Square-Off)", type="primary", use_container_width=True):
                ema_close_trade(curr_px_map, "Manual Square-Off")
                st.rerun()

        else:
            in_entry_window = (9 * 60 + 45) <= curr_mins <= (12 * 60)
            already_traded_today = (st.session_state.get("ema_traded_date") == today_str)

            preview_dir = setup["signal"] if setup["signal"] in ["BULLISH", "BEARISH"] else (oi_bias if oi_bias in ["BULLISH", "BEARISH"] else "BULLISH")
            s_name, s_sum, p_legs, net_cr = pick_40_delta_spread(preview_dir, selected_idx, spot_price, full_oc_df)

            bg_c = "linear-gradient(90deg, #064e3b, #022c22)" if setup["signal"] == "BULLISH" else ("linear-gradient(90deg, #7f1d1d, #450a0a)" if setup["signal"] == "BEARISH" else "linear-gradient(90deg, #1e293b, #0f172a)")
            bd_c = "#10b981" if setup["signal"] == "BULLISH" else ("#ef4444" if setup["signal"] == "BEARISH" else "#f59e0b")

            win_badge = "🟢 एंट्री विंडो खुली है (9:45–12:00)" if in_entry_window else "⏳ एंट्री विंडो बंद (केवल 9:45 AM – 12:00 PM)"
            if already_traded_today:
                win_badge = "✅ आज का 1 ट्रेड पूरा हो चुका है"

            st.markdown(f"""
            <div style="background:{bg_c}; padding:16px 20px; border-radius:12px; border-left:6px solid {bd_c}; color:white; margin-bottom:12px;">
                <div style="font-size:13px; color:#e2e8f0;">⏱️ {now_str} | 📍 {selected_idx} Spot: <b>{spot_price:,.2f}</b> | 🕯️ Timeframe: <b>{tf_m}-Min</b> | 16 EMA Slope: <b>{ema_slope}</b> | {win_badge}</div>
                <div style="font-size:15px; margin-top:6px;">📊 <b>ATM±3 Change in OI (15% Buffer):</b> नीचे 3 Put Chg OI = <b>{put_chg_3:+,}</b> &nbsp;vs&nbsp; ऊपर 3 Call Chg OI = <b>{call_chg_3:+,}</b> &nbsp;➔&nbsp; <b>OI सिग्नल: {oi_bias}</b></div>
                <div style="font-size:17px; font-weight:bold; color:#fde047; margin:6px 0;">{setup['reason']}</div>
                <div style="font-size:14px;">🎯 <b>40-Delta सेटअप ({preview_dir}):</b> {s_sum} &nbsp;|&nbsp; 🛡️ हेज: <b>{hedge_pts} pts</b> &nbsp;|&nbsp; नेट क्रेडिट: <b>₹{round(net_cr*lot_qty*lots_cnt):,}</b></div>
            </div>
            """, unsafe_allow_html=True)

            if is_armed and in_entry_window and not already_traded_today and setup["signal"] in ["BULLISH", "BEARISH"]:
                if "PAPER" in mode_sel or is_unlocked:
                    ok, msg = ema_execute_entry(
                        mode_sel, selected_idx, tf_m, setup["signal"], s_name, s_sum, p_legs,
                        lots_cnt, lot_qty, contract_map, f"Auto 16-EMA ({tf_m}m)"
                    )
                    if ok:
                        st.session_state["ema_traded_date"] = today_str
                        st.success(f"🚀 {msg}")
                        st.rerun()

            b1, b2 = st.columns(2)
            with b1:
                bul_name, bul_sum, bul_legs, _ = pick_40_delta_spread("BULLISH", selected_idx, spot_price, full_oc_df)
                if st.button(f"🟢 मैनुअल BULLISH एंट्री ({bul_sum})", disabled=("LIVE ALGO" in mode_sel and not is_unlocked), use_container_width=True):
                    ok, msg = ema_execute_entry(mode_sel, selected_idx, tf_m, "BULLISH", bul_name, bul_sum, bul_legs, lots_cnt, lot_qty, contract_map, "Manual")
                    if ok:
                        st.session_state["ema_traded_date"] = today_str
                        st.rerun()
            with b2:
                ber_name, ber_sum, ber_legs, _ = pick_40_delta_spread("BEARISH", selected_idx, spot_price, full_oc_df)
                if st.button(f"🔴 मैनुअल BEARISH एंट्री ({ber_sum})", disabled=("LIVE ALGO" in mode_sel and not is_unlocked), use_container_width=True):
                    ok, msg = ema_execute_entry(mode_sel, selected_idx, tf_m, "BEARISH", ber_name, ber_sum, ber_legs, lots_cnt, lot_qty, contract_map, "Manual")
                    if ok:
                        st.session_state["ema_traded_date"] = today_str
                        st.rerun()

        if not candle_df.empty:
            chart_df = candle_df[candle_df.index.date == now_ist.date()]
            if len(chart_df) < 5:
                chart_df = candle_df.tail(60)
            fig = go.Figure()
            fig.add_trace(go.Candlestick(
                x=chart_df.index, open=chart_df["Open"], high=chart_df["High"],
                low=chart_df["Low"], close=chart_df["Close"], name=f"{selected_idx} ({tf_m}m)"
            ))
            fig.add_trace(go.Scatter(
                x=chart_df.index, y=chart_df["EMA16"], mode="lines",
                line=dict(color="#facc15", width=2.5), name="16 EMA"
            ))
            fig.update_layout(
                height=360, margin=dict(l=10, r=10, t=30, b=10),
                xaxis_rangeslider_visible=False, template="plotly_dark",
                title=f"{selected_idx} — {tf_m} Minute Chart & 16 EMA"
            )
            st.plotly_chart(fig, use_container_width=True)

        if st.session_state["ema_trade_history"]:
            st.markdown("### 📜 16 EMA + OI ट्रेड हिस्ट्री")
            st.dataframe(pd.DataFrame(st.session_state["ema_trade_history"]), use_container_width=True, hide_index=True)

    render_ema16_live(ema_mode, ema_idx, tf_mins, ema_lots, ema_live_ok, auto_arm)

# ==========================================
# TAB 2: 16 EMA + OI बैकटेस्टिंग इंजन (1M से 5Y + DTE Filter + Cost Toggle)
# ==========================================
with tab_bt:
    st.subheader("📊 16 EMA Pullback + ATM±3 OI + 40 Delta Spread बैकटेस्ट")

    PERIOD_OPTIONS = {
        "1 महीना (30 दिन)": 30,
        "3 महीना (90 दिन)": 90,
        "6 महीना (180 दिन)": 180,
        "9 महीना (270 दिन)": 270,
        "1 साल (365 दिन)": 365,
        "2 साल (730 दिन)": 730,
        "3 साल (1095 दिन)": 1095,
        "4 साल (1460 दिन)": 1460,
        "5 साल (1825 दिन)": 1825
    }

    bc1, bc2, bc3, bc4 = st.columns([1.3, 1.2, 1.5, 1])
    with bc1:
        bt_idx = st.selectbox("इंडेक्स", ["Nifty 50", "Sensex"], key="ema_bt_idx")
    with bc2:
        bt_tf = st.selectbox("टाइमफ्रेम", ["5 Minute", "3 Minute"], key="ema_bt_tf")
    with bc3:
        bt_period_lbl = st.selectbox("बैकटेस्ट अवधि चुनें", list(PERIOD_OPTIONS.keys()), index=4, key="ema_bt_days")
    with bc4:
        bt_lots = st.number_input("लॉट", min_value=1, max_value=25, value=1, key="ema_bt_lots")

    dte_opts = ["0 DTE (Expiry Day)", "1 DTE", "2 DTE", "3 DTE", "4 DTE"]
    sel_dtes = st.multiselect(
        "📅 DTE फ़िल्टर (चुनें कि कौन-कौन से दिन ट्रेड करना है — 0 DTE = Expiry Day):",
        options=dte_opts,
        default=dte_opts,
        key="ema_bt_dtes"
    )
    allowed_dtes = [int(x.split(" ")[0]) for x in sel_dtes]

    cost_mode = st.radio(
        "💰 ब्रोकरेज, स्लिपेज और टैक्स सेटिंग (लगाकर या बिना लगाए चेक करें):",
        [
            "✅ Brokerage + Slippage + Tax लगाकर (Net P&L)",
            "🚫 बिना Brokerage, Slippage और Tax के (Raw / Gross P&L)"
        ],
        horizontal=True,
        key="ema_bt_cost_mode"
    )
    apply_costs = cost_mode.startswith("✅")

    if st.button("🚀 16 EMA + OI बैकटेस्ट चलाएं (Run Backtest)", use_container_width=True):
        if not allowed_dtes:
            st.warning("⚠️ कृपया कम से कम एक DTE (जैसे 0 DTE या 1 DTE) जरूर चुनें!")
        else:
            tf_m_bt = 5 if "5" in bt_tf else 3
            days_n = PERIOD_OPTIONS[bt_period_lbl]
            jwt = st.session_state.get("angel_jwt", "")
            akey = st.session_state.get("angel_key", "")

            with st.spinner(f"Angel One SmartAPI से {bt_period_lbl} के {tf_m_bt}-मिनट डेटा पर बैकटेस्ट चल रहा है..."):
                df_c = ema_fetch_intraday_candles(jwt, akey, bt_idx, tf_m_bt, days_back=days_n)
                if df_c.empty:
                    st.error("डेटा लोड नहीं हो पाया। कृपया जांचें कि Angel One लॉगिन सक्रिय है।")
                else:
                    lot_qty = 75 if bt_idx == "Nifty 50" else 20
                    step = 50 if bt_idx == "Nifty 50" else 100
                    hedge_pts = 500 if bt_idx == "Nifty 50" else 1500
                    expiry_wd = 3 if bt_idx == "Nifty 50" else 4
                    total_qty = lot_qty * bt_lots
                    iv_base = 0.14 if bt_idx == "Nifty 50" else 0.15
                    slip_mult = 1.0 if apply_costs else 0.0

                    df_c["Date"] = df_c.index.date
                    logs = []

                    for dt, day_bars in df_c.groupby("Date"):
                        days_to_exp = (expiry_wd - dt.weekday()) % 7
                        if days_to_exp not in allowed_dtes:
                            continue

                        day_bars = day_bars.between_time("09:15", "14:59")
                        if len(day_bars) < 8:
                            continue

                        open_915 = float(day_bars["Open"].iloc[0])
                        t_base = (days_to_exp + 0.5) / 365.0

                        for i in range(4, len(day_bars)):
                            bar_ts = day_bars.index[i]
                            mins_now = bar_ts.hour * 60 + bar_ts.minute
                            if mins_now < (9 * 60 + 45) or mins_now > (12 * 60):
                                continue

                            cur_bar = day_bars.iloc[i]
                            # 16 EMA Slope Check (पिछली 3 कैंडल्स से तुलना)
                            ema_now = float(cur_bar["EMA16"])
                            ema_p3 = float(day_bars["EMA16"].iloc[i-3])

                            # केवल 2-3 कैंडल पुराना ताज़ा टच ही मान्य (Fresh Touch Filter)
                            prev_bars = day_bars.iloc[max(0, i-3):i]

                            # Minimum OI Buffer (कम से कम 0.18% स्पष्ट दिशा हो तभी OI सिग्नल मान्य)
                            day_chg_pct = ((cur_bar["Close"] - open_915) / open_915) * 100.0
                            oi_dir = "BULLISH" if day_chg_pct >= 0.18 else ("BEARISH" if day_chg_pct <= -0.18 else "NEUTRAL")

                            matched_dir = None
                            if oi_dir == "BULLISH" and ema_now > ema_p3:
                                for j in range(len(prev_bars)-1, -1, -1):
                                    tb = prev_bars.iloc[j]
                                    if tb["Low"] <= tb["EMA16"] and tb["High"] > tb["EMA16"] and cur_bar["Close"] > tb["High"]:
                                        matched_dir = "BULLISH"
                                        break
                            elif oi_dir == "BEARISH" and ema_now < ema_p3:
                                for j in range(len(prev_bars)-1, -1, -1):
                                    tb = prev_bars.iloc[j]
                                    if tb["High"] >= tb["EMA16"] and tb["Low"] < tb["EMA16"] and cur_bar["Close"] < tb["Low"]:
                                        matched_dir = "BEARISH"
                                        break

                            if matched_dir:
                                spot_ent = float(cur_bar["Close"])
                                side = "PE" if matched_dir == "BULLISH" else "CE"

                                best_k = round(spot_ent / step) * step
                                best_d = 999.0
                                for off in range(-6, 7):
                                    cand_k = best_k + off * step
                                    if side == "PE" and cand_k > spot_ent + step:
                                        continue
                                    if side == "CE" and cand_k < spot_ent - step:
                                        continue
                                    dv = abs(calc_bs_delta(spot_ent, cand_k, t_base, iv_base, side))
                                    if abs(dv - 0.40) < best_d:
                                        best_d = abs(dv - 0.40)
                                        best_k = cand_k

                                hedge_k = (best_k - hedge_pts) if side == "PE" else (best_k + hedge_pts)
                                raw_sell_ent = calc_bs_price(spot_ent, best_k, t_base, iv_base, side)
                                raw_buy_ent = calc_bs_price(spot_ent, hedge_k, t_base, iv_base, side)

                                sell_ent = max(0.5, raw_sell_ent - (max(0.35, raw_sell_ent * 0.004) * slip_mult))
                                buy_ent = raw_buy_ent + (max(0.35, raw_buy_ent * 0.01) * slip_mult)

                                sl_limit = -1700.0 * bt_lots
                                tsl_locked = False
                                exit_reason = "2:59 PM Exit ⏰"
                                s_exit = sell_ent
                                b_exit = buy_ent

                                rem_bars = day_bars.iloc[i+1:]
                                total_rem = max(1, len(rem_bars))
                                for step_idx, (ex_ts, ex_bar) in enumerate(rem_bars.iterrows()):
                                    decay_frac = max(0.05, 1.0 - (0.35 * (step_idx / total_rem)))
                                    t_now = t_base * decay_frac
                                    sp_now = float(ex_bar["Close"])

                                    s_now = calc_bs_price(sp_now, best_k, t_now, iv_base, side)
                                    b_now = calc_bs_price(sp_now, hedge_k, t_now, iv_base, side)
                                    mtm_rs = ((sell_ent - s_now) + (b_now - buy_ent)) * total_qty

                                    if not tsl_locked and mtm_rs >= (1200.0 * bt_lots):
                                        tsl_locked = True
                                        sl_limit = 200.0 * bt_lots

                                    flip_pct = ((sp_now - open_915) / open_915) * 100.0
                                    new_oi = "BULLISH" if flip_pct >= 0.18 else ("BEARISH" if flip_pct <= -0.18 else "NEUTRAL")
                                    oi_flip = (matched_dir == "BULLISH" and new_oi == "BEARISH") or (matched_dir == "BEARISH" and new_oi == "BULLISH")

                                    s_exit, b_exit = s_now, b_now
                                    if mtm_rs <= sl_limit:
                                        exit_reason = f"TSL Hit (+₹{int(sl_limit):,}) 🔒" if tsl_locked else f"SL Hit (-₹{int(abs(sl_limit)):,}) 🛑"
                                        break
                                    elif oi_flip:
                                        exit_reason = "OI Signal Reversed 🔄"
                                        break

                                s_exit_final = s_exit + (max(0.35, s_exit * 0.004) * slip_mult)
                                b_exit_final = max(0.05, b_exit - (max(0.35, b_exit * 0.01) * slip_mult))

                                gross_rs = round(((raw_sell_ent - s_exit) + (b_exit - raw_buy_ent)) * total_qty, 0)
                                after_slip_rs = ((sell_ent - s_exit_final) + (b_exit_final - buy_ent)) * total_qty

                                if apply_costs:
                                    sell_to = (sell_ent + b_exit_final) * total_qty
                                    buy_to = (s_exit_final + buy_ent) * total_qty
                                    brok_tax = calc_spread_charges(sell_to, buy_to)
                                    slip_rs = round(gross_rs - after_slip_rs, 0)
                                    total_cost_rs = round(brok_tax + slip_rs, 0)
                                    net_rs = round(after_slip_rs - brok_tax, 0)
                                else:
                                    total_cost_rs = 0.0
                                    net_rs = gross_rs

                                logs.append({
                                    "तारीख": str(dt),
                                    "DTE": f"{days_to_exp} DTE",
                                    "एंट्री समय": bar_ts.strftime("%H:%M"),
                                    "दिशा": matched_dir,
                                    "स्प्रेड": f"Sell {best_k} {side} / Buy {hedge_k} {side}",
                                    "नतीजा": exit_reason,
                                    "Gross P&L (₹)": gross_rs,
                                    "खर्च (Brok+Tax+Slip)": total_cost_rs,
                                    "Final P&L (₹)": net_rs
                                })
                                break

                    if logs:
                        res_df = pd.DataFrame(logs)
                        res_df["कुल पूंजी (₹)"] = res_df["Final P&L (₹)"].cumsum()
                        tot_gross = res_df["Gross P&L (₹)"].sum()
                        tot_cost = res_df["खर्च (Brok+Tax+Slip)"].sum()
                        tot_pnl = res_df["Final P&L (₹)"].sum()
                        wins = len(res_df[res_df["Final P&L (₹)"] > 0])
                        wr = round((wins / len(res_df)) * 100, 1)
                        max_dd = round((res_df["कुल पूंजी (₹)"].cummax() - res_df["कुल पूंजी (₹)"]).max(), 0)

                        badge_txt = "Brokerage + Slippage + Tax सहित (Net)" if apply_costs else "Bina Brokerage/Tax/Slippage (Raw Gross)"
                        st.caption(f"📌 **रिपोर्ट मोड:** `{badge_txt}`")

                        m1, m2, m3, m4, m5 = st.columns(5)
                        m1.metric("Final P&L", f"₹{tot_pnl:+,.0f}", f"{len(res_df)} ट्रेड ({bt_period_lbl})")
                        m2.metric("Gross P&L (बिना खर्च)", f"₹{tot_gross:+,.0f}", f"{tf_m_bt}-Min | 40Δ")
                        m3.metric("कुल खर्च (Brok+Tax+Slip)", f"-₹{int(tot_cost):,}", "ON ✅" if apply_costs else "OFF 🚫")
                        m4.metric("Win Rate", f"{wr}%", f"{wins} जीत / {len(res_df)-wins} हार")
                        m5.metric("Max Drawdown", f"-₹{int(max_dd):,}", f"+{hedge_pts}pt Hedge")

                        st.markdown("### 📅 DTE के अनुसार परफॉर्मेंस (0 DTE vs Non-Expiry Days)")
                        dte_summary = []
                        for d_lbl, grp in res_df.groupby("DTE"):
                            tc = len(grp)
                            wc = len(grp[grp["Final P&L (₹)"] > 0])
                            dte_summary.append({
                                "DTE": d_lbl,
                                "कुल ट्रेड": tc,
                                "जीत": wc,
                                "Win %": f"{round((wc/tc)*100, 1)}%",
                                "Final P&L (₹)": round(grp["Final P&L (₹)"].sum(), 0)
                            })
                        st.dataframe(pd.DataFrame(dte_summary), use_container_width=True, hide_index=True)

                        st.line_chart(res_df.set_index("तारीख")["कुल पूंजी (₹)"])
                        st.dataframe(res_df, use_container_width=True, hide_index=True)
                    else:
                        st.info("चुने गए DTE फ़िल्टर और अवधि में कोई 16 EMA + OI सेटअप नहीं मिला।")
