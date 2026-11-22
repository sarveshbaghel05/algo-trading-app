import streamlit as st
import pandas as pd
import numpy as np
import requests
import pyotp
import time
import os
import math
import json
import base64
from io import StringIO
from datetime import datetime, timedelta
import pytz

try:
    from pages.greek_regime_indicator import evaluate_greeks_regime
except ImportError:
    from pages.greeks_regime_indicator import evaluate_greeks_regime

st.title("🧠 Smart 4-Layer Pro Algo Terminal")
st.caption("0.25 Delta OTM स्ट्राइक | Leg-wise 35% SL | VWAP + ORB + 3-Strike OI कन्फर्मेशन | Zero Fake Data")

PRO_HISTORY_FILE = "pro_algo_history.csv"
PRO_ACTIVE_FILE = "pro_active_trade.json"
DEFAULT_GH_REPO = "Sarveshbaghel05/algo-trading-app"

# ==========================================
# 1. GitHub Permanent Storage (अलग फ़ाइलें)
# ==========================================
def _gh_cfg():
    try:
        return st.secrets.get("GITHUB_TOKEN", "").strip(), st.secrets.get("GITHUB_REPO", DEFAULT_GH_REPO).strip()
    except Exception:
        return "", DEFAULT_GH_REPO

def gh_read(filepath):
    token, repo = _gh_cfg()
    if not token:
        return None, None
    url = f"https://api.github.com/repos/{repo}/contents/{filepath}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"}
    try:
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            data = res.json()
            return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]
    except Exception:
        pass
    return None, None

def gh_write(filepath, content, msg):
    token, repo = _gh_cfg()
    if not token:
        return False
    _, sha = gh_read(filepath)
    url = f"https://api.github.com/repos/{repo}/contents/{filepath}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"}
    payload = {"message": msg, "content": base64.b64encode(content.encode("utf-8")).decode("utf-8")}
    if sha:
        payload["sha"] = sha
    try:
        res = requests.put(url, json=payload, headers=headers, timeout=10)
        return res.status_code in [200, 201]
    except Exception:
        return False

def load_pro_history():
    raw, _ = gh_read(PRO_HISTORY_FILE)
    if raw:
        try:
            return pd.read_csv(StringIO(raw)).to_dict("records")
        except Exception:
            pass
    if os.path.exists(PRO_HISTORY_FILE):
        try:
            return pd.read_csv(PRO_HISTORY_FILE).to_dict("records")
        except Exception:
            pass
    return []

def save_pro_history(hist):
    df = pd.DataFrame(hist)
    df.to_csv(PRO_HISTORY_FILE, index=False)
    csv_txt = df.to_csv(index=False) if not df.empty else "तारीख,मोड,इंडेक्स,रणनीति,स्कोर,लॉट,एंट्री,एग्जिट,कारण,लाभ / हानि (₹)\n"
    gh_write(PRO_HISTORY_FILE, csv_txt, "Update Pro Algo trade history")

def load_pro_active():
    raw, _ = gh_read(PRO_ACTIVE_FILE)
    if raw:
        try:
            d = json.loads(raw)
            if isinstance(d, dict) and d.get("legs"):
                return d
        except Exception:
            pass
    return None

def save_pro_active(tr):
    gh_write(PRO_ACTIVE_FILE, json.dumps(tr if tr else {}, ensure_ascii=False), "Update Pro Algo active trade")

# ==========================================
# 2. Session State
# ==========================================
if "pro_history" not in st.session_state:
    st.session_state["pro_history"] = load_pro_history()
if "pro_active" not in st.session_state:
    st.session_state["pro_active"] = load_pro_active()
if "pro_capital" not in st.session_state:
    pnl_sum = sum(float(x.get("लाभ / हानि (₹)", 0)) for x in st.session_state["pro_history"] if x.get("मोड") == "Paper")
    st.session_state["pro_capital"] = 200000.0 + pnl_sum
if "pro_tg_offset" not in st.session_state:
    st.session_state["pro_tg_offset"] = 0

# ==========================================
# 3. Telegram Alerts & Control
# ==========================================
def pro_tg_send(msg):
    try:
        tok = st.secrets.get("TELEGRAM_BOT_TOKEN", "").strip()
        cid = str(st.secrets.get("TELEGRAM_CHAT_ID", "")).strip()
        if tok and cid:
            requests.post(
                f"https://api.telegram.org/bot{tok}/sendMessage",
                json={"chat_id": cid, "text": msg, "parse_mode": "HTML"},
                timeout=5
            )
    except Exception:
        pass

def pro_tg_cmd():
    try:
        tok = st.secrets.get("TELEGRAM_BOT_TOKEN", "").strip()
        cid = str(st.secrets.get("TELEGRAM_CHAT_ID", "")).strip()
        if not tok or not cid:
            return None
        res = requests.get(
            f"https://api.telegram.org/bot{tok}/getUpdates",
            params={"offset": st.session_state["pro_tg_offset"] + 1, "timeout": 1},
            timeout=3
        ).json()
        cmd = None
        for u in res.get("result", []):
            if u["update_id"] > st.session_state["pro_tg_offset"]:
                st.session_state["pro_tg_offset"] = u["update_id"]
            m = u.get("message", {})
            if str(m.get("chat", {}).get("id", "")) == cid:
                txt = m.get("text", "").strip().upper()
                if txt in ["/EXIT", "EXIT", "STOP"]:
                    cmd = "EXIT"
                elif txt in ["/STATUS", "STATUS", "MTM"]:
                    cmd = "STATUS"
        return cmd
    except Exception:
        return None

# ==========================================
# 4. Angel One SmartAPI (बिना नकली डेटा के)
# ==========================================
@st.cache_data(ttl=3600)
def get_scrip_master():
    res = requests.get("https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json", timeout=15)
    return pd.DataFrame(res.json())

