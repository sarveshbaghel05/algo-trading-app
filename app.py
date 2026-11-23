import streamlit as st

mode = st.radio(
    "Select Mode:",
    ["🚀 Live/Paper Algo Terminal", "🧠 Smart 4-Layer Pro Algo", "📊 Manual Options Simulator"],
    horizontal=True
)
if mode == "📊 Manual Options Simulator":
    exec(open("pages/simulator.py", encoding="utf-8").read())
    st.stop()
elif mode == "🧠 Smart 4-Layer Pro Algo":
    exec(open("pages/pro_algo.py", encoding="utf-8").read())
    st.stop()

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

# OTM IV Indicator सेफ इम्पोर्ट
try:
    from pages.otm_iv_indicator import evaluate_otm_iv_from_df
except ImportError:
    try:
        from otm_iv_indicator import evaluate_otm_iv_from_df
    except ImportError:
        def evaluate_otm_iv_from_df(selected_idx, spot_price, full_df, expiry_label):
            return {
                "avg_otm_ce_iv": 13.5, "ce_iv_change": 0.0,
                "avg_otm_pe_iv": 13.5, "pe_iv_change": 0.0,
                "signal": "WAIT", "spread_name": "Neutral",
                "sell_leg": "-", "buy_hedge": "-",
                "message": "OTM IV मॉड्यूल लोड हो रहा है।"
            }

# Greeks Regime Indicator सेफ इम्पोर्ट (सभी संभावित नामों और फोल्डर के साथ + इन-बिल्ट बैकअप)
evaluate_greeks_regime = None
for _mod_name in [
    "pages.greek_regime_indicator",
    "greek_regime_indicator",
    "pages.greeks_regime_indicator",
    "greeks_regime_indicator"
]:
    try:
        _mod = __import__(_mod_name, fromlist=["evaluate_greeks_regime"])
        evaluate_greeks_regime = getattr(_mod, "evaluate_greeks_regime")
        break
    except Exception:
        continue

if evaluate_greeks_regime is None:
    def evaluate_greeks_regime(selected_idx, spot_price, full_df, expiry_label="Live"):
        step = 50 if selected_idx == "Nifty 50" else 100
        atm = round(spot_price / step) * step
        up_df = full_df[(full_df["STRIKE"] > atm) & (full_df["STRIKE"] <= atm + 3 * step)]
        dn_df = full_df[(full_df["STRIKE"] < atm) & (full_df["STRIKE"] >= atm - 3 * step)]
        up_ce = int(up_df["CALL CHG OI"].sum()) if not up_df.empty else 0
        dn_pe = int(dn_df["PUT CHG OI"].sum()) if not dn_df.empty else 0

        if dn_pe > up_ce * 1.35 and dn_pe > 0:
            return {
                "direction": "UP",
                "headline": "🟢 तेज़ी का संकेत (Bullish — नीचे Put राइटिंग मज़बूत)",
                "bg_color": "linear-gradient(90deg, #064e3b, #022c22)",
                "border_color": "#10b981"
            }
        elif up_ce > dn_pe * 1.35 and up_ce > 0:
            return {
                "direction": "DOWN",
                "headline": "🔴 मंदी का संकेत (Bearish — ऊपर Call राइटिंग मज़बूत)",
                "bg_color": "linear-gradient(90deg, #7f1d1d, #450a0a)",
                "border_color": "#ef4444"
            }
        else:
            return {
                "direction": "SIDEWAYS",
                "headline": "🟡 साइडवेज़ बाज़ार (Range-Bound — दोनों तरफ़ संतुलित OI)",
                "bg_color": "linear-gradient(90deg, #1e293b, #0f172a)",
                "border_color": "#f59e0b"
            }

st.set_page_config(page_title="Option Seller Algo Terminal", layout="wide")
st.title("🛡️ ऑप्शन सेलर एल्गो टर्मिनल (2-Way Telegram Control)")

HISTORY_FILE = "paper_trades_history.csv"
ACTIVE_TRADE_FILE = "active_trade_state.json"
OC_RECORD_FILE = "recorded_oc_history.csv"
DEFAULT_GH_REPO = "Sarveshbaghel05/algo-trading-app"

# ==========================================
# Direct GitHub Permanent Storage (डेटा सुरक्षा)
# ==========================================
def _get_gh_config():
    try:
        token = st.secrets.get("GITHUB_TOKEN", "").strip()
        repo = st.secrets.get("GITHUB_REPO", DEFAULT_GH_REPO).strip()
        return token, repo
    except Exception:
        return "", DEFAULT_GH_REPO

def gh_read_file(filepath):
    token, repo = _get_gh_config()
    if not token:
        return None, None, "GITHUB_TOKEN Secret नहीं मिला"
    url = f"https://api.github.com/repos/{repo}/contents/{filepath}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    try:
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            data = res.json()
            raw_text = base64.b64decode(data["content"]).decode("utf-8")
            return raw_text, data["sha"], "OK"
        elif res.status_code == 404:
            return None, None, "NEW_FILE"
        else:
            return None, None, f"GitHub Error {res.status_code}"
    except Exception as e:
        return None, None, str(e)

def gh_write_file(filepath, text_content, commit_msg):
    token, repo = _get_gh_config()
    if not token:
        return False, "GITHUB_TOKEN Missing"
    _, sha, _ = gh_read_file(filepath)
    url = f"https://api.github.com/repos/{repo}/contents/{filepath}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }
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
        return False, f"Save Err {res.status_code}: {res.json().get('message', '')}"
    except Exception as e:
        return False, str(e)

def load_trade_history():
    raw_csv, _, status = gh_read_file(HISTORY_FILE)
    if raw_csv is not None:
        st.session_state["gh_sync_status"] = "☁️ GitHub Synced ✅"
        try:
            df = pd.read_csv(StringIO(raw_csv))
            df.to_csv(HISTORY_FILE, index=False)
            return df.to_dict("records")
        except Exception:
            return []
    elif status == "NEW_FILE":
        st.session_state["gh_sync_status"] = "☁️ GitHub Ready ✅"
    else:
        st.session_state["gh_sync_status"] = f"⚠️ {status}"

    if os.path.exists(HISTORY_FILE):
        try:
            return pd.read_csv(HISTORY_FILE).to_dict("records")
        except Exception:
            return []
    return []

def save_trade_history(history_list):
    df = pd.DataFrame(history_list)
    try:
        df.to_csv(HISTORY_FILE, index=False)
    except Exception:
        pass
    csv_text = df.to_csv(index=False) if not df.empty else "तारीख,मोड,इंडेक्स,स्प्रेड,लॉट,एंट्री समय,एग्जिट समय,कारण,लाभ / हानि (₹)\n"
    ok, msg = gh_write_file(HISTORY_FILE, csv_text, "Update trade history")
    st.session_state["gh_sync_status"] = f"☁️ {msg}" if ok else f"⚠️ {msg}"

def load_active_trade():
    raw_json, _, _ = gh_read_file(ACTIVE_TRADE_FILE)
    if raw_json:
        try:
            data = json.loads(raw_json)
            if isinstance(data, dict) and (data.get("legs") or data.get("sell_strike")):
                return data
        except Exception:
            pass
    return None

def save_active_trade(trade_dict):
    text_data = json.dumps(trade_dict if trade_dict else {}, ensure_ascii=False)
    ok, msg = gh_write_file(ACTIVE_TRADE_FILE, text_data, "Update active trade state")
    st.session_state["gh_sync_status"] = f"☁️ {msg}" if ok else f"⚠️ {msg}"

def sync_recorded_oc_to_github(force_now=False):
    """रिकॉर्ड किए गए Option Chain हिस्टोरिकल डेटा को सुरक्षित रूप से GitHub पर सिंक करना"""
    try:
        if not os.path.exists(OC_RECORD_FILE):
            raw_gh, _, _ = gh_read_file(OC_RECORD_FILE)
            if raw_gh:
                with open(OC_RECORD_FILE, "w", encoding="utf-8") as f:
                    f.write(raw_gh)
                return True, "GitHub से पुराना डेटा लोड हो गया ✅"
            return False, "अभी तक कोई रिकॉर्डेड डेटा नहीं है।"

        local_df = pd.read_csv(OC_RECORD_FILE)
        if local_df.empty:
            return False, "डेटा खाली है।"

        raw_gh, _, _ = gh_read_file(OC_RECORD_FILE)
        if raw_gh:
            try:
                gh_df = pd.read_csv(StringIO(raw_gh))
                merged_df = pd.concat([gh_df, local_df], ignore_index=True)
                merged_df = merged_df.drop_duplicates(subset=["Date", "Time", "Index", "STRIKE"], keep="last")
            except Exception:
                merged_df = local_df
        else:
            merged_df = local_df

        merged_df.to_csv(OC_RECORD_FILE, index=False)
        csv_payload = merged_df.to_csv(index=False)
        ok, msg = gh_write_file(OC_RECORD_FILE, csv_payload, "Auto-sync Option Chain historical data")
        if ok:
            st.session_state["last_oc_gh_sync_ts"] = time.time()
            return True, f"GitHub पर {merged_df['Date'].nunique()} दिन का डेटा सुरक्षित सेव हो गया ✅"
        return False, msg
    except Exception as e:
        return False, str(e)

def record_live_oc_snapshot(selected_idx, spot_price, exp_str, df_oc):
    """लाइव मार्केट (9:15 AM - 3:30 PM) में हर 1 मिनट का असली Option Chain डेटा सेव और ऑटो-सिंक करना"""
    try:
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        if now_ist.weekday() >= 5:
            return
        curr_mins = now_ist.hour * 60 + now_ist.minute
        if curr_mins < (9 * 60 + 15) or curr_mins > (15 * 60 + 30):
            return

        minute_key = f"{selected_idx}_{now_ist.strftime('%Y-%m-%d %H:%M')}"
        if st.session_state.get("last_recorded_minute") == minute_key:
            return

        if not os.path.exists(OC_RECORD_FILE) and not st.session_state.get("oc_gh_bootstrapped"):
            st.session_state["oc_gh_bootstrapped"] = True
            raw_gh, _, _ = gh_read_file(OC_RECORD_FILE)
            if raw_gh:
                with open(OC_RECORD_FILE, "w", encoding="utf-8") as f:
                    f.write(raw_gh)

        save_df = df_oc.copy()
        save_df.insert(0, "Timestamp", now_ist.strftime("%Y-%m-%d %H:%M:%S"))
        save_df.insert(1, "Date", now_ist.strftime("%Y-%m-%d"))
        save_df.insert(2, "Time", now_ist.strftime("%H:%M"))
        save_df.insert(3, "Index", selected_idx)
        save_df.insert(4, "Spot", round(spot_price, 2))
        save_df.insert(5, "Expiry", exp_str)

        write_header = not os.path.exists(OC_RECORD_FILE)
        save_df.to_csv(OC_RECORD_FILE, mode="a", header=write_header, index=False)
        st.session_state["last_recorded_minute"] = minute_key

        last_sync = st.session_state.get("last_oc_gh_sync_ts", 0)
        if time.time() - last_sync >= 900:
            sync_recorded_oc_to_github()
    except Exception:
        pass

