import streamlit as st

mode = st.radio("Select Mode:", ["🚀 Live/Paper Algo Terminal", "📊 Manual Options Simulator"], horizontal=True)
if mode == "📊 Manual Options Simulator":
    exec(open("pages/simulator.py", encoding="utf-8").read())
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
from datetime import datetime, timedelta
import pytz
from pages.otm_iv_indicator import evaluate_otm_iv_from_df

st.set_page_config(page_title="Option Seller Algo Terminal", layout="wide")
st.title("🛡️ ऑप्शन सेलर एल्गो टर्मिनल (2-Way Telegram Control)")

HISTORY_FILE = "paper_trades_history.csv"
OC_RECORD_FILE = "recorded_oc_history.csv"

def load_trade_history():
    if os.path.exists(HISTORY_FILE):
        try:
            df = pd.read_csv(HISTORY_FILE)
            return df.to_dict("records")
        except Exception:
            return []
    return []

def save_trade_history(history_list):
    try:
        pd.DataFrame(history_list).to_csv(HISTORY_FILE, index=False)
    except Exception:
        pass

def record_live_oc_snapshot(selected_idx, spot_price, exp_str, df_oc):
    """लाइव मार्केट (9:15 AM - 3:30 PM) में हर 1 मिनट का असली Option Chain डेटा सेव करना"""
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
    except Exception:
        pass

# ==========================================
# Session State इनिशियलाइज़ेशन
# ==========================================
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
    st.session_state["active_trade"] = None
if "auto_traded_date" not in st.session_state:
    st.session_state["auto_traded_date"] = ""
if "last_tg_update_id" not in st.session_state:
    st.session_state["last_tg_update_id"] = 0
if "base_oi_map" not in st.session_state:
    st.session_state["base_oi_map"] = {}
if "base_oi_date" not in st.session_state:
    st.session_state["base_oi_date"] = ""
if "last_recorded_minute" not in st.session_state:
    st.session_state["last_recorded_minute"] = ""

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
    payload = {"mode": "LTP", "exchangeTokens": {exchange: [str(t) for t in tokens_tuple]}}
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=5).json()
        if res.get("status"):
            return {str(item["symbolToken"]): float(item["ltp"]) for item in res["data"]["fetched"]}
    except Exception:
        pass
    return {}

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

    max_hedge = 1000 if selected_idx == "Sensex" else 300
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
    if st.session_state["base_oi_date"] != today_str:
        st.session_state["base_oi_date"] = today_str
        st.session_state["base_oi_map"] = {}

    df = raw_df.copy()
    call_chgs = []
    put_chgs = []

    for _, row in df.iterrows():
        ce_tok = row["CE_TOKEN"]
        pe_tok = row["PE_TOKEN"]
        ce_oi = int(row["CALL OI"])
        pe_oi = int(row["PUT OI"])

        if ce_tok and ce_tok not in st.session_state["base_oi_map"] and ce_oi > 0:
            st.session_state["base_oi_map"][ce_tok] = ce_oi
        if pe_tok and pe_tok not in st.session_state["base_oi_map"] and pe_oi > 0:
            st.session_state["base_oi_map"][pe_tok] = pe_oi

        ce_chg = int(row["API_CE_CHG"])
        if ce_chg == 0 and ce_tok in st.session_state["base_oi_map"]:
            ce_chg = ce_oi - st.session_state["base_oi_map"][ce_tok]

        pe_chg = int(row["API_PE_CHG"])
        if pe_chg == 0 and pe_tok in st.session_state["base_oi_map"]:
            pe_chg = pe_oi - st.session_state["base_oi_map"][pe_tok]

        call_chgs.append(ce_chg)
        put_chgs.append(pe_chg)

    df["CALL CHG OI"] = call_chgs
    df["PUT CHG OI"] = put_chgs
    final_cols = ["CALL VOL", "CALL CHG OI", "CALL OI", "CALL LTP", "STRIKE", "PUT LTP", "PUT OI", "PUT CHG OI", "PUT VOL"]
    final_df = df[final_cols]

    record_live_oc_snapshot(selected_idx, spot_price, exp_str, final_df)

    return final_df, spot_price, exp_str, contract_map, None