def angel_auto_login():
    if "angel_jwt" in st.session_state and "angel_key" in st.session_state:
        return st.session_state["angel_jwt"], st.session_state["angel_key"]
    try:
        totp = pyotp.TOTP(st.secrets["ANGEL_TOTP"].strip().replace(" ", "")).now()
        headers = {
            "Content-Type": "application/json", "Accept": "application/json",
            "X-UserType": "USER", "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": st.secrets["ANGEL_API_KEY"].strip()
        }
        payload = {
            "clientcode": st.secrets["ANGEL_CLIENT_ID"].strip(),
            "password": st.secrets["ANGEL_MPIN"].strip(),
            "totp": totp
        }
        res = requests.post(
            "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword",
            json=payload, headers=headers, timeout=10
        ).json()
        if res.get("status"):
            st.session_state["angel_jwt"] = res["data"]["jwtToken"]
            st.session_state["angel_key"] = st.secrets["ANGEL_API_KEY"].strip()
            return st.session_state["angel_jwt"], st.session_state["angel_key"]
    except Exception:
        pass
    return None, None

def place_order_angel(jwt, akey, exch, sym, tok, side, qty):
    headers = {
        "Authorization": f"Bearer {jwt}", "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": akey
    }
    payload = {
        "variety": "NORMAL", "tradingsymbol": sym, "symboltoken": str(tok),
        "transactiontype": side, "exchange": exch, "ordertype": "MARKET",
        "producttype": "INTRADAY", "duration": "DAY", "price": "0",
        "squareoff": "0", "stoploss": "0", "quantity": str(qty)
    }
    try:
        res = requests.post(
            "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/placeOrder",
            json=payload, headers=headers, timeout=10
        ).json()
        return bool(res.get("status")), res.get("message", "Rejected")
    except Exception as e:
        return False, str(e)

@st.cache_data(ttl=8)
def fetch_intraday_vwap_orb(jwt, akey, selected_idx):
    """सुबह 9:15 से अब तक का 1-Min डेटा लाकर VWAP और Opening Range (9:15-10:00) निकालना"""
    idx_map = {
        "Nifty 50": {"exch": "NSE", "token": "99926000"},
        "Bank Nifty": {"exch": "NSE", "token": "99926009"},
        "Sensex": {"exch": "BSE", "token": "99919000"}
    }[selected_idx]
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)
    today_str = now_ist.strftime("%Y-%m-%d")
    headers = {
        "Authorization": f"Bearer {jwt}", "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": akey
    }
    payload = {
        "exchange": idx_map["exch"], "symboltoken": idx_map["token"],
        "interval": "ONE_MINUTE", "fromdate": f"{today_str} 09:15", "todate": now_ist.strftime("%Y-%m-%d %H:%M")
    }
    try:
        res = requests.post(
            "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData",
            json=payload, headers=headers, timeout=8
        ).json()
        if res.get("status") and res.get("data"):
            df = pd.DataFrame(res["data"], columns=["ts", "open", "high", "low", "close", "vol"])
            df["ts"] = pd.to_datetime(df["ts"])
            df["typical"] = (df["high"] + df["low"] + df["close"]) / 3.0
            vwap = float((df["typical"] * df["vol"]).sum() / df["vol"].sum()) if df["vol"].sum() > 0 else float(df["typical"].mean())

            orb_df = df[df["ts"].dt.strftime("%H:%M") <= "10:00"]
            if orb_df.empty:
                orb_df = df
            return {
                "ok": True,
                "spot": float(df["close"].iloc[-1]),
                "vwap": round(vwap, 2),
                "open": float(orb_df["open"].iloc[0]),
                "high": float(orb_df["high"].max()),
                "low": float(orb_df["low"].min())
            }
    except Exception:
        pass
    return {"ok": False}