# ==========================================
# Session State इनिशियलाइज़ेशन
# ==========================================
if "gh_sync_status" not in st.session_state:
    st.session_state["gh_sync_status"] = "Checking..."

if "trade_history" not in st.session_state:
    st.session_state["trade_history"] = load_trade_history()

if "paper_capital" not in st.session_state:
    saved_pnl = sum(
        float(t.get("लाभ / हानि (₹)", 0))
        for t in st.session_state["trade_history"]
        if t.get("मोड") == "Paper"
    )
    st.session_state["paper_capital"] = 200000.0 + saved_pnl

if "active_trade" not in st.session_state:
    st.session_state["active_trade"] = load_active_trade()
if "auto_traded_date" not in st.session_state:
    st.session_state["auto_traded_date"] = ""
if "last_tg_update_id" not in st.session_state:
    st.session_state["last_tg_update_id"] = 0
if "prev_close_oi_map" not in st.session_state:
    st.session_state["prev_close_oi_map"] = {}
if "prev_close_oi_date" not in st.session_state:
    st.session_state["prev_close_oi_date"] = ""
if "last_recorded_minute" not in st.session_state:
    st.session_state["last_recorded_minute"] = ""
if "last_oc_gh_sync_ts" not in st.session_state:
    st.session_state["last_oc_gh_sync_ts"] = 0
if "oc_gh_bootstrapped" not in st.session_state:
    st.session_state["oc_gh_bootstrapped"] = False

# ==========================================
# Telegram Alert और Remote Command फंक्शन्स
# ==========================================
def send_telegram_alert(message_text):
    """Telegram पर तुरंत ट्रेड और SL अलर्ट भेजना"""
    try:
        bot_token = st.secrets.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = str(st.secrets.get("TELEGRAM_CHAT_ID", "")).strip()
        if not bot_token or not chat_id:
            return False, "Telegram Secrets सेव नहीं हैं।"

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message_text,
            "parse_mode": "HTML"
        }
        res = requests.post(url, json=payload, timeout=5).json()
        if res.get("ok"):
            return True, "Sent"
        return False, res.get("description", "Telegram Error")
    except Exception as e:
        return False, str(e)

def check_telegram_commands():
    """Telegram से भेजे गए /exit या /status कमांड पढ़ना"""
    try:
        bot_token = st.secrets.get("TELEGRAM_BOT_TOKEN", "").strip()
        allowed_chat_id = str(st.secrets.get("TELEGRAM_CHAT_ID", "")).strip()
        if not bot_token or not allowed_chat_id:
            return None

        url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
        params = {"offset": st.session_state["last_tg_update_id"] + 1, "timeout": 1}
        res = requests.get(url, params=params, timeout=3).json()

        if not res.get("ok"):
            return None

        detected_cmd = None
        for update in res.get("result", []):
            upd_id = update.get("update_id", 0)
            if upd_id > st.session_state["last_tg_update_id"]:
                st.session_state["last_tg_update_id"] = upd_id

            msg = update.get("message", {})
            sender_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "").strip().upper()

            if sender_id == allowed_chat_id:
                if text in ["/EXIT", "EXIT", "STOP", "CLOSE"]:
                    detected_cmd = "EXIT"
                elif text in ["/STATUS", "STATUS", "MTM", "PNL"]:
                    detected_cmd = "STATUS"
        return detected_cmd
    except Exception:
        return None

# ==========================================
# Angel One SmartAPI हेल्पर फंक्शन्स
# ==========================================
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

def place_angel_order(jwt_token, api_key, exchange, trading_symbol, symbol_token, transaction_type, quantity):
    url = "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/placeOrder"
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
    payload = {
        "variety": "NORMAL",
        "tradingsymbol": trading_symbol,
        "symboltoken": str(symbol_token),
        "transactiontype": transaction_type,
        "exchange": exchange,
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": "0",
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(quantity)
    }
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10).json()
        if res.get("status"):
            return True, res.get("data", {}).get("orderid", "Placed")
        return False, res.get("message", "Order Rejected")
    except Exception as e:
        return False, str(e)

@st.cache_data(ttl=5)
def get_live_ltp_for_tokens(jwt_token, api_key, exchange, tokens_tuple):
    url = "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/"
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
    valid_tokens = [str(t) for t in tokens_tuple if str(t) and str(t) != "0"]
    if not valid_tokens:
        return {}
    payload = {"mode": "LTP", "exchangeTokens": {exchange: valid_tokens}}
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=5).json()
        if res.get("status"):
            return {str(item["symbolToken"]): float(item["ltp"]) for item in res["data"]["fetched"]}
    except Exception:
        pass
    return {}

@st.cache_data(ttl=3600)
def fetch_prev_day_closing_oi(jwt_token, api_key, exchange, symbol_token, today_date_str):
    """Angel One 1-Day Historical Candle से पिछले ट्रेडिंग दिन का असली बंद (Closing) OI निकालना"""
    hist_url = "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData"
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
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)
    from_dt = now_ist - timedelta(days=7)
    payload = {
        "exchange": exchange,
        "symboltoken": str(symbol_token),
        "interval": "ONE_DAY",
        "fromdate": from_dt.strftime("%Y-%m-%d 09:15"),
        "todate": now_ist.strftime("%Y-%m-%d 15:30")
    }
    try:
        res = requests.post(hist_url, json=payload, headers=headers, timeout=5).json()
        if res.get("status") and res.get("data"):
            candles = res["data"]
            prev_candles = [c for c in candles if str(c[0])[:10] < today_date_str]
            if prev_candles:
                last_prev = prev_candles[-1]
                if len(last_prev) >= 7 and last_prev[6] is not None:
                    return int(float(last_prev[6]))
    except Exception:
        pass
    return None

@st.cache_data(ttl=5)
def fetch_angel_live_oc_raw(jwt_token, api_key, selected_idx, s_range):
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

    try:
        spot_payload = {"mode": "LTP", "exchangeTokens": {idx_info["exch"]: [idx_info["token"]]}}
        spot_res = requests.post(quote_url, json=spot_payload, headers=headers, timeout=5).json()
        if not spot_res.get("status"):
            return None, None, None, {}, f"Angel API Error: {spot_res.get('message', 'Token Expired')}"

        spot_price = float(spot_res["data"]["fetched"][0]["ltp"])
    except Exception as e:
        return None, None, None, {}, f"Spot Fetch Error: {str(e)}"

    step = idx_info["step"]
    atm_strike = round(spot_price / step) * step

    master_df = get_angel_scrip_master()
    opt_df = master_df[
        (master_df["name"] == idx_info["name"]) &
        (master_df["exch_seg"] == idx_info["opt_exch"]) &
        (master_df["instrumenttype"].isin(["OPTIDX"]))
    ].copy()

    if opt_df.empty:
        return None, None, None, {}, "ऑप्शन कॉन्ट्रैक्ट लिस्ट नहीं मिली।"

    opt_df["expiry_dt"] = pd.to_datetime(opt_df["expiry"], format="%d%b%Y", errors="coerce")
    opt_df = opt_df.dropna(subset=["expiry_dt"])
    today = pd.Timestamp.now().normalize()
    future_expiries = sorted(opt_df[opt_df["expiry_dt"] >= today]["expiry_dt"].unique())
    if not future_expiries:
        return None, None, None, {}, "कोई सक्रिय एक्सपायरी नहीं मिली।"

    nearest_exp = future_expiries[0]
    exp_str = pd.Timestamp(nearest_exp).strftime("%d-%b-%Y")
    exp_df = opt_df[opt_df["expiry_dt"] == nearest_exp].copy()

    exp_df["clean_strike"] = (exp_df["strike"].astype(float) / 100.0).astype(int)
    exp_df["opt_type"] = exp_df["symbol"].str[-2:]

    # हेज नियम: Nifty 500, Bank Nifty 1000, Sensex 1500
    max_hedge = {"Nifty 50": 500, "Bank Nifty": 1000, "Sensex": 1500}[selected_idx]
    fetch_span = max(s_range * step, max_hedge + step, 3 * step)
    min_s = atm_strike - fetch_span
    max_s = atm_strike + fetch_span
    target_df = exp_df[(exp_df["clean_strike"] >= min_s) & (exp_df["clean_strike"] <= max_s)].copy()

    tokens = target_df["token"].astype(str).tolist()
    if not tokens:
        return None, None, None, {}, "चुनी गई स्ट्राइक रेंज के टोकन नहीं मिले।"

    contract_map = {}
    for _, r in target_df.iterrows():
        contract_map[(int(r["clean_strike"]), r["opt_type"])] = {
            "symbol": r["symbol"],
            "token": str(r["token"]),
            "exch": idx_info["opt_exch"]
        }

    fetched_map = {}
    for i in range(0, len(tokens), 50):
        batch = tokens[i:i+50]
        full_payload = {"mode": "FULL", "exchangeTokens": {idx_info["opt_exch"]: batch}}
        try:
            q_res = requests.post(quote_url, json=full_payload, headers=headers, timeout=5).json()
            if q_res.get("status") and q_res.get("data", {}).get("fetched"):
                for item in q_res["data"]["fetched"]:
                    fetched_map[str(item["symbolToken"])] = item
        except Exception:
            pass

    if not fetched_map:
        return None, None, None, {}, "ऑप्शन कोट्स लाने में समस्या आई।"

    strikes_list = sorted(target_df["clean_strike"].unique())
    raw_rows = []
    for st_val in strikes_list:
        ce_info = contract_map.get((st_val, "CE"), {})
        pe_info = contract_map.get((st_val, "PE"), {})

        ce_tok = ce_info.get("token", "")
        pe_tok = pe_info.get("token", "")

        ce_q = fetched_map.get(ce_tok, {})
        pe_q = fetched_map.get(pe_tok, {})

        ce_oi = int(float(ce_q.get("opnInterest", 0) or 0))
        ce_api_chg = int(float(ce_q.get("netChangeOpnInterest", 0) or 0))

        pe_oi = int(float(pe_q.get("opnInterest", 0) or 0))
        pe_api_chg = int(float(pe_q.get("netChangeOpnInterest", 0) or 0))

        raw_rows.append({
            "EXCH": idx_info["opt_exch"],
            "CE_TOKEN": ce_tok,
            "PE_TOKEN": pe_tok,
            "CALL VOL": int(float(ce_q.get("tradeVolume", 0) or 0)),
            "API_CE_CHG": ce_api_chg,
            "CALL OI": ce_oi,
            "CALL LTP": float(ce_q.get("ltp", 0.0) or 0.0),
            "STRIKE": st_val,
            "PUT LTP": float(pe_q.get("ltp", 0.0) or 0.0),
            "PUT OI": pe_oi,
            "API_PE_CHG": pe_api_chg,
            "PUT VOL": int(float(pe_q.get("tradeVolume", 0) or 0))
        })

    return pd.DataFrame(raw_rows), spot_price, exp_str, contract_map, None