def execute_spread_entry(mode, selected_idx, side, sell_strike, buy_strike, sell_ltp, buy_ltp, lots, lot_qty, contract_map, trigger_src="Manual"):
    ist = pytz.timezone('Asia/Kolkata')
    now_str = datetime.now(ist).strftime("%I:%M:%S %p")
    total_qty = lots * lot_qty
    exch = "BFO" if selected_idx == "Sensex" else "NFO"

    sell_c = contract_map.get((sell_strike, side), {"symbol": f"{selected_idx}_{sell_strike}_{side}", "token": "0", "exch": exch})
    buy_c = contract_map.get((buy_strike, side), {"symbol": f"{selected_idx}_{buy_strike}_{side}", "token": "0", "exch": exch})

    if mode == "🔴 LIVE ALGO (असली पैसा)":
        if "angel_jwt" not in st.session_state:
            return False, "Angel One लॉगिन नहीं है!"
        jwt = st.session_state["angel_jwt"]
        akey = st.session_state["angel_key"]

        ok_buy, msg_buy = place_angel_order(jwt, akey, buy_c["exch"], buy_c["symbol"], buy_c["token"], "BUY", total_qty)
        if not ok_buy:
            send_telegram_alert(f"⚠️ <b>ORDER REJECTED ({selected_idx})</b>\nHedge Buy विफल: {msg_buy}")
            return False, f"Hedge Buy ऑर्डर विफल: {msg_buy}"

        time.sleep(0.4)

        ok_sell, msg_sell = place_angel_order(jwt, akey, sell_c["exch"], sell_c["symbol"], sell_c["token"], "SELL", total_qty)
        if not ok_sell:
            place_angel_order(jwt, akey, buy_c["exch"], buy_c["symbol"], buy_c["token"], "SELL", total_qty)
            send_telegram_alert(f"⚠️ <b>ATM SELL REJECTED ({selected_idx})</b>\nHedge सुरक्षित काट दिया गया: {msg_sell}")
            return False, f"ATM Sell रिजेक्ट हुआ (Hedge भी सुरक्षित काट दिया गया): {msg_sell}"

    sl_limit = 2000.0 * lots
    st.session_state["active_trade"] = {
        "mode": mode,
        "index": selected_idx,
        "side": side,
        "lots": lots,
        "qty": total_qty,
        "exch": exch,
        "sell_strike": sell_strike,
        "buy_strike": buy_strike,
        "sell_symbol": sell_c["symbol"],
        "buy_symbol": buy_c["symbol"],
        "sell_token": sell_c["token"],
        "buy_token": buy_c["token"],
        "sell_entry": sell_ltp,
        "buy_entry": buy_ltp,
        "entry_time": now_str,
        "sl_rs": sl_limit
    }

    mode_tag = "🟡 PAPER TRADE" if "PAPER" in mode else "🔴 LIVE ALGO TRADE"
    spread_type = "Bull Put Spread" if side == "PE" else "Bear Call Spread"
    tg_msg = (
        f"🚀 <b>{mode_tag} EXECUTED ({trigger_src})</b>\n"
        f"📊 <b>Index:</b> {selected_idx} ({spread_type})\n"
        f"🛡️ <b>1. Hedge Buy:</b> {buy_strike} {side} @ ₹{buy_ltp}\n"
        f"💰 <b>2. ATM Sell:</b> {sell_strike} {side} @ ₹{sell_ltp}\n"
        f"📦 <b>Qty:</b> {lots} Lot ({total_qty} Qty)\n"
        f"🛑 <b>Auto Stop-Loss:</b> -₹{int(sl_limit):,}\n"
        f"⏰ <b>Time:</b> {now_str}\n\n"
        f"💡 <i>पोजीशन बंद करने के लिए <b>/exit</b> और लाइव प्रॉफिट देखने के लिए <b>/status</b> भेजें।</i>"
    )
    send_telegram_alert(tg_msg)
    return True, f"सफल! पहले {buy_strike} {side} Buy और फिर {sell_strike} {side} Sell एग्जीक्यूट हुआ।"