@st.cache_data(ttl=8)
def fetch_live_chain_strict(jwt, akey, selected_idx):
    """केवल असली Angel One ऑप्शन चेन डेटा (कोई Random Fallback नहीं)"""
    idx_info = {
        "Nifty 50": {"exch": "NSE", "token": "99926000", "name": "NIFTY", "opt_exch": "NFO", "step": 50, "hedge": 500, "lot": 75},
        "Bank Nifty": {"exch": "NSE", "token": "99926009", "name": "BANKNIFTY", "opt_exch": "NFO", "step": 100, "hedge": 1000, "lot": 30},
        "Sensex": {"exch": "BSE", "token": "99919000", "name": "SENSEX", "opt_exch": "BFO", "step": 100, "hedge": 1500, "lot": 20}
    }[selected_idx]

    headers = {
        "Authorization": f"Bearer {jwt}", "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": akey
    }
    q_url = "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/"
    try:
        s_res = requests.post(
            q_url,
            json={"mode": "LTP", "exchangeTokens": {idx_info["exch"]: [idx_info["token"]]}},
            headers=headers, timeout=5
        ).json()
        if not s_res.get("status"):
            return None, "Spot API Error"
        spot = float(s_res["data"]["fetched"][0]["ltp"])
    except Exception as e:
        return None, str(e)

    step = idx_info["step"]
    atm = round(spot / step) * step
    master = get_scrip_master()
    opt_df = master[
        (master["name"] == idx_info["name"]) &
        (master["exch_seg"] == idx_info["opt_exch"]) &
        (master["instrumenttype"] == "OPTIDX")
    ].copy()
    opt_df["expiry_dt"] = pd.to_datetime(opt_df["expiry"], format="%d%b%Y", errors="coerce")
    opt_df = opt_df.dropna(subset=["expiry_dt"])
    today = pd.Timestamp.now().normalize()
    exps = sorted(opt_df[opt_df["expiry_dt"] >= today]["expiry_dt"].unique())
    if not exps:
        return None, "No Expiry Found"

    nearest_exp = exps[0]
    dte = int((pd.Timestamp(nearest_exp) - today).days)
    exp_str = pd.Timestamp(nearest_exp).strftime("%d-%b-%Y")
    exp_df = opt_df[opt_df["expiry_dt"] == nearest_exp].copy()
    exp_df["clean_strike"] = (exp_df["strike"].astype(float) / 100.0).astype(int)
    exp_df["opt_type"] = exp_df["symbol"].str[-2:]

    span = idx_info["hedge"] + (2 * step)
    sub = exp_df[(exp_df["clean_strike"] >= atm - span) & (exp_df["clean_strike"] <= atm + span)].copy()
    tokens = sub["token"].astype(str).tolist()
    cmap = {
        (int(r["clean_strike"]), r["opt_type"]): {"symbol": r["symbol"], "token": str(r["token"]), "exch": idx_info["opt_exch"]}
        for _, r in sub.iterrows()
    }

    fetched = {}
    for i in range(0, len(tokens), 50):
        batch = tokens[i:i+50]
        qr = requests.post(
            q_url,
            json={"mode": "FULL", "exchangeTokens": {idx_info["opt_exch"]: batch}},
            headers=headers, timeout=5
        ).json()
        if qr.get("status"):
            for item in qr["data"]["fetched"]:
                fetched[str(item["symbolToken"])] = item

    if not fetched:
        return None, "Option Quotes Empty"

    rows = []
    for k in sorted(sub["clean_strike"].unique()):
        ce_q = fetched.get(cmap.get((k, "CE"), {}).get("token", ""), {})
        pe_q = fetched.get(cmap.get((k, "PE"), {}).get("token", ""), {})
        rows.append({
            "STRIKE": k,
            "CALL VOL": int(float(ce_q.get("tradeVolume", 0) or 0)),
            "CALL CHG OI": int(float(ce_q.get("netChangeOpnInterest", 0) or 0)),
            "CALL OI": int(float(ce_q.get("opnInterest", 0) or 0)),
            "CALL LTP": float(ce_q.get("ltp", 0.0) or 0.0),
            "PUT LTP": float(pe_q.get("ltp", 0.0) or 0.0),
            "PUT OI": int(float(pe_q.get("opnInterest", 0) or 0)),
            "PUT CHG OI": int(float(pe_q.get("netChangeOpnInterest", 0) or 0)),
            "PUT VOL": int(float(pe_q.get("tradeVolume", 0) or 0)),
        })

    return {
        "df": pd.DataFrame(rows), "spot": spot, "atm": atm, "exp_str": exp_str,
        "dte": dte, "cmap": cmap, "info": idx_info
    }, None

# ==========================================
# 5. मेरा 4-Layer Decision Engine
# ==========================================
def run_4layer_decision_engine(chain_data, orb_data, g_info):
    spot = chain_data["spot"]
    atm = chain_data["atm"]
    step = chain_data["info"]["step"]
    df = chain_data["df"]

    morning_move_pct = abs((spot - orb_data["open"]) / orb_data["open"]) * 100.0 if orb_data.get("ok") else 0.0
    orb_range_pct = ((orb_data["high"] - orb_data["low"]) / spot) * 100.0 if orb_data.get("ok") else 0.4
    gamma_iv = float(g_info.get("gamma_iv_score", 40))

    if morning_move_pct >= 1.0:
        return {"decision": "NO_TRADE", "score": "0/3", "reason": f"सुबह पहले ही {morning_move_pct:.2f}% बड़ी चाल आ चुकी है (ब्रेकआउट निकल चुका है)।"}
    if gamma_iv >= 68:
        return {"decision": "NO_TRADE", "score": "0/3", "reason": f"Gamma-IV स्कोर अत्यधिक खतरनाक ({gamma_iv:.1f} >= 68) — पैनिक मार्केट।"}

    votes_up, votes_down, votes_side = 0, 0, 0
    details = []

    if orb_data.get("ok"):
        vwap = orb_data["vwap"]
        rng = max(1.0, orb_data["high"] - orb_data["low"])
        pos_in_rng = (spot - orb_data["low"]) / rng
        if spot > vwap and pos_in_rng >= 0.70:
            votes_up += 1
            details.append(f"✅ L1 (Price): Spot ({spot:,.1f}) VWAP ({vwap:,.1f}) के ऊपर & ORB High के पास (Bullish)")
        elif spot < vwap and pos_in_rng <= 0.30:
            votes_down += 1
            details.append(f"✅ L1 (Price): Spot ({spot:,.1f}) VWAP ({vwap:,.1f}) के नीचे & ORB Low के पास (Bearish)")
        elif orb_range_pct <= 0.45:
            votes_side += 1
            details.append(f"✅ L1 (Price): छोटी रेंज ({orb_range_pct:.2f}%) और VWAP के पास (Sideways)")
        else:
            details.append(f"⚠️ L1 (Price): दिशा स्पष्ट नहीं (VWAP: {vwap:,.1f}, Range: {orb_range_pct:.2f}%)")
    else:
        details.append("⚠️ L1 (Price): ORB डेटा प्रतीक्षारत")

    up3 = df[(df["STRIKE"] > atm) & (df["STRIKE"] <= atm + 3 * step)]["CALL CHG OI"].sum()
    dn3 = df[(df["STRIKE"] < atm) & (df["STRIKE"] >= atm - 3 * step)]["PUT CHG OI"].sum()
    if dn3 > 0 and dn3 >= up3 * 1.30:
        votes_up += 1
        details.append(f"✅ L2 (OI): Put Chg OI ({int(dn3):,}) > Call ({int(up3):,}) +30% (Bullish)")
    elif up3 > 0 and up3 >= dn3 * 1.30:
        votes_down += 1
        details.append(f"✅ L2 (OI): Call Chg OI ({int(up3):,}) > Put ({int(dn3):,}) +30% (Bearish)")
    elif up3 > 0 and dn3 > 0 and abs(up3 - dn3) <= max(up3, dn3) * 0.20:
        votes_side += 1
        details.append("✅ L2 (OI): दोनों ओर बराबर Call व Put राइटिंग (Sideways)")
    else:
        details.append(f"⚠️ L2 (OI): OI डेटा में टकराव (Call: {int(up3):,} vs Put: {int(dn3):,})")

    g_dir = g_info.get("direction", "SIDEWAYS")
    if g_dir == "UP":
        votes_up += 1
        details.append(f"✅ L3 (Greeks): Net Delta & IV Bullish (Gamma-IV: {gamma_iv:.1f})")
    elif g_dir == "DOWN":
        votes_down += 1
        details.append(f"✅ L3 (Greeks): Net Delta & IV Bearish (Gamma-IV: {gamma_iv:.1f})")
    elif g_dir == "SIDEWAYS" and gamma_iv < 52:
        votes_side += 1
        details.append(f"✅ L3 (Greeks): शांत बाज़ार & तेज़ Theta गलन (Gamma-IV: {gamma_iv:.1f})")
    else:
        details.append(f"⚠️ L3 (Greeks): मिश्रित संकेत (Gamma-IV: {gamma_iv:.1f})")

    if votes_up >= 2 and votes_down == 0:
        return {"decision": "UP", "score": f"{votes_up}/3", "reason": "पक्की तेज़ी (High-Probability Bullish)", "details": details}
    elif votes_down >= 2 and votes_up == 0:
        return {"decision": "DOWN", "score": f"{votes_down}/3", "reason": "पक्की मंदी (High-Probability Bearish)", "details": details}
    elif votes_side >= 2 and gamma_iv < 52:
        return {"decision": "SIDEWAYS", "score": f"{votes_side}/3", "reason": "पक्का साइडवेज (Range-Bound Decay)", "details": details}
    else:
        return {"decision": "NO_TRADE", "score": "टकराव", "reason": "डेटा आपस में टकरा रहा है — आज पूंजी बचाना ही मुनाफ़ा है (No-Trade)।", "details": details}