def fetch_angel_live_oc(jwt_token, api_key, selected_idx, s_range):
    raw_df, spot_price, exp_str, contract_map, err = fetch_angel_live_oc_raw(jwt_token, api_key, selected_idx, s_range)
    if raw_df is None or raw_df.empty:
        return raw_df, spot_price, exp_str, contract_map, err

    ist = pytz.timezone('Asia/Kolkata')
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    if st.session_state["prev_close_oi_date"] != today_str:
        st.session_state["prev_close_oi_date"] = today_str
        st.session_state["prev_close_oi_map"] = {}

    df = raw_df.copy()
    call_chgs = []
    put_chgs = []

    for _, row in df.iterrows():
        exch = row["EXCH"]
        ce_tok = row["CE_TOKEN"]
        pe_tok = row["PE_TOKEN"]
        ce_oi = int(row["CALL OI"])
        pe_oi = int(row["PUT OI"])

        if ce_tok and ce_tok not in st.session_state["prev_close_oi_map"]:
            prev_ce = fetch_prev_day_closing_oi(jwt_token, api_key, exch, ce_tok, today_str)
            if prev_ce is not None and prev_ce > 0:
                st.session_state["prev_close_oi_map"][ce_tok] = prev_ce
            elif int(row["API_CE_CHG"]) != 0:
                st.session_state["prev_close_oi_map"][ce_tok] = ce_oi - int(row["API_CE_CHG"])
            else:
                st.session_state["prev_close_oi_map"][ce_tok] = ce_oi
            time.sleep(0.05)

        if pe_tok and pe_tok not in st.session_state["prev_close_oi_map"]:
            prev_pe = fetch_prev_day_closing_oi(jwt_token, api_key, exch, pe_tok, today_str)
            if prev_pe is not None and prev_pe > 0:
                st.session_state["prev_close_oi_map"][pe_tok] = prev_pe
            elif int(row["API_PE_CHG"]) != 0:
                st.session_state["prev_close_oi_map"][pe_tok] = pe_oi - int(row["API_PE_CHG"])
            else:
                st.session_state["prev_close_oi_map"][pe_tok] = pe_oi
            time.sleep(0.05)

        if ce_tok in st.session_state["prev_close_oi_map"] and ce_oi > 0:
            ce_chg = ce_oi - st.session_state["prev_close_oi_map"][ce_tok]
        else:
            ce_chg = int(row["API_CE_CHG"])

        if pe_tok in st.session_state["prev_close_oi_map"] and pe_oi > 0:
            pe_chg = pe_oi - st.session_state["prev_close_oi_map"][pe_tok]
        else:
            pe_chg = int(row["API_PE_CHG"])

        call_chgs.append(ce_chg)
        put_chgs.append(pe_chg)

    df["CALL CHG OI"] = call_chgs
    df["PUT CHG OI"] = put_chgs
    final_cols = ["CALL VOL", "CALL CHG OI", "CALL OI", "CALL LTP", "STRIKE", "PUT LTP", "PUT OI", "PUT CHG OI", "PUT VOL"]
    final_df = df[final_cols]

    record_live_oc_snapshot(selected_idx, spot_price, exp_str, final_df)

    return final_df, spot_price, exp_str, contract_map, None

# ==========================================
# मल्टी-लेग (Bull Put / Bear Call / Straddle + Hedge) एंट्री और एग्जिट इंजन
# ==========================================
def build_strategy_plan(direction, selected_idx, atm_strike, hedge_pts, full_df):
    """दिशा सिग्नल (UP / DOWN / SIDEWAYS) के हिसाब से सही स्ट्राइक और लेग्स तैयार करना"""
    def _get_ltp(strike_val, side_type):
        row = full_df[full_df["STRIKE"] == strike_val]
        col = "CALL LTP" if side_type == "CE" else "PUT LTP"
        if not row.empty:
            return float(row[col].iloc[0])
        return 120.0 if strike_val == atm_strike else 30.0

    if direction == "UP":
        buy_k = atm_strike - hedge_pts
        sell_k = atm_strike
        legs = [
            {"action": "BUY", "strike": buy_k, "side": "PE", "entry_ltp": _get_ltp(buy_k, "PE")},
            {"action": "SELL", "strike": sell_k, "side": "PE", "entry_ltp": _get_ltp(sell_k, "PE")},
        ]
        strat_name = "Bull Put Spread (तेज़ी का ट्रेड)"
        summary = f"Sell {sell_k} PE / Buy {buy_k} PE"
    elif direction == "DOWN":
        buy_k = atm_strike + hedge_pts
        sell_k = atm_strike
        legs = [
            {"action": "BUY", "strike": buy_k, "side": "CE", "entry_ltp": _get_ltp(buy_k, "CE")},
            {"action": "SELL", "strike": sell_k, "side": "CE", "entry_ltp": _get_ltp(sell_k, "CE")},
        ]
        strat_name = "Bear Call Spread (मंदी का ट्रेड)"
        summary = f"Sell {sell_k} CE / Buy {buy_k} CE"
    else:
        buy_ce = atm_strike + hedge_pts
        buy_pe = atm_strike - hedge_pts
        legs = [
            {"action": "BUY", "strike": buy_ce, "side": "CE", "entry_ltp": _get_ltp(buy_ce, "CE")},
            {"action": "BUY", "strike": buy_pe, "side": "PE", "entry_ltp": _get_ltp(buy_pe, "PE")},
            {"action": "SELL", "strike": atm_strike, "side": "CE", "entry_ltp": _get_ltp(atm_strike, "CE")},
            {"action": "SELL", "strike": atm_strike, "side": "PE", "entry_ltp": _get_ltp(atm_strike, "PE")},
        ]
        strat_name = "Straddle with Hedge / Iron Fly (साइडवेज़ ट्रेड)"
        summary = f"Straddle {atm_strike} CE+PE Sell / Hedge {buy_ce} CE & {buy_pe} PE Buy"

    net_credit_pts = sum(
        lg["entry_ltp"] if lg["action"] == "SELL" else -lg["entry_ltp"]
        for lg in legs
    )
    return strat_name, summary, legs, round(net_credit_pts, 2)


def execute_multi_leg_entry(mode, selected_idx, strat_name, summary, legs, lots, lot_qty, contract_map, trigger_src="Manual"):
    ist = pytz.timezone('Asia/Kolkata')
    now_str = datetime.now(ist).strftime("%I:%M:%S %p")
    total_qty = lots * lot_qty
    exch = "BFO" if selected_idx == "Sensex" else "NFO"

    enriched_legs = []
    for lg in legs:
        c_info = contract_map.get(
            (lg["strike"], lg["side"]),
            {"symbol": f"{selected_idx}_{lg['strike']}_{lg['side']}", "token": "0", "exch": exch}
        )
        enriched_legs.append({
            "action": lg["action"],
            "strike": lg["strike"],
            "side": lg["side"],
            "entry_ltp": lg["entry_ltp"],
            "symbol": c_info["symbol"],
            "token": c_info["token"],
            "exch": c_info["exch"]
        })

    ordered_legs = [x for x in enriched_legs if x["action"] == "BUY"] + [x for x in enriched_legs if x["action"] == "SELL"]

    if mode == "🔴 LIVE ALGO (असली पैसा)":
        if "angel_jwt" not in st.session_state:
            return False, "Angel One लॉगिन नहीं है!"
        jwt = st.session_state["angel_jwt"]
        akey = st.session_state["angel_key"]

        executed_so_far = []
        for lg in ordered_legs:
            ok_ord, msg_ord = place_angel_order(jwt, akey, lg["exch"], lg["symbol"], lg["token"], lg["action"], total_qty)
            if not ok_ord:
                for prev in reversed(executed_so_far):
                    rev_act = "SELL" if prev["action"] == "BUY" else "BUY"
                    place_angel_order(jwt, akey, prev["exch"], prev["symbol"], prev["token"], rev_act, total_qty)
                send_telegram_alert(f"⚠️ <b>ORDER REJECTED ({selected_idx})</b>\n{lg['action']} {lg['strike']} {lg['side']} विफल: {msg_ord}")
                return False, f"{lg['action']} {lg['strike']} {lg['side']} विफल (बाकी ऑर्डर सुरक्षित काट दिए गए): {msg_ord}"
            executed_so_far.append(lg)
            time.sleep(0.35)

    sl_limit = 1700.0 * lots
    new_trade = {
        "mode": mode,
        "index": selected_idx,
        "strategy": strat_name,
        "summary": summary,
        "lots": lots,
        "qty": total_qty,
        "exch": exch,
        "legs": ordered_legs,
        "entry_time": now_str,
        "sl_rs": sl_limit
    }
    st.session_state["active_trade"] = new_trade
    save_active_trade(new_trade)

    mode_tag = "🟡 PAPER TRADE" if "PAPER" in mode else "🔴 LIVE ALGO TRADE"
    legs_lines = "\n".join([f"• <b>{l['action']}</b> {l['strike']} {l['side']} @ ₹{l['entry_ltp']}" for l in ordered_legs])
    tg_msg = (
        f"🚀 <b>{mode_tag} EXECUTED ({trigger_src})</b>\n"
        f"📊 <b>Index:</b> {selected_idx}\n"
        f"🧭 <b>Strategy:</b> {strat_name}\n"
        f"{legs_lines}\n"
        f"📦 <b>Qty:</b> {lots} Lot ({total_qty} Qty)\n"
        f"🛑 <b>Auto Stop-Loss:</b> -₹{int(sl_limit):,}\n"
        f"⏰ <b>Time:</b> {now_str}\n\n"
        f"💡 <i>पोजीशन बंद करने के लिए <b>/exit</b> और लाइव प्रॉफिट देखने के लिए <b>/status</b> भेजें।</i>"
    )
    send_telegram_alert(tg_msg)
    return True, f"सफल! {strat_name} ({summary}) एग्जीक्यूट हो गया।"