def close_active_trade(curr_sell_ltp, curr_buy_ltp, reason="Manual Exit"):
    tr = st.session_state["active_trade"]
    if not tr:
        return

    ist = pytz.timezone('Asia/Kolkata')
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    exit_time = datetime.now(ist).strftime("%I:%M:%S %p")

    if tr["mode"] == "🔴 LIVE ALGO (असली पैसा)" and "angel_jwt" in st.session_state:
        jwt = st.session_state["angel_jwt"]
        akey = st.session_state["angel_key"]
        place_angel_order(jwt, akey, tr["exch"], tr["sell_symbol"], tr["sell_token"], "BUY", tr["qty"])
        time.sleep(0.4)
        place_angel_order(jwt, akey, tr["exch"], tr["buy_symbol"], tr["buy_token"], "SELL", tr["qty"])

    pnl_pts = (tr["sell_entry"] - curr_sell_ltp) + (curr_buy_ltp - tr["buy_entry"])
    pnl_rs = round(pnl_pts * tr["qty"], 2)

    if tr["mode"] == "🟡 PAPER TRADING (वर्चुअल प्रैक्टिस)":
        st.session_state["paper_capital"] += pnl_rs

    mode_short = "Paper" if "PAPER" in tr["mode"] else "LIVE"
    st.session_state["trade_history"].insert(0, {
        "तारीख": today_str,
        "मोड": mode_short,
        "इंडेक्स": tr["index"],
        "स्प्रेड": f"Sell {tr['sell_strike']} {tr['side']} / Buy {tr['buy_strike']} {tr['side']}",
        "लॉट": tr["lots"],
        "एंट्री समय": tr["entry_time"],
        "एग्जिट समय": exit_time,
        "कारण": reason,
        "लाभ / हानि (₹)": pnl_rs
    })
    save_trade_history(st.session_state["trade_history"])

    icon = "🛑" if "SL Hit" in reason else ("✅" if pnl_rs >= 0 else "⚠️")
    tg_exit_msg = (
        f"{icon} <b>TRADE CLOSED ({mode_short})</b>\n"
        f"📊 <b>Index:</b> {tr['index']}\n"
        f"📌 <b>Spread:</b> Sell {tr['sell_strike']} {tr['side']} / Buy {tr['buy_strike']} {tr['side']}\n"
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

def calc_roundtrip_charges(sell_entry, sell_exit, buy_entry, buy_exit, qty):
    """Angel One असली ब्रोकरेज + STT + NSE + Stamp Duty + GST (4 ऑर्डर राउंड ट्रिप)"""
    sell_turnover = (sell_entry + buy_exit) * qty
    buy_turnover = (buy_entry + sell_exit) * qty
    total_turnover = sell_turnover + buy_turnover

    brokerage = 80.0
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
    "📊 10:00 AM बैकटेस्ट (1-Min)"
])