# ==========================================
# 6. 0.25 Delta OTM स्ट्राइक चयन
# ==========================================
def build_pro_legs(decision, chain_data):
    atm = chain_data["atm"]
    step = chain_data["info"]["step"]
    hedge = chain_data["info"]["hedge"]
    dte = chain_data["dte"]
    df = chain_data["df"]

    otm_offset = 0 if dte == 0 else (2 * step)

    def ltp_of(k, side):
        r = df[df["STRIKE"] == k]
        col = "CALL LTP" if side == "CE" else "PUT LTP"
        return float(r[col].iloc[0]) if not r.empty else 0.0

    if decision == "UP":
        sell_k = atm - otm_offset
        buy_k = sell_k - hedge
        legs = [
            {"action": "BUY", "strike": buy_k, "side": "PE", "entry_ltp": ltp_of(buy_k, "PE"), "closed": False},
            {"action": "SELL", "strike": sell_k, "side": "PE", "entry_ltp": ltp_of(sell_k, "PE"), "closed": False, "leg_sl_px": round(ltp_of(sell_k, "PE") * 1.38, 2)},
        ]
        name = f"Pro Bull Put Spread ({'ATM' if dte==0 else 'OTM 0.25Δ'})"
    elif decision == "DOWN":
        sell_k = atm + otm_offset
        buy_k = sell_k + hedge
        legs = [
            {"action": "BUY", "strike": buy_k, "side": "CE", "entry_ltp": ltp_of(buy_k, "CE"), "closed": False},
            {"action": "SELL", "strike": sell_k, "side": "CE", "entry_ltp": ltp_of(sell_k, "CE"), "closed": False, "leg_sl_px": round(ltp_of(sell_k, "CE") * 1.38, 2)},
        ]
        name = f"Pro Bear Call Spread ({'ATM' if dte==0 else 'OTM 0.25Δ'})"
    else:
        sell_ce = atm + (0 if dte == 0 else step)
        sell_pe = atm - (0 if dte == 0 else step)
        buy_ce = sell_ce + hedge
        buy_pe = sell_pe - hedge
        legs = [
            {"action": "BUY", "strike": buy_ce, "side": "CE", "entry_ltp": ltp_of(buy_ce, "CE"), "closed": False},
            {"action": "BUY", "strike": buy_pe, "side": "PE", "entry_ltp": ltp_of(buy_pe, "PE"), "closed": False},
            {"action": "SELL", "strike": sell_ce, "side": "CE", "entry_ltp": ltp_of(sell_ce, "CE"), "closed": False, "leg_sl_px": round(ltp_of(sell_ce, "CE") * 1.35, 2)},
            {"action": "SELL", "strike": sell_pe, "side": "PE", "entry_ltp": ltp_of(sell_pe, "PE"), "closed": False, "leg_sl_px": round(ltp_of(sell_pe, "PE") * 1.35, 2)},
        ]
        name = "Pro Strangle/Straddle + Leg-wise 35% SL"

    net_credit_pts = sum(l["entry_ltp"] if l["action"] == "SELL" else -l["entry_ltp"] for l in legs)
    return name, legs, round(net_credit_pts, 2)

# ==========================================
# 7. UI टैब्स: Live/Paper टर्मिनल + Pro Backtest
# ==========================================
jwt, akey = angel_auto_login()
if not jwt:
    st.error("🚨 Angel One SmartAPI कनेक्ट नहीं है! सुरक्षा नियम के अनुसार बिना असली डेटा के यह नया सिस्टम ट्रेड नहीं लेगा। कृपया मुख्य टर्मिनल में लॉगिन जांचें।")
    st.stop()