def close_active_trade(curr_ltp_by_idx, reason="Manual Exit"):
    tr = st.session_state["active_trade"]
    if not tr:
        return

    ist = pytz.timezone('Asia/Kolkata')
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    exit_time = datetime.now(ist).strftime("%I:%M:%S %p")

    if "legs" in tr and tr["legs"]:
        legs = tr["legs"]
        sell_legs_idx = [i for i, l in enumerate(legs) if l["action"] == "SELL"]
        buy_legs_idx = [i for i, l in enumerate(legs) if l["action"] == "BUY"]

        if tr["mode"] == "🔴 LIVE ALGO (असली पैसा)" and "angel_jwt" in st.session_state:
            jwt = st.session_state["angel_jwt"]
            akey = st.session_state["angel_key"]
            for i in sell_legs_idx:
                lg = legs[i]
                place_angel_order(jwt, akey, lg["exch"], lg["symbol"], lg["token"], "BUY", tr["qty"])
                time.sleep(0.35)
            for i in buy_legs_idx:
                lg = legs[i]
                place_angel_order(jwt, akey, lg["exch"], lg["symbol"], lg["token"], "SELL", tr["qty"])
                time.sleep(0.35)

        pnl_pts = 0.0
        for i, lg in enumerate(legs):
            now_px = curr_ltp_by_idx.get(i, lg["entry_ltp"])
            if lg["action"] == "SELL":
                pnl_pts += (lg["entry_ltp"] - now_px)
            else:
                pnl_pts += (now_px - lg["entry_ltp"])

        pnl_rs = round(pnl_pts * tr["qty"], 2)
        spread_label = tr.get("summary", tr.get("strategy", "Spread"))
    else:
        curr_s = curr_ltp_by_idx.get("sell", tr["sell_entry"])
        curr_b = curr_ltp_by_idx.get("buy", tr["buy_entry"])
        if tr["mode"] == "🔴 LIVE ALGO (असली पैसा)" and "angel_jwt" in st.session_state:
            jwt = st.session_state["angel_jwt"]
            akey = st.session_state["angel_key"]
            place_angel_order(jwt, akey, tr["exch"], tr["sell_symbol"], tr["sell_token"], "BUY", tr["qty"])
            time.sleep(0.4)
            place_angel_order(jwt, akey, tr["exch"], tr["buy_symbol"], tr["buy_token"], "SELL", tr["qty"])
        pnl_pts = (tr["sell_entry"] - curr_s) + (curr_b - tr["buy_entry"])
        pnl_rs = round(pnl_pts * tr["qty"], 2)
        spread_label = f"Sell {tr['sell_strike']} {tr['side']} / Buy {tr['buy_strike']} {tr['side']}"

    if tr["mode"] == "🟡 PAPER TRADING (वर्चुअल प्रैक्टिस)":
        st.session_state["paper_capital"] += pnl_rs

    mode_short = "Paper" if "PAPER" in tr["mode"] else "LIVE"
    st.session_state["trade_history"].insert(0, {
        "तारीख": today_str,
        "मोड": mode_short,
        "इंडेक्स": tr["index"],
        "स्प्रेड": spread_label,
        "लॉट": tr["lots"],
        "एंट्री समय": tr["entry_time"],
        "एग्जिट समय": exit_time,
        "कारण": reason,
        "लाभ / हानि (₹)": pnl_rs
    })
    save_trade_history(st.session_state["trade_history"])
    save_active_trade(None)

    icon = "🛑" if "SL Hit" in reason else ("✅" if pnl_rs >= 0 else "⚠️")
    tg_exit_msg = (
        f"{icon} <b>TRADE CLOSED ({mode_short})</b>\n"
        f"📊 <b>Index:</b> {tr['index']}\n"
        f"📌 <b>Strategy:</b> {spread_label}\n"
        f"📝 <b>Reason:</b> {reason}\n"
        f"💵 <b>Final P&L:</b> <b>₹{pnl_rs:+,.0f}</b>\n"
        f"⏰ <b>Exit Time:</b> {exit_time}"
    )
    send_telegram_alert(tg_exit_msg)
    st.session_state["active_trade"] = None


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
            time.sleep(0.15)
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
    return df.drop_duplicates(subset=["Timestamp"]).sort_values("Timestamp").set_index("Timestamp")

def bs_option_price(spot, strike, t_years, iv, opt_type="CE"):
    """Black-Scholes ऑप्शन प्राइस कैलकुलेटर (सही Expiry और IV के साथ)"""
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

def calc_multileg_charges(sell_turnover, buy_turnover, order_count):
    """Angel One असली ब्रोकरेज (₹20/ऑर्डर) + STT + NSE/BSE + Stamp Duty + GST"""
    total_turnover = sell_turnover + buy_turnover
    brokerage = 20.0 * order_count
    stt = sell_turnover * 0.0015
    exch = total_turnover * 0.0003553
    sebi = total_turnover * 0.000001
    stamp = buy_turnover * 0.00003
    gst = (brokerage + exch + sebi) * 0.18
    return round(brokerage + stt + exch + sebi + stamp + gst, 2)

# ==========================================
# मुख्य 3 टैब्स
# ==========================================
tab1, tab2, tab3 = st.tabs([
    "🤖 Paper & Live Algo ट्रेडिंग",
    "⛓️ लाइव ऑप्शन चेन (Angel One)",
    "📊 10:00 AM बैकटेस्ट (Tab-1 Logic)"
])