# ==========================================
# TAB 1: PAPER TRADING & LIVE ALGO TRADING ENGINE
# ==========================================
with tab1:
    st.subheader("🤖 पेपर ट्रेडिंग और लाइव एल्गो कंट्रोल सेंटर (⚡ Smooth Live)")

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
        auto_10am = st.toggle("⏰ सुबह 10:00 AM पर ऑटो-एंट्री चालू रखें (ऊपर 3 vs नीचे 3 Chg OI)", value=False)
    with c_auto2:
        st.info(f"💰 **पेपर बैलेंस:** `₹{st.session_state['paper_capital']:,.0f}` | 🛑 **Auto Stop-Loss:** `₹{2000 * algo_lots:,.0f}` (`₹2,000/Lot`)")

    @st.fragment(run_every=10)
    def render_algo_dashboard(mode_sel, selected_idx, lots_cnt, is_unlocked, is_auto_10):
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        now_str = now_ist.strftime("%I:%M:%S %p")
        today_str = now_ist.strftime("%Y-%m-%d")

        tg_cmd = check_telegram_commands()

        if selected_idx == "Nifty 50":
            sym, step, default_spot, hedge_pts, lot_qty = "^NSEI", 50, 24000.0, 300, 75
        elif selected_idx == "Bank Nifty":
            sym, step, default_spot, hedge_pts, lot_qty = "^NSEBANK", 100, 51000.0, 300, 30
        else:
            sym, step, default_spot, hedge_pts, lot_qty = "^BSESN", 100, 79000.0, 1000, 20

        is_live_api = False
        full_df = None
        spot_price = default_spot
        contract_map = {}

        if "angel_jwt" in st.session_state and "angel_key" in st.session_state:
            live_df, live_spot, _, c_map, err = fetch_angel_live_oc(
                st.session_state["angel_jwt"], st.session_state["angel_key"], selected_idx, 5
            )
            if live_df is not None and not live_df.empty:
                full_df = live_df
                spot_price = live_spot
                contract_map = c_map
                is_live_api = True

        if not is_live_api:
            spot_data = yf.download(sym, period="1d", interval="1m", progress=False)
            if isinstance(spot_data.columns, pd.MultiIndex):
                spot_data.columns = spot_data.columns.get_level_values(0)
            spot_price = float(spot_data['Close'].iloc[-1]) if not spot_data.empty else default_spot
            atm_t = round(spot_price / step) * step
            np.random.seed(int(now_ist.strftime("%H%M%S")) + int(atm_t))
            strikes_t = [atm_t + (i * step) for i in range(-12, 13)]
            full_df = pd.DataFrame({
                "STRIKE": strikes_t,
                "CALL CHG OI": [int(np.random.randint(-5, 30) * 1000) for _ in strikes_t],
                "PUT CHG OI": [int(np.random.randint(-5, 30) * 1000) for _ in strikes_t],
                "CALL LTP": [max(5.0, round(max(0, spot_price - s) + 75, 1)) for s in strikes_t],
                "PUT LTP": [max(5.0, round(max(0, s - spot_price) + 75, 1)) for s in strikes_t]
            })

        atm_strike = round(spot_price / step) * step

        up_3_df = full_df[(full_df["STRIKE"] > atm_strike) & (full_df["STRIKE"] <= atm_strike + 3*step)]
        down_3_df = full_df[(full_df["STRIKE"] < atm_strike) & (full_df["STRIKE"] >= atm_strike - 3*step)]

        up_3_call_chg = int(up_3_df["CALL CHG OI"].sum()) if not up_3_df.empty else 0
        down_3_put_chg = int(down_3_df["PUT CHG OI"].sum()) if not down_3_df.empty else 0

        if down_3_put_chg >= up_3_call_chg:
            rec_side = "PE"
            rec_sell_strike = atm_strike
            rec_buy_strike = atm_strike - hedge_pts
            rec_title = f"🟢 एल्गो सिग्नल: Bull Put Spread (Sell {rec_sell_strike} PE + Buy {rec_buy_strike} PE)"
        else:
            rec_side = "CE"
            rec_sell_strike = atm_strike
            rec_buy_strike = atm_strike + hedge_pts
            rec_title = f"🔴 एल्गो सिग्नल: Bear Call Spread (Sell {rec_sell_strike} CE + Buy {rec_buy_strike} CE)"

        sell_row = full_df[full_df["STRIKE"] == rec_sell_strike]
        buy_row = full_df[full_df["STRIKE"] == rec_buy_strike]
        ltp_col = "PUT LTP" if rec_side == "PE" else "CALL LTP"

        rec_sell_ltp = float(sell_row[ltp_col].values[0]) if not sell_row.empty else 120.0
        rec_buy_ltp = float(buy_row[ltp_col].values[0]) if not buy_row.empty else 45.0
        est_credit_rs = round((rec_sell_ltp - rec_buy_ltp) * lot_qty * lots_cnt, 0)

        active_tr = st.session_state["active_trade"]
        if active_tr is not None:
            st.markdown("### 🔥 चालू पोजीशन (Live MTM & Telegram Remote Control)")
            curr_sell_ltp = active_tr["sell_entry"]
            curr_buy_ltp = active_tr["buy_entry"]

            if is_live_api and "angel_jwt" in st.session_state and active_tr["sell_token"] != "0":
                ltp_map = get_live_ltp_for_tokens(
                    st.session_state["angel_jwt"], st.session_state["angel_key"],
                    active_tr["exch"], (active_tr["sell_token"], active_tr["buy_token"])
                )
                curr_sell_ltp = ltp_map.get(active_tr["sell_token"], curr_sell_ltp)
                curr_buy_ltp = ltp_map.get(active_tr["buy_token"], curr_buy_ltp)
            else:
                curr_sell_ltp = max(1.0, round(active_tr["sell_entry"] + np.random.uniform(-4, 4), 1))
                curr_buy_ltp = max(1.0, round(active_tr["buy_entry"] + np.random.uniform(-2, 2), 1))

            live_pnl_pts = (active_tr["sell_entry"] - curr_sell_ltp) + (curr_buy_ltp - active_tr["buy_entry"])
            live_pnl_rs = round(live_pnl_pts * active_tr["qty"], 2)

            if tg_cmd == "STATUS":
                send_telegram_alert(
                    f"📊 <b>LIVE POSITION STATUS</b>\n"
                    f"📌 <b>Spread:</b> Sell {active_tr['sell_strike']} {active_tr['side']} / Buy {active_tr['buy_strike']} {active_tr['side']}\n"
                    f"💵 <b>Current MTM:</b> <b>₹{live_pnl_rs:+,.0f}</b>\n"
                    f"🛑 <b>Auto SL:</b> -₹{int(active_tr['sl_rs']):,}"
                )

            if tg_cmd == "EXIT":
                close_active_trade(curr_sell_ltp, curr_buy_ltp, reason="Telegram /exit Command 📲")
                st.warning("📲 Telegram से `/exit` कमांड मिला! पोजीशन बंद कर दी गई है।")
                st.rerun()
            elif live_pnl_rs <= -active_tr["sl_rs"]:
                close_active_trade(curr_sell_ltp, curr_buy_ltp, reason=f"Auto SL Hit (-₹{int(active_tr['sl_rs'])})")
                st.error("🛑 Stop-Loss हिट हुआ! पोजीशन बंद करके Telegram पर अलर्ट भेज दिया गया है।")
                st.rerun()
            elif now_ist.hour == 15 and now_ist.minute >= 15:
                close_active_trade(curr_sell_ltp, curr_buy_ltp, reason="3:15 PM Auto Square-Off")
                st.info("⏰ 3:15 PM हो गए! पोजीशन बंद करके Telegram पर रिपोर्ट भेज दी गई है।")
                st.rerun()

            p1, p2, p3, p4 = st.columns(4)
            p1.metric("मोड & इंडेक्स", f"{active_tr['index']}", active_tr["mode"].split(" ")[1])
            p2.metric("बेची गई ATM स्ट्राइक", f"{active_tr['sell_strike']} {active_tr['side']}", f"Entry: ₹{active_tr['sell_entry']} | Now: ₹{curr_sell_ltp}")
            p3.metric("खरीदी गई Hedge स्ट्राइक", f"{active_tr['buy_strike']} {active_tr['side']}", f"Entry: ₹{active_tr['buy_entry']} | Now: ₹{curr_buy_ltp}")
            p4.metric("लाइव MTM लाभ / हानि", f"₹{live_pnl_rs:+,.0f}", f"Auto SL: -₹{int(active_tr['sl_rs']):,}")

            b_exit1, b_exit2 = st.columns([3, 1])
            with b_exit1:
                if st.button("🚨 पोजीशन अभी बंद करें (Square-Off Now)", type="primary", use_container_width=True):
                    close_active_trade(curr_sell_ltp, curr_buy_ltp, reason="Manual Square-Off")
                    st.success("✅ पोजीशन बंद कर दी गई है और Telegram अलर्ट भेज दिया गया है।")
                    st.rerun()
            with b_exit2:
                if st.button("🔄 Check Telegram /exit", use_container_width=True):
                    st.rerun()

        else:
            if tg_cmd == "STATUS":
                send_telegram_alert("ℹ️ <b>STATUS:</b> अभी कोई भी ट्रेड चालू नहीं है।")
            elif tg_cmd == "EXIT":
                send_telegram_alert("ℹ️ <b>EXIT:</b> बंद करने के लिए कोई चालू पोजीशन नहीं है।")

            st.markdown(f"""
            <div style="background: linear-gradient(90deg, #1e293b, #0f172a); padding: 16px; border-radius: 12px; border: 1px solid #334155; color: white; margin-bottom: 12px;">
                <div style="font-size: 13px; color: #94a3b8;">⏱️ {now_str} | 📍 {selected_idx} Spot: <b>{spot_price:,.2f}</b> | ऊपर 3 Call Chg OI: <b>{up_3_call_chg:,}</b> vs नीचे 3 Put Chg OI: <b>{down_3_put_chg:,}</b></div>
                <h4 style="margin: 6px 0; color: #38bdf8;">{rec_title}</h4>
                <div style="font-size: 14px;">1️⃣ पहले Hedge Buy: <b>{rec_buy_strike} {rec_side}</b> (@ ₹{rec_buy_ltp}) &nbsp;➔&nbsp; 2️⃣ फिर ATM Sell: <b>{rec_sell_strike} {rec_side}</b> (@ ₹{rec_sell_ltp}) &nbsp;|&nbsp; अनुमानित क्रेडिट: <b>₹{est_credit_rs:,.0f}</b></div>
            </div>
            """, unsafe_allow_html=True)

            if is_auto_10 and now_ist.hour == 10 and now_ist.minute <= 2 and st.session_state["auto_traded_date"] != today_str:
                if "PAPER" in mode_sel or is_unlocked:
                    ok, msg = execute_spread_entry(
                        mode_sel, selected_idx, rec_side, rec_sell_strike, rec_buy_strike,
                        rec_sell_ltp, rec_buy_ltp, lots_cnt, lot_qty, contract_map, trigger_src="10:00 AM Auto"
                    )
                    if ok:
                        st.session_state["auto_traded_date"] = today_str
                        st.success(f"⏰ 10:00 AM Auto-Trade: {msg}")
                        st.rerun()

            btn_label = f"🟡 1-Click PAPER TRADE लगाएं ({lots_cnt} Lot)" if "PAPER" in mode_sel else f"🔴 1-Click LIVE ORDER लगाएं ({lots_cnt} Lot)"
            btn_disabled = ("LIVE ALGO" in mode_sel and not is_unlocked)

            if st.button(btn_label, disabled=btn_disabled, use_container_width=True):
                ok, msg = execute_spread_entry(
                    mode_sel, selected_idx, rec_side, rec_sell_strike, rec_buy_strike,
                    rec_sell_ltp, rec_buy_ltp, lots_cnt, lot_qty, contract_map, trigger_src="1-Click"
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
                    if os.path.exists(HISTORY_FILE):
                        os.remove(HISTORY_FILE)
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
            sym, step, default_spot, hedge_pts = "^NSEI", 50, 24000.0, 300
        elif selected_idx == "Bank Nifty":
            sym, step, default_spot, hedge_pts = "^NSEBANK", 100, 51000.0, 300
        else:
            sym, step, default_spot, hedge_pts = "^BSESN", 100, 79000.0, 1000

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

        # --- नया OTM 3-Strike IV इंडिकेटर (pages/otm_iv_indicator.py से) ---
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
# TAB 3: सुबह 10:00 AM Change in OI — High-Accuracy Backtest
# ==========================================
with tab3:
    st.subheader("🎯 सुबह 10:00 AM — High-Accuracy बैकटेस्ट (1-Min Spot + Black-Scholes + Net Brokerage)")

    rec_days_count = 0
    if os.path.exists(OC_RECORD_FILE):
        try:
            rec_meta = pd.read_csv(OC_RECORD_FILE, usecols=["Date"])
            rec_days_count = rec_meta["Date"].nunique()
        except Exception:
            rec_days_count = 0

    st.caption(f"💾 **ऑटो-रिकॉर्डेड असली Option Chain डेटा:** `{rec_days_count} दिन` सेव हो चुके हैं (`{OC_RECORD_FILE}` — लाइव मार्केट में ऐप खुलते ही हर मिनट अपने आप रिकॉर्ड होता है)।")

    bt_source = st.radio(
        "बैकटेस्ट डेटा स्रोत चुनें:",
        [
            "📈 Angel One 1-Min Spot + Black-Scholes & Expiry Model (1 महीना से 5 साल)",
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

    sl_per_lot = 2000.0
    total_sl_rs = sl_per_lot * lots

    if st.button("🚀 सटीक बैकटेस्ट चलाएं (Run Accurate Backtest)", use_container_width=True):
        if bt_idx == "Nifty 50":
            bt_sym, step, lot_qty, hedge_points, expiry_weekday, base_iv = "^NSEI", 50, 75, 300, 3, 0.135
        elif bt_idx == "Bank Nifty":
            bt_sym, step, lot_qty, hedge_points, expiry_weekday, base_iv = "^NSEBANK", 100, 30, 300, 2, 0.165
        else:
            bt_sym, step, lot_qty, hedge_points, expiry_weekday, base_iv = "^BSESN", 100, 20, 1000, 4, 0.145

        total_qty = lot_qty * lots
        trade_col_name = f"ट्रेड (ATM + {hedge_points} Hedge)"
        trade_logs = []

        if "रिकॉर्डेड" in bt_source:
            if not os.path.exists(OC_RECORD_FILE):
                st.warning("अभी तक कोई रिकॉर्डेड डेटा फाइल नहीं बनी है। लाइव मार्केट के दौरान ऐप खोलें, डेटा अपने आप सेव होने लगेगा।")
            else:
                rec_df = pd.read_csv(OC_RECORD_FILE)
                rec_df = rec_df[rec_df["Index"] == bt_idx].copy()
                if rec_df.empty:
                    st.warning(f"{bt_idx} के लिए अभी तक कोई रिकॉर्डेड डेटा उपलब्ध नहीं है।")
                else:
                    for dt, day_oc in rec_df.groupby("Date"):
                        day_oc = day_oc.sort_values("Timestamp")
                        snap_10 = day_oc[(day_oc["Time"] >= "10:00") & (day_oc["Time"] <= "10:05")]
                        if snap_10.empty:
                            continue
                        first_time = snap_10["Time"].iloc[0]
                        oc_10 = snap_10[snap_10["Time"] == first_time]
                        spot_1000 = float(oc_10["Spot"].iloc[0])
                        atm_1000 = round(spot_1000 / step) * step

                        up_3 = oc_10[(oc_10["STRIKE"] > atm_1000) & (oc_10["STRIKE"] <= atm_1000 + 3 * step)]
                        dn_3 = oc_10[(oc_10["STRIKE"] < atm_1000) & (oc_10["STRIKE"] >= atm_1000 - 3 * step)]
                        up_chg = int(up_3["CALL CHG OI"].sum())
                        dn_chg = int(dn_3["PUT CHG OI"].sum())

                        if dn_chg >= up_chg:
                            side, sell_k, buy_k = "PE", atm_1000, atm_1000 - hedge_points
                            ltp_col = "PUT LTP"
                        else:
                            side, sell_k, buy_k = "CE", atm_1000, atm_1000 + hedge_points
                            ltp_col = "CE LTP" if "CE LTP" in oc_10.columns else "CALL LTP"

                        s_row = oc_10[oc_10["STRIKE"] == sell_k]
                        b_row = oc_10[oc_10["STRIKE"] == buy_k]
                        if s_row.empty or b_row.empty:
                            continue

                        sell_entry = float(s_row[ltp_col].iloc[0])
                        buy_entry = float(b_row[ltp_col].iloc[0])
                        legs_text = f"Sell {sell_k} {side} / Buy {buy_k} {side}"

                        after_10 = day_oc[(day_oc["Time"] >= first_time) & (day_oc["Time"] <= "15:15")]
                        exit_status = "3:15 Exit ✅"
                        curr_s, curr_b = sell_entry, buy_entry

                        for tm, tm_df in after_10.groupby("Time"):
                            sr = tm_df[tm_df["STRIKE"] == sell_k]
                            br = tm_df[tm_df["STRIKE"] == buy_k]
                            if not sr.empty and not br.empty:
                                curr_s = float(sr[ltp_col].iloc[0])
                                curr_b = float(br[ltp_col].iloc[0])
                                mtm_rs = ((sell_entry - curr_s) + (curr_b - buy_entry)) * total_qty
                                if mtm_rs <= -total_sl_rs:
                                    exit_status = f"SL Hit ❌ ({tm})"
                                    break

                        gross_rs = ((sell_entry - curr_s) + (curr_b - buy_entry)) * total_qty
                        costs_rs = calc_roundtrip_charges(sell_entry, curr_s, buy_entry, curr_b, total_qty) if deduct_costs else 0.0
                        net_rs = round(gross_rs - costs_rs, 0)
                        net_pts = round(net_rs / total_qty, 1)

                        trade_logs.append({
                            "तारीख": str(dt),
                            "10:00 Spot": round(spot_1000, 1),
                            trade_col_name: legs_text,
                            "नतीजा": exit_status,
                            "नेट पॉइंट्स": net_pts,
                            "लाभ / हानि (₹)": net_rs
                        })

        else:
            days_num = int(bt_days.split(" ")[0])
            with st.spinner(f"Angel One SmartAPI से {days_num} दिनों का 1-मिनट असली स्पॉट डेटा लोड हो रहा है (बड़े डेटा में 10–25 सेकंड लग सकते हैं)..."):
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

                        days_to_exp = (expiry_weekday - dt.weekday()) % 7

                        morning_move = spot_1000 - open_915
                        is_bull_put = (morning_move >= 0)

                        if is_bull_put:
                            opt_type = "PE"
                            sell_k = atm_1000
                            buy_k = atm_1000 - hedge_points
                            legs_text = f"Sell {sell_k} PE / Buy {buy_k} PE"
                        else:
                            opt_type = "CE"
                            sell_k = atm_1000
                            buy_k = atm_1000 + hedge_points
                            legs_text = f"Sell {sell_k} CE / Buy {buy_k} CE"

                        t_entry = (days_to_exp + (315.0 / 375.0)) / 365.0
                        raw_sell_entry = bs_option_price(spot_1000, sell_k, t_entry, daily_iv, opt_type)
                        raw_buy_entry = bs_option_price(spot_1000, buy_k, t_entry, daily_iv, opt_type)

                        slip_mult = 1.0 if deduct_costs else 0.0
                        sell_entry = round(raw_sell_entry - (max(0.5, raw_sell_entry * 0.004) * slip_mult), 2)
                        buy_entry = round(raw_buy_entry + (max(0.5, raw_buy_entry * 0.012) * slip_mult), 2)

                        exit_status = "3:15 Exit ✅"
                        final_sell_exit = sell_entry
                        final_buy_exit = buy_entry
                        total_bars = len(trade_session)

                        for idx_bar, (ts, row) in enumerate(trade_session.iterrows()):
                            mins_left_today = max(15.0, 315.0 * (1.0 - (idx_bar / max(1, total_bars - 1))))
                            t_curr = (days_to_exp + (mins_left_today / 375.0)) / 365.0

                            worst_spot = float(row['Low']) if is_bull_put else float(row['High'])
                            curr_sell = bs_option_price(worst_spot, sell_k, t_curr, daily_iv, opt_type)
                            curr_buy = bs_option_price(worst_spot, buy_k, t_curr, daily_iv, opt_type)

                            unrealized_rs = ((sell_entry - curr_sell) + (curr_buy - buy_entry)) * total_qty
                            if unrealized_rs <= -total_sl_rs:
                                exit_status = f"SL Hit ❌ ({ts.strftime('%I:%M %p')})"
                                final_sell_exit = curr_sell + (max(0.5, curr_sell * 0.008) * slip_mult)
                                final_buy_exit = max(0.05, curr_buy - (max(0.5, curr_buy * 0.012) * slip_mult))
                                break

                        if exit_status == "3:15 Exit ✅":
                            close_spot = float(trade_session['Close'].iloc[-1])
                            t_close = (days_to_exp + (15.0 / 375.0)) / 365.0
                            c_sell = bs_option_price(close_spot, sell_k, t_close, daily_iv, opt_type)
                            c_buy = bs_option_price(close_spot, buy_k, t_close, daily_iv, opt_type)
                            final_sell_exit = c_sell + (max(0.5, c_sell * 0.004) * slip_mult)
                            final_buy_exit = max(0.05, c_buy - (max(0.5, c_buy * 0.012) * slip_mult))

                        gross_pnl_rs = ((sell_entry - final_sell_exit) + (final_buy_exit - buy_entry)) * total_qty
                        costs_rs = calc_roundtrip_charges(sell_entry, final_sell_exit, buy_entry, final_buy_exit, total_qty) if deduct_costs else 0.0
                        day_pnl_rs = round(gross_pnl_rs - costs_rs, 0)
                        day_pts = round(day_pnl_rs / total_qty, 1)

                        trade_logs.append({
                            "तारीख": str(dt),
                            "10:00 Spot": round(spot_1000, 1),
                            trade_col_name: legs_text,
                            "नतीजा": exit_status,
                            "नेट पॉइंट्स": day_pts,
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

            st.line_chart(res_df.set_index("तारीख")["कुल पूंजी बदलाव (₹)"])
            st.dataframe(res_df, use_container_width=True, hide_index=True)