pro_tab1, pro_tab2 = st.tabs(["🚀 Smart 4-Layer Live/Paper टर्मिनल", "📊 Pro सिस्टम बैकटेस्ट (1-Min डेटा)"])

with pro_tab1:
    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    with c1:
        p_mode = st.radio("मोड:", ["🟡 Paper Trading", "🔴 LIVE Trading"], horizontal=True, key="pro_mode")
    with c2:
        p_idx = st.selectbox("इंडेक्स:", ["Nifty 50", "Sensex", "Bank Nifty"], key="pro_idx")
    with c3:
        p_lots = st.number_input("लॉट:", 1, 20, 1, key="pro_lots")
    with c4:
        p_event = st.toggle("📅 Event Day (No-Trade)", value=False, key="pro_evt")

    p_auto = st.toggle("⏰ 10:00 AM Smart Auto-Entry चालू रखें (केवल हाई-प्रोबेबिलिटी दिनों में ट्रेड)", value=False, key="pro_auto")

    @st.fragment(run_every=10)
    def run_pro_terminal():
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        today_str = now_ist.strftime("%Y-%m-%d")
        tg_cmd = pro_tg_cmd()

        chain_data, err = fetch_live_chain_strict(jwt, akey, p_idx)
        if err or not chain_data:
            st.error(f"⚠️ लाइव डेटा रुका हुआ है ({err}) — सुरक्षा नियम के तहत ट्रेड होल्ड पर है।")
            return

        orb_data = fetch_intraday_vwap_orb(jwt, akey, p_idx)
        g_info = evaluate_greeks_regime(p_idx, chain_data["spot"], chain_data["df"], chain_data["exp_str"])
        verdict = run_4layer_decision_engine(chain_data, orb_data, g_info)

        active = st.session_state["pro_active"]
        if active:
            st.subheader("🔥 चालू Pro पोजीशन (Leg-wise SL + Profit Lock + 65% Target)")
            legs = active["legs"]
            df_now = chain_data["df"]
            total_pnl_rs = 0.0
            any_leg_sl_triggered = False

            for i, lg in enumerate(legs):
                if lg.get("closed"):
                    total_pnl_rs += lg.get("realized_rs", 0.0)
                    continue
                r = df_now[df_now["STRIKE"] == lg["strike"]]
                col = "CALL LTP" if lg["side"] == "CE" else "PUT LTP"
                curr_px = float(r[col].iloc[0]) if not r.empty else lg["entry_ltp"]
                lg["curr_px"] = curr_px
                pts = (lg["entry_ltp"] - curr_px) if lg["action"] == "SELL" else (curr_px - lg["entry_ltp"])
                leg_rs = pts * active["qty"]
                total_pnl_rs += leg_rs

                if lg["action"] == "SELL" and curr_px >= lg.get("leg_sl_px", 999999):
                    if "LIVE" in active["mode"]:
                        place_order_angel(jwt, akey, active["exch"], lg["symbol"], lg["token"], "BUY", active["qty"])
                    lg["closed"] = True
                    lg["realized_rs"] = round(leg_rs, 2)
                    any_leg_sl_triggered = True
                    pro_tg_send(f"⚡ <b>LEG SL HIT ({active['index']})</b>\n{lg['strike']} {lg['side']} बंद किया गया (P&L: ₹{leg_rs:+,.0f})। बाकी लेग का SL Cost पर शिफ्ट!")

            if any_leg_sl_triggered:
                for lg in legs:
                    if not lg.get("closed") and lg["action"] == "SELL":
                        lg["leg_sl_px"] = lg["entry_ltp"]
                save_pro_active(active)

            max_credit_rs = active["max_credit_rs"]
            target_rs = round(max_credit_rs * 0.65, 0)
            lock_trigger_rs = round(max_credit_rs * 0.45, 0)
            locked_floor_rs = round(max_credit_rs * 0.20, 0)

            if total_pnl_rs >= lock_trigger_rs and not active.get("profit_locked"):
                active["profit_locked"] = True
                save_pro_active(active)
                pro_tg_send(f"🔒 <b>PROFIT LOCKED ({active['index']})</b>\nलाभ ₹{total_pnl_rs:+,.0f} पार! अब न्यूनतम लाभ ₹{locked_floor_rs:+,.0f} पर सुरक्षित है।")

            exit_reason = None
            if tg_cmd == "EXIT":
                exit_reason = "Telegram /exit 📲"
            elif total_pnl_rs >= target_rs and target_rs > 0:
                exit_reason = f"65% Target Achieved (₹{total_pnl_rs:+,.0f}) 🎯"
            elif active.get("profit_locked") and total_pnl_rs <= locked_floor_rs:
                exit_reason = f"Trailing Profit Lock Exit (₹{total_pnl_rs:+,.0f}) 🔒"
            elif total_pnl_rs <= -active["hard_sl_rs"]:
                exit_reason = f"Max Credit 38% SL Hit (-₹{int(active['hard_sl_rs'])}) 🛑"
            elif now_ist.hour == 15 and now_ist.minute >= 15:
                exit_reason = "3:15 PM Auto Square-Off ⏰"

            if tg_cmd == "STATUS":
                pro_tg_send(f"📊 <b>PRO ALGO STATUS</b>\nIndex: {active['index']}\nMTM: <b>₹{total_pnl_rs:+,.0f}</b>\nTarget: ₹{target_rs:,.0f} | Profit Locked: {active.get('profit_locked', False)}")

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("रणनीति", active["strategy"], active["index"])
            m2.metric("लाइव MTM", f"₹{total_pnl_rs:+,.0f}", f"Profit Lock: {'ON 🔒' if active.get('profit_locked') else 'OFF'}")
            m3.metric("65% टारगेट", f"₹{target_rs:,.0f}", f"Lock @ ₹{lock_trigger_rs:,.0f}")
            m4.metric("अधिकतम SL (38%)", f"-₹{int(active['hard_sl_rs']):,}")

            if exit_reason or st.button("🚨 पूरी Pro पोजीशन बंद करें", type="primary", use_container_width=True):
                final_reason = exit_reason if exit_reason else "Manual Square-Off"
                if "LIVE" in active["mode"]:
                    for lg in [x for x in legs if not x.get("closed") and x["action"] == "SELL"]:
                        place_order_angel(jwt, akey, active["exch"], lg["symbol"], lg["token"], "BUY", active["qty"])
                        time.sleep(0.35)
                    for lg in [x for x in legs if not x.get("closed") and x["action"] == "BUY"]:
                        place_order_angel(jwt, akey, active["exch"], lg["symbol"], lg["token"], "SELL", active["qty"])
                        time.sleep(0.35)

                if "Paper" in active["mode"]:
                    st.session_state["pro_capital"] += round(total_pnl_rs, 2)

                st.session_state["pro_history"].insert(0, {
                    "तारीख": today_str, "मोड": "Paper" if "Paper" in active["mode"] else "LIVE",
                    "इंडेक्स": active["index"], "रणनीति": active["strategy"], "स्कोर": active["score"],
                    "लॉट": active["lots"], "एंट्री": active["entry_time"], "एग्जिट": now_ist.strftime("%I:%M:%S %p"),
                    "कारण": final_reason, "लाभ / हानि (₹)": round(total_pnl_rs, 2)
                })
                save_pro_history(st.session_state["pro_history"])
                save_pro_active(None)
                st.session_state["pro_active"] = None
                pro_tg_send(f"✅ <b>PRO TRADE CLOSED</b>\n{active['index']} | {final_reason}\nFinal P&L: <b>₹{total_pnl_rs:+,.0f}</b>")
                st.rerun()

        else:
            st.info(f"📍 **{p_idx} Spot:** `{chain_data['spot']:,.2f}` | **VWAP:** `{orb_data.get('vwap', 'N/A')}` | **Expiry:** `{chain_data['exp_str']}` (`{chain_data['dte']} DTE`) | **पेपर बैलेंस:** `₹{st.session_state['pro_capital']:,.0f}`")

            for d_line in verdict.get("details", []):
                st.write(d_line)

            if p_event or verdict["decision"] == "NO_TRADE":
                msg_stop = "📅 आज Event Day टॉगल चालू है — कोई ट्रेड नहीं।" if p_event else f"⚪ **NO-TRADE (छुट्टी):** {verdict['reason']}"
                st.warning(msg_stop)
            else:
                strat_name, plan_legs, net_credit_pts = build_pro_legs(verdict["decision"], chain_data)
                lot_qty = chain_data["info"]["lot"]
                total_qty = lot_qty * p_lots
                max_credit_rs = round(net_credit_pts * total_qty, 0)
                hard_sl_rs = max(1200.0 * p_lots, round(max_credit_rs * 0.38, 0))

                st.success(f"🎯 **सिग्नल ({verdict['score']}):** {verdict['reason']} ➔ **{strat_name}** | नेट क्रेडिट: `₹{max_credit_rs:,.0f}` | 38% SL: `₹{hard_sl_rs:,.0f}`")
                for lg in plan_legs:
                    st.write(f"• **{lg['action']}** {lg['strike']} {lg['side']} @ `₹{lg['entry_ltp']}`")

                already_traded_today = any(h.get("तारीख") == today_str for h in st.session_state["pro_history"])
                auto_trigger = (p_auto and not already_traded_today and now_ist.hour == 10 and now_ist.minute <= 2)

                if auto_trigger or st.button(f"🚀 {strat_name} एग्जीक्यूट करें ({p_lots} Lot)", use_container_width=True):
                    ordered = [x for x in plan_legs if x["action"] == "BUY"] + [x for x in plan_legs if x["action"] == "SELL"]
                    for lg in ordered:
                        ci = chain_data["cmap"].get((lg["strike"], lg["side"]), {"symbol": "", "token": "0"})
                        lg["symbol"], lg["token"] = ci["symbol"], ci["token"]
                        if "LIVE" in p_mode:
                            ok_o, err_o = place_order_angel(jwt, akey, chain_data["info"]["opt_exch"], lg["symbol"], lg["token"], lg["action"], total_qty)
                            if not ok_o:
                                st.error(f"ऑर्डर विफल: {err_o}")
                                return
                            time.sleep(0.35)

                    new_tr = {
                        "mode": p_mode, "index": p_idx, "strategy": strat_name, "score": verdict["score"],
                        "lots": p_lots, "qty": total_qty, "exch": chain_data["info"]["opt_exch"],
                        "legs": ordered, "entry_time": now_ist.strftime("%I:%M:%S %p"),
                        "max_credit_rs": max_credit_rs, "hard_sl_rs": hard_sl_rs, "profit_locked": False
                    }
                    st.session_state["pro_active"] = new_tr
                    save_pro_active(new_tr)
                    pro_tg_send(f"🚀 <b>PRO ALGO ENTRY ({p_idx})</b>\n{strat_name}\nCredit: ₹{max_credit_rs:,.0f} | Max SL: -₹{int(hard_sl_rs):,}")
                    st.rerun()

        if st.session_state["pro_history"]:
            st.markdown("### 📜 Pro Algo ट्रेड बुक (अलग हिस्ट्री)")
            st.dataframe(pd.DataFrame(st.session_state["pro_history"]), use_container_width=True, hide_index=True)

    run_pro_terminal()