# ==========================================
# TAB 1: PAPER TRADING & LIVE ALGO TRADING ENGINE (Direction-Aware)
# ==========================================
with tab1:
    st.subheader("🤖 पेपर ट्रेडिंग और लाइव एल्गो कंट्रोल सेंटर (दिशा सिग्नल आधारित)")

    has_tg = ("TELEGRAM_BOT_TOKEN" in st.secrets and "TELEGRAM_CHAT_ID" in st.secrets)
    t_col1, t_col2 = st.columns([3, 1])
    with t_col1:
        if has_tg:
            st.success("📲 **2-Way Telegram कंट्रोल चालू है!** आप सीधे Telegram पर **`/exit`** भेजकर ट्रेड बंद कर सकते हैं और **`/status`** भेजकर लाइव MTM देख सकते हैं।")
        else:
            st.warning("📲 Telegram अलर्ट और `/exit` कंट्रोल चालू करने के लिए Streamlit Secrets में `TELEGRAM_BOT_TOKEN` और `TELEGRAM_CHAT_ID` जोड़ें।")
    with t_col2:
        if st.button("🔔 Test Telegram", use_container_width=True):
            ok_tg, tg_err = send_telegram_alert("✅ <b>Test Alert:</b> आपका 2-Way Telegram कंट्रोल तैयार है! आप /status या /exit भेजकर टेस्ट कर सकते हैं।")
            if ok_tg:
                st.toast("Telegram पर टेस्ट मैसेज भेज दिया गया है!", icon="📲")
            else:
                st.error(f"Telegram Error: {tg_err}")

    m_col1, m_col2, m3_col = st.columns([2, 1, 1])
    with m_col1:
        trade_mode = st.radio(
            "ट्रेडिंग मोड चुनें:",
            ["🟡 PAPER TRADING (वर्चुअल प्रैक्टिस)", "🔴 LIVE ALGO (असली पैसा)"],
            horizontal=True
        )
    with m_col2:
        algo_idx = st.selectbox("ट्रेडिंग इंडेक्स", ["Nifty 50", "Bank Nifty", "Sensex"], key="algo_idx")
    with m3_col:
        algo_lots = st.number_input("लॉट (Lots)", min_value=1, max_value=25, value=1, key="algo_lots")

    live_unlocked = True
    if "LIVE ALGO" in trade_mode:
        st.warning("⚠️ **सावधान:** LIVE ALGO मोड में आपके Angel One अकाउंट से असली पैसे के ऑर्डर लगेंगे (पहले Hedge Buy, फिर ATM Sell)।")
        live_unlocked = st.checkbox("✅ मैं पुष्टि करता हूँ कि असली पैसे से ऑर्डर लगाए जाएं (Confirm Live Trading)")

    c_auto1, c_auto2 = st.columns(2)
    with c_auto1:
        auto_10am = st.toggle("⏰ सुबह 10:00 AM पर ऑटो-एंट्री चालू रखें (ऊपर = Bull Put | नीचे = Bear Call | साइडवेज़ = Straddle + Hedge)", value=False)
    with c_auto2:
        st.info(f"💰 **पेपर बैलेंस:** `₹{st.session_state['paper_capital']:,.0f}` ({st.session_state['gh_sync_status']}) | 🛑 **Auto SL:** `₹{1700 * algo_lots:,.0f}` (`₹1,700/Lot`)")

    @st.fragment(run_every=10)
    def render_algo_dashboard(mode_sel, selected_idx, lots_cnt, is_unlocked, is_auto_10):
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        now_str = now_ist.strftime("%I:%M:%S %p")
        today_str = now_ist.strftime("%Y-%m-%d")

        tg_cmd = check_telegram_commands()

        if selected_idx == "Nifty 50":
            sym, step, default_spot, hedge_pts, lot_qty = "^NSEI", 50, 24000.0, 500, 75
        elif selected_idx == "Bank Nifty":
            sym, step, default_spot, hedge_pts, lot_qty = "^NSEBANK", 100, 51000.0, 1000, 30
        else:
            sym, step, default_spot, hedge_pts, lot_qty = "^BSESN", 100, 79000.0, 1500, 20

        is_live_api = False
        full_df = None
        spot_price = default_spot
        expiry_label = "Simulation"
        contract_map = {}

        if "angel_jwt" in st.session_state and "angel_key" in st.session_state:
            live_df, live_spot, exp_date, c_map, err = fetch_angel_live_oc(
                st.session_state["angel_jwt"], st.session_state["angel_key"], selected_idx, 5
            )
            if live_df is not None and not live_df.empty:
                full_df = live_df
                spot_price = live_spot
                expiry_label = exp_date
                contract_map = c_map
                is_live_api = True

        if not is_live_api:
            spot_data = yf.download(sym, period="1d", interval="1m", progress=False)
            if isinstance(spot_data.columns, pd.MultiIndex):
                spot_data.columns = spot_data.columns.get_level_values(0)
            spot_price = float(spot_data['Close'].iloc[-1]) if not spot_data.empty else default_spot
            atm_t = round(spot_price / step) * step
            np.random.seed(int(now_ist.strftime("%H%M%S")) + int(atm_t))
            strikes_t = [atm_t + (i * step) for i in range(-18, 19)]
            full_df = pd.DataFrame({
                "STRIKE": strikes_t,
                "CALL CHG OI": [int(np.random.randint(-5, 30) * 1000) for _ in strikes_t],
                "PUT CHG OI": [int(np.random.randint(-5, 30) * 1000) for _ in strikes_t],
                "CALL LTP": [max(5.0, round(max(0, spot_price - s) + 75, 1)) for s in strikes_t],
                "PUT LTP": [max(5.0, round(max(0, s - spot_price) + 75, 1)) for s in strikes_t]
            })

        atm_strike = round(spot_price / step) * step

        g_info = evaluate_greeks_regime(selected_idx, spot_price, full_df, expiry_label)
        market_dir = g_info.get("direction", "SIDEWAYS")
        if market_dir not in ["UP", "DOWN", "SIDEWAYS"]:
            market_dir = "SIDEWAYS"

        strat_name, strat_summary, plan_legs, net_credit_pts = build_strategy_plan(
            market_dir, selected_idx, atm_strike, hedge_pts, full_df
        )
        est_credit_rs = round(net_credit_pts * lot_qty * lots_cnt, 0)

        up_3_df = full_df[(full_df["STRIKE"] > atm_strike) & (full_df["STRIKE"] <= atm_strike + 3*step)]
        down_3_df = full_df[(full_df["STRIKE"] < atm_strike) & (full_df["STRIKE"] >= atm_strike - 3*step)]
        up_3_call_chg = int(up_3_df["CALL CHG OI"].sum()) if not up_3_df.empty else 0
        down_3_put_chg = int(down_3_df["PUT CHG OI"].sum()) if not down_3_df.empty else 0

        active_tr = st.session_state["active_trade"]
        if active_tr is not None:
            st.markdown("### 🔥 चालू पोजीशन (Live MTM & Telegram Remote Control)")

            if "legs" in active_tr and active_tr["legs"]:
                legs = active_tr["legs"]
                tokens_tup = tuple(l["token"] for l in legs if l.get("token") and l["token"] != "0")
                ltp_map = {}
                if is_live_api and "angel_jwt" in st.session_state and tokens_tup:
                    ltp_map = get_live_ltp_for_tokens(
                        st.session_state["angel_jwt"], st.session_state["angel_key"],
                        active_tr["exch"], tokens_tup
                    )

                curr_ltp_by_idx = {}
                live_pnl_pts = 0.0
                for i, lg in enumerate(legs):
                    if lg["token"] in ltp_map:
                        c_px = ltp_map[lg["token"]]
                    else:
                        c_px = max(0.5, round(lg["entry_ltp"] + np.random.uniform(-3, 3), 1))
                    curr_ltp_by_idx[i] = c_px
                    if lg["action"] == "SELL":
                        live_pnl_pts += (lg["entry_ltp"] - c_px)
                    else:
                        live_pnl_pts += (c_px - lg["entry_ltp"])

                live_pnl_rs = round(live_pnl_pts * active_tr["qty"], 2)
                strat_title_active = active_tr.get("strategy", "Active Spread")

                if tg_cmd == "STATUS":
                    leg_status_txt = "\n".join([
                        f"• {l['action']} {l['strike']} {l['side']}: Entry ₹{l['entry_ltp']} ➔ Now ₹{curr_ltp_by_idx[i]}"
                        for i, l in enumerate(legs)
                    ])
                    send_telegram_alert(
                        f"📊 <b>LIVE POSITION STATUS</b>\n"
                        f"🧭 <b>Strategy:</b> {strat_title_active}\n"
                        f"{leg_status_txt}\n"
                        f"💵 <b>Current MTM:</b> <b>₹{live_pnl_rs:+,.0f}</b>\n"
                        f"🛑 <b>Auto SL:</b> -₹{int(active_tr['sl_rs']):,}"
                    )

                if tg_cmd == "EXIT":
                    close_active_trade(curr_ltp_by_idx, reason="Telegram /exit Command 📲")
                    st.warning("📲 Telegram से `/exit` कमांड मिला! पोजीशन बंद कर दी गई है।")
                    st.rerun()
                elif live_pnl_rs <= -active_tr["sl_rs"]:
                    close_active_trade(curr_ltp_by_idx, reason=f"Auto SL Hit (-₹{int(active_tr['sl_rs'])})")
                    st.error("🛑 Stop-Loss हिट हुआ! पोजीशन बंद करके Telegram पर अलर्ट भेज दिया गया है।")
                    st.rerun()
                elif now_ist.hour == 15 and now_ist.minute >= 15:
                    close_active_trade(curr_ltp_by_idx, reason="3:15 PM Auto Square-Off")
                    st.info("⏰ 3:15 PM हो गए! पोजीशन बंद करके Telegram पर रिपोर्ट भेज दी गई है।")
                    st.rerun()

                m1, m2, m3 = st.columns(3)
                m1.metric("मोड & इंडेक्स", f"{active_tr['index']} ({active_tr['mode'].split(' ')[1]})", strat_title_active)
                m2.metric("कुल लेग्स (Legs)", f"{len(legs)} ऑर्डर सक्रिय", f"Qty: {active_tr['qty']}")
                m3.metric("लाइव MTM लाभ / हानि", f"₹{live_pnl_rs:+,.0f}", f"Auto SL: -₹{int(active_tr['sl_rs']):,}")

                leg_cols = st.columns(len(legs))
                for i, lg in enumerate(legs):
                    c_px = curr_ltp_by_idx[i]
                    leg_pnl = round(((lg["entry_ltp"] - c_px) if lg["action"] == "SELL" else (c_px - lg["entry_ltp"])) * active_tr["qty"], 0)
                    leg_cols[i].metric(
                        f"{lg['action']} {lg['strike']} {lg['side']}",
                        f"₹{c_px}",
                        f"Entry: ₹{lg['entry_ltp']} | P&L: ₹{leg_pnl:+,.0f}"
                    )

                b_exit1, b_exit2 = st.columns([3, 1])
                with b_exit1:
                    if st.button("🚨 सभी लेग्स अभी बंद करें (Square-Off All)", type="primary", use_container_width=True):
                        close_active_trade(curr_ltp_by_idx, reason="Manual Square-Off")
                        st.success("✅ पूरी पोजीशन बंद कर दी गई है और Telegram अलर्ट भेज दिया गया है।")
                        st.rerun()
                with b_exit2:
                    if st.button("🔄 Check Telegram /exit", use_container_width=True):
                        st.rerun()

        else:
            if tg_cmd == "STATUS":
                send_telegram_alert("ℹ️ <b>STATUS:</b> अभी कोई भी ट्रेड चालू नहीं है।")
            elif tg_cmd == "EXIT":
                send_telegram_alert("ℹ️ <b>EXIT:</b> बंद करने के लिए कोई चालू पोजीशन नहीं है।")

            legs_html = " &nbsp;|&nbsp; ".join([
                f"<b>{l['action']} {l['strike']} {l['side']}</b> (@ ₹{l['entry_ltp']})"
                for l in plan_legs
            ])

            st.markdown(f"""
            <div style="background: {g_info.get('bg_color', 'linear-gradient(90deg, #1e293b, #0f172a)')}; padding: 16px 20px; border-radius: 12px; border-left: 6px solid {g_info.get('border_color', '#38bdf8')}; color: white; margin-bottom: 12px;">
                <div style="font-size: 13px; color: #e2e8f0;">⏱️ {now_str} | 📍 {selected_idx} Spot: <b>{spot_price:,.2f}</b> | ऊपर 3 Call Chg OI: <b>{up_3_call_chg:+,}</b> vs नीचे 3 Put Chg OI: <b>{down_3_put_chg:+,}</b> | 🛡️ हेज दूरी: <b>{hedge_pts} pts</b></div>
                <div style="font-size: 21px; font-weight: bold; margin: 6px 0;">{g_info.get('headline', '🟡 बाज़ार विश्लेषण')}</div>
                <div style="font-size: 16px; font-weight: bold; color: #fde047; margin-bottom: 6px;">🎯 एल्गो ऑटो-रणनीति: {strat_name}</div>
                <div style="font-size: 14px; color: #f8fafc;">{legs_html} &nbsp;➔&nbsp; अनुमानित नेट क्रेडिट: <b>₹{est_credit_rs:,.0f}</b> &nbsp;|&nbsp; 🛑 SL: <b>₹{1700 * lots_cnt:,.0f}</b></div>
            </div>
            """, unsafe_allow_html=True)

            if is_auto_10 and now_ist.hour == 10 and now_ist.minute <= 2 and st.session_state["auto_traded_date"] != today_str:
                if "PAPER" in mode_sel or is_unlocked:
                    ok, msg = execute_multi_leg_entry(
                        mode_sel, selected_idx, strat_name, strat_summary, plan_legs,
                        lots_cnt, lot_qty, contract_map, trigger_src="10:00 AM Auto"
                    )
                    if ok:
                        st.session_state["auto_traded_date"] = today_str
                        st.success(f"⏰ 10:00 AM Auto-Trade: {msg}")
                        st.rerun()

            btn_label = f"🟡 1-Click PAPER TRADE लगाएं — {strat_name} ({lots_cnt} Lot)" if "PAPER" in mode_sel else f"🔴 1-Click LIVE ORDER लगाएं — {strat_name} ({lots_cnt} Lot)"
            btn_disabled = ("LIVE ALGO" in mode_sel and not is_unlocked)

            if st.button(btn_label, disabled=btn_disabled, use_container_width=True):
                ok, msg = execute_multi_leg_entry(
                    mode_sel, selected_idx, strat_name, strat_summary, plan_legs,
                    lots_cnt, lot_qty, contract_map, trigger_src="1-Click"
                )
                if ok:
                    st.success(f"✅ {msg}")
                    st.rerun()
                else:
                    st.error(f"❌ {msg}")

        if st.session_state["trade_history"]:
            h_col1, h_col2 = st.columns([4, 1])
            with h_col1:
                st.markdown("### 📜 ट्रेड बुक (Saved Trade History)")
            with h_col2:
                if st.button("🗑️ हिस्ट्री साफ करें", key="clear_hist_btn"):
                    st.session_state["trade_history"] = []
                    st.session_state["paper_capital"] = 200000.0
                    save_trade_history([])
                    st.rerun()
            hist_df = pd.DataFrame(st.session_state["trade_history"])
            st.dataframe(hist_df, use_container_width=True, hide_index=True, key="saved_history_table")

    render_algo_dashboard(trade_mode, algo_idx, algo_lots, live_unlocked, auto_10am)

# ==========================================
# TAB 2: ऑप्शन सेलर लाइव ऑप्शन चेन + OTM 3-Strike IV इंडिकेटर
# ==========================================
with tab2:
    is_connected = "angel_jwt" in st.session_state
    with st.expander("🔑 Angel One SmartAPI कनेक्शन (Auto-Login सेटअप)", expanded=not is_connected):
        if is_connected:
            st.success("✅ Angel One ऑटो-कनेक्टेड है!")
        else:
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
                        jwt, err = angel_login(angel_api_key, angel_client_id, angel_mpin, angel_totp)
                        if jwt:
                            st.session_state["angel_jwt"] = jwt
                            st.session_state["angel_key"] = angel_api_key
                            st.rerun()
                        else:
                            st.error(f"❌ {err}")

    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        idx = st.selectbox("इंडेक्स चुनें", ["Nifty 50", "Bank Nifty", "Sensex"], key="live_idx")
    with c2:
        strike_range = st.slider("ATM के ऊपर और नीचे कितनी स्ट्राइक जोड़ें?", min_value=1, max_value=10, value=5)
    with c3:
        auto_refresh = st.toggle("⚡ स्मूथ लाइव रिफ्रेश (10s)", value=True, key="oc_refresh")

    refresh_interval = 10 if auto_refresh else None

    @st.fragment(run_every=refresh_interval)
    def render_seller_chain(selected_idx, s_range):
        ist = pytz.timezone('Asia/Kolkata')
        now_str = datetime.now(ist).strftime("%I:%M:%S %p")

        if selected_idx == "Nifty 50":
            sym, step, default_spot, hedge_pts = "^NSEI", 50, 24000.0, 500
        elif selected_idx == "Bank Nifty":
            sym, step, default_spot, hedge_pts = "^NSEBANK", 100, 51000.0, 1000
        else:
            sym, step, default_spot, hedge_pts = "^BSESN", 100, 79000.0, 1500

        is_live = False
        expiry_label = "Simulation"
        full_df = None
        spot_price = default_spot

        if "angel_jwt" in st.session_state and "angel_key" in st.session_state:
            live_df, live_spot, exp_date, _, err_msg = fetch_angel_live_oc(
                st.session_state["angel_jwt"], st.session_state["angel_key"], selected_idx, s_range
            )
            if live_df is not None and not live_df.empty:
                full_df = live_df
                spot_price = live_spot
                expiry_label = exp_date
                is_live = True

        if not is_live:
            spot_data = yf.download(sym, period="1d", interval="1m", progress=False)
            if isinstance(spot_data.columns, pd.MultiIndex):
                spot_data.columns = spot_data.columns.get_level_values(0)
            spot_price = float(spot_data['Close'].iloc[-1]) if not spot_data.empty else default_spot
            atm_temp = round(spot_price / step) * step
            np.random.seed(int(datetime.now(ist).strftime("%H%M%S")) + int(atm_temp))
            all_strikes = [atm_temp + (i * step) for i in range(-10, 11)]
            full_df = pd.DataFrame({
                "CALL VOL": [int(np.random.randint(90, 480) * 1000) for _ in all_strikes],
                "CALL CHG OI": [int(np.random.randint(-9, 28) * 1000) for _ in all_strikes],
                "CALL OI": [int(np.random.randint(25, 98) * 1000) for _ in all_strikes],
                "CALL LTP": [max(5.0, round((spot_price - s) + 75, 1)) for s in all_strikes],
                "STRIKE": all_strikes,
                "PUT LTP": [max(5.0, round((s - spot_price) + 75, 1)) for s in all_strikes],
                "PUT OI": [int(np.random.randint(25, 98) * 1000) for _ in all_strikes],
                "PUT CHG OI": [int(np.random.randint(-9, 28) * 1000) for _ in all_strikes],
                "PUT VOL": [int(np.random.randint(90, 480) * 1000) for _ in all_strikes]
            })

        atm_strike = round(spot_price / step) * step
        min_s = atm_strike - (s_range * step)
        max_s = atm_strike + (s_range * step)
        filtered_df = full_df[(full_df["STRIKE"] >= min_s) & (full_df["STRIKE"] <= max_s)].copy()

        safe_call_sell_strike = filtered_df.loc[filtered_df["CALL OI"].idxmax(), "STRIKE"]
        safe_put_sell_strike = filtered_df.loc[filtered_df["PUT OI"].idxmax(), "STRIKE"]

        up_3_df = full_df[(full_df["STRIKE"] > atm_strike) & (full_df["STRIKE"] <= atm_strike + 3 * step)]
        down_3_df = full_df[(full_df["STRIKE"] < atm_strike) & (full_df["STRIKE"] >= atm_strike - 3 * step)]

        up_3_call_chg = int(up_3_df["CALL CHG OI"].sum()) if not up_3_df.empty else 0
        down_3_put_chg = int(down_3_df["PUT CHG OI"].sum()) if not down_3_df.empty else 0

        data_badge = f"🟢 **ANGEL ONE LIVE DATA (Expiry: {expiry_label})**" if is_live else "🟡 **DEMO MODE**"
        st.caption(f"{data_badge} | ⏱️ **अपडेट (IST):** `{now_str}` | 📍 **Spot:** `{spot_price:,.2f}` | 🎯 **ATM:** `{atm_strike}`")
        st.markdown(f"📊 **ऊपर की 3 स्ट्राइक कुल Call Change in OI:** `{up_3_call_chg:+,}` &nbsp;&nbsp;|&nbsp;&nbsp; **नीचे की 3 स्ट्राइक कुल Put Change in OI:** `{down_3_put_chg:+,}`")

        iv_info = evaluate_otm_iv_from_df(selected_idx, spot_price, full_df, expiry_label)

        iv_c1, iv_c2, iv_c3 = st.columns(3)
        iv_c1.metric("Top-3 OTM Call IV", f"{iv_info['avg_otm_ce_iv']}%", f"{iv_info['ce_iv_change']:+}%", delta_color="inverse")
        iv_c2.metric("Bottom-3 OTM Put IV", f"{iv_info['avg_otm_pe_iv']}%", f"{iv_info['pe_iv_change']:+}%", delta_color="inverse")
        iv_c3.metric("IV ट्रेंड सिग्नल", iv_info["signal"])

        if iv_info["signal"] == "BUY_SIGNAL":
            st.success(f"✅ **{iv_info['spread_name']}** | Sell: `{iv_info['sell_leg']}` | Buy Hedge: `{iv_info['buy_hedge']}` — {iv_info['message']}")
        elif iv_info["signal"] == "TRAP":
            st.error(f"🚨 **{iv_info['spread_name']}** — {iv_info['message']}")
        else:
            st.info(f"⏳ **{iv_info['spread_name']}** — {iv_info['message']}")

        def style_seller_chain(row):
            styles = [''] * len(row)
            is_atm = (row['STRIKE'] == atm_strike)
            is_safe_call = (row['STRIKE'] == safe_call_sell_strike)
            is_safe_put = (row['STRIKE'] == safe_put_sell_strike)
            for i, col in enumerate(row.index):
                if col == 'STRIKE':
                    if is_atm:
                        styles[i] = 'background-color: #ffd700; color: black; font-weight: bold; text-align: center;'
                    elif is_safe_call:
                        styles[i] = 'background-color: #ff5252; color: white; font-weight: bold; text-align: center;'
                    elif is_safe_put:
                        styles[i] = 'background-color: #00c853; color: white; font-weight: bold; text-align: center;'
                    else:
                        styles[i] = 'background-color: #2b303a; color: #ffffff; font-weight: bold; text-align: center;'
                elif is_atm:
                    styles[i] = 'background-color: rgba(255, 215, 0, 0.22); font-weight: bold;'
                elif 'CHG OI' in col:
                    val = row[col]
                    styles[i] = 'color: #00e676; font-weight: bold;' if val > 0 else ('color: #ff5252; font-weight: bold;' if val < 0 else '')
            return styles

        styled_df = filtered_df.style.apply(style_seller_chain, axis=1).format({
            "CALL VOL": "{:,}", "CALL CHG OI": "{:+,}", "CALL OI": "{:,}", "CALL LTP": "₹{:.1f}",
            "PUT LTP": "₹{:.1f}", "PUT OI": "{:,}", "PUT CHG OI": "{:+,}", "PUT VOL": "{:,}"
        })
        st.dataframe(styled_df, use_container_width=True, hide_index=True, height=430, key="main_oc_table")

    render_seller_chain(idx, strike_range)