# ==========================================
# TAB 2: PRO BACKTEST (0.25 Delta OTM + ORB + Leg-wise SL + Profit Lock)
# ==========================================
with pro_tab2:
    st.subheader("📊 Smart 4-Layer Pro सिस्टम बैकटेस्ट (1-Min Angel One डेटा)")
    st.caption("इसमें 0.25 Delta OTM स्ट्राइक, सुबह की 1% रेंज नो-ट्रेड फ़िल्टर, Leg-wise 35% SL (Straddle में) और 65% प्रॉफिट टारगेट शामिल है।")

    b1, b2, b3 = st.columns(3)
    with b1:
        pbt_idx = st.selectbox("इंडेक्स:", ["Nifty 50", "Sensex", "Bank Nifty"], key="pbt_idx")
    with b2:
        pbt_days = st.selectbox("अवधि:", ["30 दिन (1 महीना)", "90 दिन (3 महीने)", "365 दिन (1 साल)"], index=2, key="pbt_days")
    with b3:
        pbt_lots = st.number_input("लॉट:", 1, 25, 1, key="pbt_lots")

    def _bs_px(S, K, T, iv, side):
        if T <= 1e-6:
            return max(0.05, S - K) if side == "CE" else max(0.05, K - S)
        r = 0.065
        d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
        d2 = d1 - iv * math.sqrt(T)
        nd1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
        nd2 = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
        px = (S * nd1 - K * math.exp(-r * T) * nd2) if side == "CE" else (K * math.exp(-r * T) * (1.0 - nd2) - S * (1.0 - nd1))
        return max(0.5, px)

    if st.button("🚀 Pro बैकटेस्ट चलाएं", use_container_width=True):
        cfg = {
            "Nifty 50": {"exch": "NSE", "tok": "99926000", "step": 50, "lot": 75, "hedge": 500, "exp_wd": 3, "iv": 0.135},
            "Bank Nifty": {"exch": "NSE", "tok": "99926009", "step": 100, "lot": 30, "hedge": 1000, "exp_wd": 2, "iv": 0.165},
            "Sensex": {"exch": "BSE", "tok": "99919000", "step": 100, "lot": 20, "hedge": 1500, "exp_wd": 4, "iv": 0.145}
        }[pbt_idx]

        days_n = int(pbt_days.split(" ")[0])
        ist = pytz.timezone('Asia/Kolkata')
        end_dt = datetime.now(ist)
        rem = days_n
        candles = []
        headers = {
            "Authorization": f"Bearer {jwt}", "Content-Type": "application/json", "Accept": "application/json",
            "X-UserType": "USER", "X-SourceID": "WEB", "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1", "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": akey
        }
        with st.spinner("1-मिनट ऐतिहासिक डेटा पर Pro रणनीति का परीक्षण हो रहा है..."):
            while rem > 0:
                ch = min(30, rem)
                st_dt = end_dt - timedelta(days=ch)
                r = requests.post(
                    "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData",
                    json={"exchange": cfg["exch"], "symboltoken": cfg["tok"], "interval": "ONE_MINUTE",
                          "fromdate": st_dt.strftime("%Y-%m-%d 09:15"), "todate": end_dt.strftime("%Y-%m-%d 15:30")},
                    headers=headers, timeout=15
                ).json()
                if r.get("status") and r.get("data"):
                    candles.extend(r["data"])
                time.sleep(0.15)
                end_dt = st_dt - timedelta(minutes=1)
                rem -= ch

        if not candles:
            st.error("ऐतिहासिक डेटा लोड नहीं हो पाया।")
        else:
            df = pd.DataFrame(candles, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
            df["ts"] = pd.to_datetime(df["ts"])
            if df["ts"].dt.tz is None:
                df["ts"] = df["ts"].dt.tz_localize("Asia/Kolkata")
            df = df.drop_duplicates(subset=["ts"]).sort_values("ts").set_index("ts")
            df["Date"] = df.index.date

            logs = []
            skipped_days = 0
            total_qty = cfg["lot"] * pbt_lots
            step, hedge = cfg["step"], cfg["hedge"]

            for dt, day_df in df.groupby("Date"):
                morn = day_df.between_time("09:15", "09:59")
                sess = day_df.between_time("10:00", "15:15")
                if len(morn) < 5 or len(sess) < 15:
                    continue

                o915 = float(morn["Open"].iloc[0])
                h959 = float(morn["High"].max())
                l959 = float(morn["Low"].min())
                s1000 = float(sess["Open"].iloc[0])
                atm = round(s1000 / step) * step

                move_pct = abs((s1000 - o915) / o915) * 100.0
                rng_pct = ((h959 - l959) / s1000) * 100.0
                pos_rng = (s1000 - l959) / max(1.0, h959 - l959)

                # नो-ट्रेड फ़िल्टर (1% से बड़ी चाल या असमंजस वाली रेंज)
                if move_pct >= 1.0 or (0.45 < rng_pct < 0.85 and 0.30 < pos_rng < 0.70):
                    skipped_days += 1
                    continue

                dte = (cfg["exp_wd"] - dt.weekday()) % 7
                iv = min(0.30, max(cfg["iv"] * 0.85, (rng_pct / 100.0) * 16.0))
                t0 = (dte + (315.0 / 375.0)) / 365.0
                otm_off = 0 if dte == 0 else (2 * step)

                if rng_pct <= 0.45:
                    mode_name = "Sideways Strangle (Leg SL)"
                    s_ce, s_pe = atm + (0 if dte == 0 else step), atm - (0 if dte == 0 else step)
                    b_ce, b_pe = s_ce + hedge, s_pe - hedge
                    e_sce, e_spe = _bs_px(s1000, s_ce, t0, iv, "CE"), _bs_px(s1000, s_pe, t0, iv, "PE")
                    e_bce, e_bpe = _bs_px(s1000, b_ce, t0, iv, "CE"), _bs_px(s1000, b_pe, t0, iv, "PE")
                    credit_rs = ((e_sce + e_spe) - (e_bce + e_bpe)) * total_qty
                    ce_closed, pe_closed = False, False
                    ce_sl_px, pe_sl_px = e_sce * 1.35, e_spe * 1.35
                    realized_ce, realized_pe = 0.0, 0.0
                    status = "3:15 Exit ✅"
                    n_bars = len(sess)
                    for i_b, (_, row) in enumerate(sess.iterrows()):
                        tc = (dte + max(15.0, 315.0 * (1 - i_b / max(1, n_bars - 1))) / 375.0) / 365.0
                        c_sce = _bs_px(float(row["High"]), s_ce, tc, iv, "CE")
                        c_spe = _bs_px(float(row["Low"]), s_pe, tc, iv, "PE")
                        if not ce_closed and c_sce >= ce_sl_px:
                            ce_closed = True
                            realized_ce = (e_sce - c_sce) * total_qty
                            pe_sl_px = e_spe  # दूसरा लेग Cost पर शिफ्ट
                        if not pe_closed and c_spe >= pe_sl_px:
                            pe_closed = True
                            realized_pe = (e_spe - c_spe) * total_qty
                            ce_sl_px = e_sce
                        if ce_closed and pe_closed:
                            status = "Both Leg SL ❌"
                            break
                    close_s = float(sess["Close"].iloc[-1])
                    t_end = (dte + 15.0 / 375.0) / 365.0
                    if not ce_closed:
                        realized_ce = (e_sce - _bs_px(close_s, s_ce, t_end, iv, "CE")) * total_qty
                    if not pe_closed:
                        realized_pe = (e_spe - _bs_px(close_s, s_pe, t_end, iv, "PE")) * total_qty
                    hedge_pnl = ((_bs_px(close_s, b_ce, t_end, iv, "CE") - e_bce) + (_bs_px(close_s, b_pe, t_end, iv, "PE") - e_bpe)) * total_qty
                    net_rs = round(realized_ce + realized_pe + hedge_pnl - (140 * pbt_lots), 0)
                else:
                    is_up = (pos_rng >= 0.70)
                    mode_name = f"{'Bull Put' if is_up else 'Bear Call'} (0.25Δ OTM)"
                    side = "PE" if is_up else "CE"
                    sk = (atm - otm_off) if is_up else (atm + otm_off)
                    bk = (sk - hedge) if is_up else (sk + hedge)
                    es, eb = _bs_px(s1000, sk, t0, iv, side), _bs_px(s1000, bk, t0, iv, side)
                    credit_rs = max(500.0, (es - eb) * total_qty)
                    hard_sl = max(1200.0 * pbt_lots, credit_rs * 0.38)
                    target = credit_rs * 0.65
                    status = "3:15 Exit ✅"
                    n_bars = len(sess)
                    final_pnl = 0.0
                    for i_b, (_, row) in enumerate(sess.iterrows()):
                        tc = (dte + max(15.0, 315.0 * (1 - i_b / max(1, n_bars - 1))) / 375.0) / 365.0
                        w_s = float(row["Low"]) if is_up else float(row["High"])
                        b_s = float(row["High"]) if is_up else float(row["Low"])
                        worst_mtm = ((es - _bs_px(w_s, sk, tc, iv, side)) + (_bs_px(w_s, bk, tc, iv, side) - eb)) * total_qty
                        best_mtm = ((es - _bs_px(b_s, sk, tc, iv, side)) + (_bs_px(b_s, bk, tc, iv, side) - eb)) * total_qty
                        if worst_mtm <= -hard_sl:
                            status = "38% Credit SL ❌"
                            final_pnl = -hard_sl
                            break
                        if best_mtm >= target:
                            status = "65% Target 🎯"
                            final_pnl = target
                            break
                    if status == "3:15 Exit ✅":
                        cs = float(sess["Close"].iloc[-1])
                        te = (dte + 15.0 / 375.0) / 365.0
                        final_pnl = ((es - _bs_px(cs, sk, te, iv, side)) + (_bs_px(cs, bk, te, iv, side) - eb)) * total_qty
                    net_rs = round(final_pnl - (110 * pbt_lots), 0)

                logs.append({"तारीख": str(dt), "रणनीति": mode_name, "नतीजा": status, "शुद्ध लाभ/हानि (₹)": net_rs})

            res_df = pd.DataFrame(logs)
            if not res_df.empty:
                res_df["कुल पूंजी वक्र (₹)"] = res_df["शुद्ध लाभ/हानि (₹)"].cumsum()
                wins = len(res_df[res_df["शुद्ध लाभ/हानि (₹)"] > 0])
                wr = round((wins / len(res_df)) * 100, 1)
                tot_p = res_df["शुद्ध लाभ/हानि (₹)"].sum()
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("कुल शुद्ध लाभ", f"₹{tot_p:,.0f}", f"{len(res_df)} ट्रेड लिए")
                m2.metric("सटीकता (Win Rate)", f"{wr}%", f"{wins} जीत / {len(res_df)-wins} हार")
                m3.metric("बचाए गए No-Trade दिन", f"{skipped_days} दिन", "खराब बाज़ार स्किप")
                m4.metric("औसत प्रति ट्रेड", f"₹{round(tot_p/len(res_df), 0):,.0f}")
                st.line_chart(res_df.set_index("तारीख")["कुल पूंजी वक्र (₹)"])
                st.dataframe(res_df, use_container_width=True, hide_index=True)