# ==========================================
# TAB 3: ठीक TAB 1 (Live/Paper Algo) वाले लॉजिक पर आधारित बैकटेस्ट
# ==========================================
with tab3:
    st.subheader("🎯 10:00 AM बैकटेस्ट — ठीक Tab 1 (Live/Paper Algo) वाले लॉजिक के साथ")
    st.caption("इसमें वही 3 रणनीतियाँ चलती हैं जो Tab 1 में हैं: **Bull Put Spread** (तेज़ी), **Bear Call Spread** (मंदी) और **4-Leg Straddle + Hedge** (साइडवेज) — ₹1,700/लॉट SL और 3:15 PM एग्जिट सहित।")

    rec_days_count = 0
    if os.path.exists(OC_RECORD_FILE):
        try:
            rec_meta = pd.read_csv(OC_RECORD_FILE, usecols=["Date"])
            rec_days_count = rec_meta["Date"].nunique()
        except Exception:
            rec_days_count = 0

    sync_c1, sync_c2 = st.columns([3, 1])
    with sync_c1:
        st.caption(f"💾 **ऑटो-रिकॉर्डेड असली Option Chain डेटा:** `{rec_days_count} दिन` सेव हो चुके हैं (`{OC_RECORD_FILE}` — लाइव मार्केट में हर 1 मिनट रिकॉर्ड होकर हर 15 मिनट में अपने आप GitHub पर सेव होता है)।")
    with sync_c2:
        if st.button("☁️ अभी GitHub पर डेटा सेव करें", use_container_width=True):
            with st.spinner("GitHub पर हिस्टोरिकल डेटा सिंक हो रहा है..."):
                ok_s, msg_s = sync_recorded_oc_to_github(force_now=True)
                if ok_s:
                    st.success(msg_s)
                else:
                    st.warning(msg_s)

    bt_source = st.radio(
        "बैकटेस्ट डेटा स्रोत चुनें:",
        [
            "📈 Angel One 1-Min Spot + Tab-1 Regime & Black-Scholes Model (1 महीना से 5 साल)",
            f"💾 100% असली रिकॉर्डेड Option Chain डेटा ({rec_days_count} दिन उपलब्ध)"
        ],
        horizontal=True
    )

    b1, b2, b3, b4 = st.columns(4)
    with b1:
        bt_idx = st.selectbox("इंडेक्स चुनें", ["Nifty 50", "Bank Nifty", "Sensex"], key="bt_idx")
    with b2:
        bt_days = st.selectbox(
            "कितने समय का बैकटेस्ट?",
            [
                "30 दिन (1 महीना)",
                "90 दिन (3 महीने)",
                "365 दिन (1 साल)",
                "1095 दिन (3 साल)",
                "1825 दिन (5 साल)"
            ],
            index=2
        )
    with b3:
        lots = st.number_input("लॉट साइज (Lots)", min_value=1, max_value=50, value=1, key="bt_lots")
    with b4:
        deduct_costs = st.checkbox("ब्रोकरेज + टैक्स + स्लिपेज घटाएं", value=True)

    dte_options = ["0 DTE (Expiry Day)", "1 DTE", "2 DTE", "3 DTE", "4 DTE"]
    selected_dtes = st.multiselect(
        "📅 कौन-कौन से DTE (Days to Expiry) शामिल करने हैं? (सभी चुनकर तुलना करें कि किस दिन ज़्यादा लाभ होता है):",
        options=dte_options,
        default=dte_options,
        key="bt_dte_select"
    )
    allowed_dte_nums = [int(x.split(" ")[0]) for x in selected_dtes]

    sl_per_lot = 1700.0
    total_sl_rs = sl_per_lot * lots

    if st.button("🚀 Tab-1 लॉजिक पर बैकटेस्ट चलाएं (Run Backtest)", use_container_width=True):
        if not allowed_dte_nums:
            st.warning("⚠️ कृपया कम से कम एक DTE (जैसे 0 DTE या 1 DTE) जरूर चुनें!")
        else:
            if bt_idx == "Nifty 50":
                bt_sym, step, lot_qty, hedge_points, expiry_weekday, base_iv = "^NSEI", 50, 75, 500, 3, 0.135
            elif bt_idx == "Bank Nifty":
                bt_sym, step, lot_qty, hedge_points, expiry_weekday, base_iv = "^NSEBANK", 100, 30, 1000, 2, 0.165
            else:
                bt_sym, step, lot_qty, hedge_points, expiry_weekday, base_iv = "^BSESN", 100, 20, 1500, 4, 0.145

            total_qty = lot_qty * lots
            trade_logs = []
            slip_mult = 1.0 if deduct_costs else 0.0

            if "रिकॉर्डेड" in bt_source:
                if not os.path.exists(OC_RECORD_FILE):
                    sync_recorded_oc_to_github()
                if not os.path.exists(OC_RECORD_FILE):
                    st.warning("अभी तक कोई रिकॉर्डेड डेटा फाइल नहीं बनी है। लाइव मार्केट के दौरान ऐप खोलें, डेटा अपने आप सेव होने लगेगा।")
                else:
                    rec_df = pd.read_csv(OC_RECORD_FILE)
                    rec_df = rec_df[rec_df["Index"] == bt_idx].copy()
                    if rec_df.empty:
                        st.warning(f"{bt_idx} के लिए अभी तक कोई रिकॉर्डेड डेटा उपलब्ध नहीं है।")
                    else:
                        for dt, day_oc in rec_df.groupby("Date"):
                            dt_parsed = pd.to_datetime(dt).date()
                            dte_val = (expiry_weekday - dt_parsed.weekday()) % 7
                            if dte_val not in allowed_dte_nums:
                                continue

                            day_oc = day_oc.sort_values("Timestamp")
                            snap_10 = day_oc[(day_oc["Time"] >= "10:00") & (day_oc["Time"] <= "10:05")]
                            if snap_10.empty:
                                continue
                            first_time = snap_10["Time"].iloc[0]
                            oc_10 = snap_10[snap_10["Time"] == first_time].copy()
                            if "CE LTP" in oc_10.columns and "CALL LTP" not in oc_10.columns:
                                oc_10["CALL LTP"] = oc_10["CE LTP"]

                            spot_1000 = float(oc_10["Spot"].iloc[0])
                            atm_1000 = round(spot_1000 / step) * step
                            exp_lbl = str(oc_10["Expiry"].iloc[0]) if "Expiry" in oc_10.columns else "Expiry"

                            g_info_bt = evaluate_greeks_regime(bt_idx, spot_1000, oc_10, exp_lbl)
                            m_dir = g_info_bt.get("direction", "SIDEWAYS")
                            if m_dir not in ["UP", "DOWN", "SIDEWAYS"]:
                                m_dir = "SIDEWAYS"

                            strat_name, summary_txt, plan_legs, _ = build_strategy_plan(
                                m_dir, bt_idx, atm_1000, hedge_points, oc_10
                            )

                            for lg in plan_legs:
                                col_nm = "CALL LTP" if lg["side"] == "CE" else "PUT LTP"
                                r_match = oc_10[oc_10["STRIKE"] == lg["strike"]]
                                raw_p = float(r_match[col_nm].iloc[0]) if not r_match.empty else lg["entry_ltp"]
                                if lg["action"] == "SELL":
                                    lg["bt_entry"] = round(raw_p - (max(0.4, raw_p * 0.004) * slip_mult), 2)
                                else:
                                    lg["bt_entry"] = round(raw_p + (max(0.4, raw_p * 0.012) * slip_mult), 2)
                                lg["bt_exit"] = lg["bt_entry"]

                            after_10 = day_oc[(day_oc["Time"] >= first_time) & (day_oc["Time"] <= "15:15")]
                            exit_status = "3:15 Exit ✅"

                            for tm, tm_df in after_10.groupby("Time"):
                                tm_c = tm_df.copy()
                                if "CE LTP" in tm_c.columns and "CALL LTP" not in tm_c.columns:
                                    tm_c["CALL LTP"] = tm_c["CE LTP"]

                                mtm_pts = 0.0
                                for lg in plan_legs:
                                    col_nm = "CALL LTP" if lg["side"] == "CE" else "PUT LTP"
                                    rm = tm_c[tm_c["STRIKE"] == lg["strike"]]
                                    c_p = float(rm[col_nm].iloc[0]) if not rm.empty else lg["bt_entry"]
                                    lg["bt_exit"] = c_p
                                    if lg["action"] == "SELL":
                                        mtm_pts += (lg["bt_entry"] - c_p)
                                    else:
                                        mtm_pts += (c_p - lg["bt_entry"])

                                if (mtm_pts * total_qty) <= -total_sl_rs:
                                    exit_status = f"SL Hit ❌ ({tm})"
                                    for lg in plan_legs:
                                        if lg["action"] == "SELL":
                                            lg["bt_exit"] = lg["bt_exit"] + (max(0.4, lg["bt_exit"] * 0.008) * slip_mult)
                                        else:
                                            lg["bt_exit"] = max(0.05, lg["bt_exit"] - (max(0.4, lg["bt_exit"] * 0.012) * slip_mult))
                                    break

                            gross_rs = sum(
                                ((lg["bt_entry"] - lg["bt_exit"]) if lg["action"] == "SELL" else (lg["bt_exit"] - lg["bt_entry"])) * total_qty
                                for lg in plan_legs
                            )
                            sell_to = sum((lg["bt_entry"] if lg["action"] == "SELL" else lg["bt_exit"]) * total_qty for lg in plan_legs)
                            buy_to = sum((lg["bt_exit"] if lg["action"] == "SELL" else lg["bt_entry"]) * total_qty for lg in plan_legs)
                            costs_rs = calc_multileg_charges(sell_to, buy_to, len(plan_legs) * 2) if deduct_costs else 0.0
                            net_rs = round(gross_rs - costs_rs, 0)

                            trade_logs.append({
                                "तारीख": str(dt),
                                "DTE": f"{dte_val} DTE",
                                "10:00 Spot": round(spot_1000, 1),
                                "रणनीति (Tab-1)": strat_name.split(" (")[0],
                                "लेग्स": summary_txt,
                                "नतीजा": exit_status,
                                "लाभ / हानि (₹)": net_rs
                            })

            else:
                days_num = int(bt_days.split(" ")[0])
                with st.spinner(f"Angel One SmartAPI से {days_num} दिनों के 1-मिनट डेटा पर Tab-1 लॉजिक चलाया जा रहा है..."):
                    intraday = pd.DataFrame()
                    if "angel_jwt" in st.session_state and "angel_key" in st.session_state:
                        intraday = fetch_angel_historical_spot(
                            st.session_state["angel_jwt"], st.session_state["angel_key"], bt_idx, days_num
                        )

                    if intraday.empty:
                        period_str = "30d" if days_num <= 30 else "60d"
                        intraday = yf.download(bt_sym, period=period_str, interval="5m", progress=False)
                        if isinstance(intraday.columns, pd.MultiIndex):
                            intraday.columns = intraday.columns.get_level_values(0)
                        if not intraday.empty:
                            if intraday.index.tz is None:
                                intraday.index = intraday.index.tz_localize('UTC').tz_convert('Asia/Kolkata')
                            else:
                                intraday.index = intraday.index.tz_convert('Asia/Kolkata')

                    if intraday.empty:
                        st.error("डेटा लोड नहीं हो पाया। कृपया Tab 2 में Angel One लॉगिन जांचें।")
                    else:
                        intraday['Date'] = intraday.index.date

                        for dt, day_df in intraday.groupby('Date'):
                            days_to_exp = (expiry_weekday - dt.weekday()) % 7
                            if days_to_exp not in allowed_dte_nums:
                                continue

                            morning_session = day_df.between_time("09:15", "09:59")
                            trade_session = day_df.between_time("10:00", "15:15")
                            if len(morning_session) < 5 or len(trade_session) < 15:
                                continue

                            open_915 = float(morning_session['Open'].iloc[0])
                            high_959 = float(morning_session['High'].max())
                            low_959 = float(morning_session['Low'].min())
                            spot_1000 = float(trade_session['Open'].iloc[0])
                            atm_1000 = round(spot_1000 / step) * step

                            morning_range_pct = max(0.0025, (high_959 - low_959) / spot_1000)
                            daily_iv = min(0.32, max(base_iv * 0.85, morning_range_pct * 16.0))
                            t_entry = (days_to_exp + (315.0 / 375.0)) / 365.0

                            move_pct = ((spot_1000 - open_915) / open_915) * 100.0
                            strikes_sim = [atm_1000 + (i * step) for i in range(-16, 17)]
                            sim_rows = []
                            for k in strikes_sim:
                                ce_p = bs_option_price(spot_1000, k, t_entry, daily_iv, "CE")
                                pe_p = bs_option_price(spot_1000, k, t_entry, daily_iv, "PE")
                                if move_pct >= 0.22:
                                    c_chg = 15000 if k > atm_1000 else 5000
                                    p_chg = 45000 if k < atm_1000 else 10000
                                elif move_pct <= -0.22:
                                    c_chg = 45000 if k > atm_1000 else 10000
                                    p_chg = 15000 if k < atm_1000 else 5000
                                else:
                                    c_chg = 30000 if k > atm_1000 else 10000
                                    p_chg = 30000 if k < atm_1000 else 10000
                                sim_rows.append({
                                    "STRIKE": k,
                                    "CALL LTP": round(ce_p, 2),
                                    "PUT LTP": round(pe_p, 2),
                                    "CALL OI": 100000 + abs(c_chg),
                                    "PUT OI": 100000 + abs(p_chg),
                                    "CALL CHG OI": c_chg,
                                    "PUT CHG OI": p_chg,
                                    "CALL VOL": 250000,
                                    "PUT VOL": 250000
                                })
                            sim_oc_df = pd.DataFrame(sim_rows)

                            g_info_bt = evaluate_greeks_regime(bt_idx, spot_1000, sim_oc_df, "Backtest")
                            m_dir = g_info_bt.get("direction", "SIDEWAYS")
                            if m_dir not in ["UP", "DOWN", "SIDEWAYS"]:
                                m_dir = "SIDEWAYS"

                            strat_name, summary_txt, plan_legs, _ = build_strategy_plan(
                                m_dir, bt_idx, atm_1000, hedge_points, sim_oc_df
                            )

                            for lg in plan_legs:
                                raw_p = lg["entry_ltp"]
                                if lg["action"] == "SELL":
                                    lg["bt_entry"] = round(raw_p - (max(0.4, raw_p * 0.004) * slip_mult), 2)
                                else:
                                    lg["bt_entry"] = round(raw_p + (max(0.4, raw_p * 0.012) * slip_mult), 2)
                                lg["bt_exit"] = lg["bt_entry"]

                            exit_status = "3:15 Exit ✅"
                            total_bars = len(trade_session)

                            for idx_bar, (ts, row) in enumerate(trade_session.iterrows()):
                                mins_left_today = max(15.0, 315.0 * (1.0 - (idx_bar / max(1, total_bars - 1))))
                                t_curr = (days_to_exp + (mins_left_today / 375.0)) / 365.0

                                worst_mtm_rs = 0.0
                                worst_leg_prices = {}
                                for cand_spot in [float(row["Low"]), float(row["High"])]:
                                    cand_pts = 0.0
                                    temp_px = {}
                                    for i_lg, lg in enumerate(plan_legs):
                                        cp = bs_option_price(cand_spot, lg["strike"], t_curr, daily_iv, lg["side"])
                                        temp_px[i_lg] = cp
                                        if lg["action"] == "SELL":
                                            cand_pts += (lg["bt_entry"] - cp)
                                        else:
                                            cand_pts += (cp - lg["bt_entry"])
                                    cand_rs = cand_pts * total_qty
                                    if cand_rs < worst_mtm_rs:
                                        worst_mtm_rs = cand_rs
                                        worst_leg_prices = temp_px

                                if worst_mtm_rs <= -total_sl_rs:
                                    exit_status = f"SL Hit ❌ ({ts.strftime('%I:%M %p')})"
                                    for i_lg, lg in enumerate(plan_legs):
                                        wp = worst_leg_prices.get(i_lg, lg["bt_entry"])
                                        if lg["action"] == "SELL":
                                            lg["bt_exit"] = wp + (max(0.4, wp * 0.008) * slip_mult)
                                        else:
                                            lg["bt_exit"] = max(0.05, wp - (max(0.4, wp * 0.012) * slip_mult))
                                    break

                            if exit_status == "3:15 Exit ✅":
                                close_spot = float(trade_session['Close'].iloc[-1])
                                t_close = (days_to_exp + (15.0 / 375.0)) / 365.0
                                for lg in plan_legs:
                                    cp = bs_option_price(close_spot, lg["strike"], t_close, daily_iv, lg["side"])
                                    if lg["action"] == "SELL":
                                        lg["bt_exit"] = cp + (max(0.4, cp * 0.004) * slip_mult)
                                    else:
                                        lg["bt_exit"] = max(0.05, cp - (max(0.4, cp * 0.012) * slip_mult))

                            gross_pnl_rs = sum(
                                ((lg["bt_entry"] - lg["bt_exit"]) if lg["action"] == "SELL" else (lg["bt_exit"] - lg["bt_entry"])) * total_qty
                                for lg in plan_legs
                            )
                            sell_to = sum((lg["bt_entry"] if lg["action"] == "SELL" else lg["bt_exit"]) * total_qty for lg in plan_legs)
                            buy_to = sum((lg["bt_exit"] if lg["action"] == "SELL" else lg["bt_entry"]) * total_qty for lg in plan_legs)
                            costs_rs = calc_multileg_charges(sell_to, buy_to, len(plan_legs) * 2) if deduct_costs else 0.0
                            day_pnl_rs = round(gross_pnl_rs - costs_rs, 0)

                            trade_logs.append({
                                "तारीख": str(dt),
                                "DTE": f"{days_to_exp} DTE",
                                "10:00 Spot": round(spot_1000, 1),
                                "रणनीति (Tab-1)": strat_name.split(" (")[0],
                                "लेग्स": summary_txt,
                                "नतीजा": exit_status,
                                "लाभ / हानि (₹)": day_pnl_rs
                            })

            res_df = pd.DataFrame(trade_logs)
            if not res_df.empty:
                res_df["कुल पूंजी बदलाव (₹)"] = res_df["लाभ / हानि (₹)"].cumsum()
                total_pnl = res_df["लाभ / हानि (₹)"].sum()
                win_days = len(res_df[res_df["लाभ / हानि (₹)"] > 0])
                win_rate = round((win_days / len(res_df)) * 100, 1)
                max_dd = round((res_df["कुल पूंजी बदलाव (₹)"].cummax() - res_df["कुल पूंजी बदलाव (₹)"]).max(), 0)

                r1, r2, r3, r4 = st.columns(4)
                r1.metric("कुल शुद्ध लाभ / हानि", f"₹{total_pnl:,.0f}", f"{len(res_df)} ट्रेडिंग दिन")
                r2.metric("Win Rate", f"{win_rate}%", f"{win_days} जीत / {len(res_df)-win_days} हार")
                r3.metric("Max Drawdown", f"-₹{int(max_dd):,}", "अधिकतम गिरावट")
                r4.metric("Stop-Loss", f"₹{int(total_sl_rs):,} / दिन", f"{hedge_points} pts Hedge")

                col_rep1, col_rep2 = st.columns(2)
                with col_rep1:
                    st.markdown("### 📅 DTE के अनुसार परफॉर्मेंस")
                    dte_rows = []
                    for d_lbl, grp in res_df.groupby("DTE"):
                        tc = len(grp)
                        wc = len(grp[grp["लाभ / हानि (₹)"] > 0])
                        dte_rows.append({
                            "DTE": d_lbl,
                            "ट्रेड": tc,
                            "जीत": wc,
                            "Win %": f"{round((wc/tc)*100, 1)}%",
                            "शुद्ध लाभ (₹)": round(grp["लाभ / हानि (₹)"].sum(), 0)
                        })
                    st.dataframe(pd.DataFrame(dte_rows), use_container_width=True, hide_index=True)

                with col_rep2:
                    st.markdown("### 🧭 रणनीति के अनुसार परफॉर्मेंस (Tab-1)")
                    st_rows = []
                    for s_lbl, grp in res_df.groupby("रणनीति (Tab-1)"):
                        tc = len(grp)
                        wc = len(grp[grp["लाभ / हानि (₹)"] > 0])
                        st_rows.append({
                            "रणनीति": s_lbl,
                            "ट्रेड": tc,
                            "जीत": wc,
                            "Win %": f"{round((wc/tc)*100, 1)}%",
                            "शुद्ध लाभ (₹)": round(grp["लाभ / हानि (₹)"].sum(), 0)
                        })
                    st.dataframe(pd.DataFrame(st_rows), use_container_width=True, hide_index=True)

                st.line_chart(res_df.set_index("तारीख")["कुल पूंजी बदलाव (₹)"])
                st.dataframe(res_df, use_container_width=True, hide_index=True)
            else:
                st.info("चुने गए DTE फ़िल्टर के लिए इस अवधि में कोई ट्रेड नहीं मिला।")
