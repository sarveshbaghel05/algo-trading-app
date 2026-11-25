import streamlit as st
import pandas as pd
import numpy as np
import requests
import time
import os
import math
import json
from io import StringIO
from datetime import datetime, timedelta
import pytz

from oi_base import (
    ensure_angel_session,
    get_scrip_master_df,
    get_index_meta,
    get_live_expiry_info,
    get_exact_t_years,
    build_backtest_dte_map,
    compute_accurate_chg_oi,
    load_algo_settings,
    set_toggle_setting,
    is_strategy_traded_today,
    mark_strategy_traded_today,
    get_saved_tg_offset,
    save_tg_offset,
    gh_read_text,
    gh_write_text,
)


# ==========================================
# Built-in Safe Fallback (सही Gamma-IV स्केल के साथ)
# ==========================================
def _pro_norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _pro_norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _pro_iv(option_price, spot, strike, t_years, opt_type="CE"):
    if option_price <= 0.5 or t_years <= 1e-6 or spot <= 0 or strike <= 0:
        return 0.14
    intrinsic = max(0.0, spot - strike) if opt_type == "CE" else max(0.0, strike - spot)
    if option_price <= intrinsic + 0.2:
        return 0.08
    low, high, r = 0.02, 2.00, 0.065
    for _ in range(35):
        mid = 0.5 * (low + high)
        d1 = (math.log(spot / strike) + (r + 0.5 * mid * mid) * t_years) / (mid * math.sqrt(t_years))
        d2 = d1 - mid * math.sqrt(t_years)
        px = (spot * _pro_norm_cdf(d1) - strike * math.exp(-r * t_years) * _pro_norm_cdf(d2)) if opt_type == "CE" else (strike * math.exp(-r * t_years) * _pro_norm_cdf(-d2) - spot * _pro_norm_cdf(-d1))
        if px > option_price:
            high = mid
        else:
            low = mid
    return round(0.5 * (low + high), 4)


def _calc_pro_gamma_iv_score(avg_gamma, spot_price, avg_iv_decimal):
    """0-100 स्केल पर सटीक Gamma-IV स्कोर (सामान्य दिन: 20-45, तेज़ हलचल: 50-65, पैनिक: 65+)"""
    one_pct_move = spot_price * 0.01
    delta_shift_1pct = avg_gamma * one_pct_move
    raw_score = (delta_shift_1pct * 95.0) + (avg_iv_decimal * 65.0)
    return round(min(100.0, max(5.0, raw_score)), 1)


def _builtin_pro_greeks(selected_idx, spot_price, full_df, expiry_str="Unknown", oi_valid=True):
    meta = get_index_meta(selected_idx)
    step = meta["step"]
    atm_strike = round(spot_price / step) * step
    if full_df is None or full_df.empty or expiry_str in ["Unknown", "Simulation", "DEMO"] or not oi_valid:
        return {
            "direction": "WAIT", "headline": "⏳ असली डेटा व क्लोज़िंग OI की प्रतीक्षा (WAIT)",
            "gamma_iv_risk": 0.0, "gamma_iv_score": 0.0, "trade_plan": "प्रतीक्षा करें।"
        }
    atm_row = full_df[full_df["STRIKE"] == atm_strike]
    if atm_row.empty:
        return {
            "direction": "WAIT", "headline": "⏳ ATM डेटा लोड हो रहा है...",
            "gamma_iv_risk": 0.0, "gamma_iv_score": 0.0, "trade_plan": "प्रतीक्षा करें।"
        }
    t_years = max(get_exact_t_years(expiry_str), 1.0 / (365.0 * 24.0))
    ce_iv = max(_pro_iv(float(atm_row["CALL LTP"].iloc[0]), spot_price, atm_strike, t_years, "CE"), 0.05)
    pe_iv = max(_pro_iv(float(atm_row["PUT LTP"].iloc[0]), spot_price, atm_strike, t_years, "PE"), 0.05)
    r = 0.065
    d1_c = (math.log(spot_price / atm_strike) + (r + 0.5 * ce_iv * ce_iv) * t_years) / (ce_iv * math.sqrt(t_years))
    d1_p = (math.log(spot_price / atm_strike) + (r + 0.5 * pe_iv * pe_iv) * t_years) / (pe_iv * math.sqrt(t_years))
    net_delta = round(_pro_norm_cdf(d1_c) + (_pro_norm_cdf(d1_p) - 1.0), 3)
    avg_gamma = ((_pro_norm_pdf(d1_c) / (spot_price * ce_iv * math.sqrt(t_years))) + (_pro_norm_pdf(d1_p) / (spot_price * pe_iv * math.sqrt(t_years)))) / 2.0
    avg_iv_dec = (ce_iv + pe_iv) / 2.0
    gamma_iv_risk = _calc_pro_gamma_iv_score(avg_gamma, spot_price, avg_iv_dec)

    if gamma_iv_risk >= 68.0:
        return {
            "direction": "WAIT", "headline": f"🚨 अत्यधिक Gamma-IV खतरा ({gamma_iv_risk}/100) — ट्रेड से बचें",
            "gamma_iv_risk": gamma_iv_risk, "gamma_iv_score": gamma_iv_risk, "trade_plan": "नई सेलिंग से बचें।"
        }

    up_3 = full_df[(full_df["STRIKE"] > atm_strike) & (full_df["STRIKE"] <= atm_strike + 3 * step)]
    dn_3 = full_df[(full_df["STRIKE"] < atm_strike) & (full_df["STRIKE"] >= atm_strike - 3 * step)]
    up_ce = int(up_3["CALL CHG OI"].sum()) if not up_3.empty else 0
    dn_pe = int(dn_3["PUT CHG OI"].sum()) if not dn_3.empty else 0

    bull_pts = (2 if (dn_pe > 0 and dn_pe >= up_ce * 1.25) else 0) + (1 if net_delta >= 0.06 else 0)
    bear_pts = (2 if (up_ce > 0 and up_ce >= dn_pe * 1.25) else 0) + (1 if net_delta <= -0.06 else 0)

    if bull_pts >= 2 and bull_pts > bear_pts:
        return {"direction": "UP", "headline": "🟢 BULLISH", "gamma_iv_risk": gamma_iv_risk, "gamma_iv_score": gamma_iv_risk}
    elif bear_pts >= 2 and bear_pts > bull_pts:
        return {"direction": "DOWN", "headline": "🔴 BEARISH", "gamma_iv_risk": gamma_iv_risk, "gamma_iv_score": gamma_iv_risk}
    elif (up_ce > 0 or dn_pe > 0) and gamma_iv_risk < 55.0 and abs(net_delta) < 0.08:
        return {"direction": "SIDEWAYS", "headline": "🟡 SIDEWAYS", "gamma_iv_risk": gamma_iv_risk, "gamma_iv_score": gamma_iv_risk}
    return {"direction": "WAIT", "headline": "⏳ WAIT", "gamma_iv_risk": gamma_iv_risk, "gamma_iv_score": gamma_iv_risk}


evaluate_greeks_regime = _builtin_pro_greeks

PRO_HISTORY_FILE = "pro_algo_history.csv"
PRO_ACTIVE_FILE = "pro_active_trade.json"


# ==========================================
# 1. GitHub Permanent Storage
# ==========================================
def load_pro_history():
    raw, _, status = gh_read_text(PRO_HISTORY_FILE)
    if status == "OK" and raw:
        try:
            df = pd.read_csv(StringIO(raw))
            df.to_csv(PRO_HISTORY_FILE, index=False)
            return df.to_dict("records")
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
    gh_write_text(PRO_HISTORY_FILE, csv_txt, "Update Pro Algo trade history")


def load_pro_active():
    raw, _, status = gh_read_text(PRO_ACTIVE_FILE)
    if status == "OK" and raw:
        try:
            d = json.loads(raw)
            if isinstance(d, dict) and d.get("legs"):
                return d
        except Exception:
            pass
    return None


def save_pro_active(tr):
    gh_write_text(PRO_ACTIVE_FILE, json.dumps(tr if tr else {}, ensure_ascii=False), "Update Pro Algo active trade")


# ==========================================
# 2. Telegram Alerts & Safe Control
# ==========================================
def pro_tg_send(msg):
    try:
        tok = st.secrets.get("TELEGRAM_BOT_TOKEN", "").strip()
        cid = str(st.secrets.get("TELEGRAM_CHAT_ID", "")).strip()
        if tok and cid:
            requests.post(
                f"https://api.telegram.org/bot{tok}/sendMessage",
                json={"chat_id": cid, "text": msg, "parse_mode": "HTML"},
                timeout=5,
            )
    except Exception:
        pass


def pro_tg_cmd():
    try:
        tok = st.secrets.get("TELEGRAM_BOT_TOKEN", "").strip()
        cid = str(st.secrets.get("TELEGRAM_CHAT_ID", "")).strip()
        if not tok or not cid:
            return None
        last_off = get_saved_tg_offset()
        res = requests.get(
            f"https://api.telegram.org/bot{tok}/getUpdates",
            params={"offset": last_off + 1, "timeout": 1},
            timeout=3,
        ).json()
        cmd = None
        max_u = last_off
        now_ts = time.time()
        for u in res.get("result", []):
            uid = int(u.get("update_id", 0))
            if uid > max_u:
                max_u = uid
            m = u.get("message", {})
            if now_ts - float(m.get("date", now_ts)) > 90:
                continue
            if str(m.get("chat", {}).get("id", "")) == cid:
                txt = m.get("text", "").strip().upper()
                if txt in ["/EXIT", "EXIT", "STOP"]:
                    cmd = "EXIT"
                elif txt in ["/STATUS", "STATUS", "MTM"]:
                    cmd = "STATUS"
        if max_u > last_off:
            save_tg_offset(max_u)
        return cmd
    except Exception:
        return None


# ==========================================
# 3. Angel One Order & Strict Live Data
# ==========================================
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
    last_err = "Rejected"
    for _ in range(3):
        try:
            res = requests.post(
                "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/placeOrder",
                json=payload, headers=headers, timeout=10
            ).json()
            if res.get("status"):
                return True, res.get("data", {}).get("orderid", "Placed")
            last_err = res.get("message", "Rejected")
        except Exception as e:
            last_err = str(e)
        time.sleep(0.4)
    return False, last_err


@st.cache_data(ttl=8)
def fetch_intraday_vwap_orb(jwt, akey, selected_idx, live_spot):
    meta = get_index_meta(selected_idx)
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    today_str = now_ist.strftime("%Y-%m-%d")
    headers = {
        "Authorization": f"Bearer {jwt}", "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": akey
    }
    payload = {
        "exchange": meta["exch"], "symboltoken": meta["token"],
        "interval": "ONE_MINUTE", "fromdate": f"{today_str} 09:15", "todate": now_ist.strftime("%Y-%m-%d %H:%M")
    }
    try:
        res = requests.post(
            "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData",
            json=payload, headers=headers, timeout=8
        ).json()
        if res.get("status") and res.get("data"):
            df = pd.DataFrame(res["data"], columns=["ts", "open", "high", "low", "close", "vol"])
            df["ts_str"] = df["ts"].astype(str)
            df["d_part"] = df["ts_str"].str.slice(0, 10)
            df["t_part"] = df["ts_str"].str.slice(11, 16)
            df = df[(df["d_part"] == today_str) & (df["t_part"] >= "09:15")].sort_values("t_part").reset_index(drop=True)

            if live_spot > 0 and not df.empty:
                low_b, high_b = live_spot * 0.985, live_spot * 1.015
                df = df[
                    (df["close"] >= low_b) & (df["close"] <= high_b) &
                    (df["open"] >= low_b) & (df["open"] <= high_b) &
                    (df["high"] >= low_b) & (df["high"] <= high_b) &
                    (df["low"] >= low_b) & (df["low"] <= high_b)
                ].reset_index(drop=True)

            if not df.empty:
                df["typical"] = (df["high"] + df["low"] + df["close"]) / 3.0
                vwap = float((df["typical"] * df["vol"]).sum() / df["vol"].sum()) if df["vol"].sum() > 0 else float(df["typical"].mean())
                orb_df = df[df["t_part"] <= "10:00"]
                if orb_df.empty:
                    orb_df = df
                return {
                    "ok": True, "spot": live_spot, "vwap": round(vwap, 2),
                    "open": float(df["open"].iloc[0]), "high": float(orb_df["high"].max()), "low": float(orb_df["low"].min())
                }
    except Exception:
        pass
    return {"ok": False}


@st.cache_data(ttl=8)
def _fetch_live_chain_raw(jwt, akey, selected_idx):
    meta = get_index_meta(selected_idx)
    headers = {
        "Authorization": f"Bearer {jwt}", "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": akey
    }
    q_url = "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/"
    try:
        s_res = requests.post(
            q_url, json={"mode": "LTP", "exchangeTokens": {meta["exch"]: [meta["token"]]}}, headers=headers, timeout=5
        ).json()
        if not s_res.get("status"):
            return None, "Spot API Error"
        spot = float(s_res["data"]["fetched"][0].get("ltp", 0.0) or 0.0)
        if spot <= 0:
            return None, "Spot LTP 0 आ रहा है"
    except Exception as e:
        return None, str(e)

    step = meta["step"]
    atm = round(spot / step) * step
    exp_str, trading_dte, _ = get_live_expiry_info(selected_idx)
    if exp_str == "Unknown":
        return None, "No Expiry Found"

    master = get_scrip_master_df()
    opt_df = master[
        (master["name"] == meta["name"]) &
        (master["exch_seg"] == meta["opt_exch"]) &
        (master["instrumenttype"] == "OPTIDX")
    ].copy()
    opt_df["expiry_dt"] = pd.to_datetime(opt_df["expiry"], format="%d%b%Y", errors="coerce")
    opt_df = opt_df.dropna(subset=["expiry_dt"])
    exp_df = opt_df[opt_df["expiry_dt"] == pd.to_datetime(exp_str, format="%d-%b-%Y")].copy()
    exp_df["clean_strike"] = (exp_df["strike"].astype(float) / 100.0).astype(int)
    exp_df["opt_type"] = exp_df["symbol"].str[-2:]

    span = meta["hedge"] + (3 * step)
    sub = exp_df[(exp_df["clean_strike"] >= atm - span) & (exp_df["clean_strike"] <= atm + span)].copy()
    tokens = sub["token"].astype(str).tolist()
    cmap = {
        (int(r["clean_strike"]), r["opt_type"]): {"symbol": r["symbol"], "token": str(r["token"]), "exch": meta["opt_exch"]}
        for _, r in sub.iterrows()
    }

    fetched = {}
    for i in range(0, len(tokens), 50):
        batch = tokens[i:i+50]
        qr = requests.post(q_url, json={"mode": "FULL", "exchangeTokens": {meta["opt_exch"]: batch}}, headers=headers, timeout=5).json()
        if qr.get("status"):
            for item in qr["data"]["fetched"]:
                fetched[str(item["symbolToken"])] = item

    if not fetched:
        return None, "Option Quotes Empty"

    rows = []
    for k in sorted(sub["clean_strike"].unique()):
        ce_tok = cmap.get((k, "CE"), {}).get("token", "")
        pe_tok = cmap.get((k, "PE"), {}).get("token", "")
        ce_q = fetched.get(ce_tok, {})
        pe_q = fetched.get(pe_tok, {})
        rows.append({
            "CE_TOKEN": ce_tok, "PE_TOKEN": pe_tok, "STRIKE": k,
            "CALL VOL": int(float(ce_q.get("tradeVolume", 0) or 0)),
            "CALL OI": int(float(ce_q.get("opnInterest", 0) or 0)),
            "API_CALL_CHG": int(float(ce_q.get("netChangeOpenInterest", 0) or 0)),
            "CALL LTP": float(ce_q.get("ltp", 0.0) or 0.0),
            "PUT LTP": float(pe_q.get("ltp", 0.0) or 0.0),
            "PUT OI": int(float(pe_q.get("opnInterest", 0) or 0)),
            "API_PUT_CHG": int(float(pe_q.get("netChangeOpenInterest", 0) or 0)),
            "PUT VOL": int(float(pe_q.get("tradeVolume", 0) or 0)),
        })

    return {
        "raw_df": pd.DataFrame(rows), "spot": spot,
        "atm": atm, "exp_str": exp_str, "dte": trading_dte, "cmap": cmap, "info": meta
    }, None


def fetch_live_chain_strict(jwt, akey, selected_idx):
    data, err = _fetch_live_chain_raw(jwt, akey, selected_idx)
    if err or not data:
        return data, err

    df_acc, oi_valid, oi_source = compute_accurate_chg_oi(selected_idx, data["raw_df"])
    data["df"] = df_acc[["STRIKE", "CALL VOL", "CALL CHG OI", "CALL OI", "CALL LTP", "PUT LTP", "PUT OI", "PUT CHG OI", "PUT VOL"]]
    data["oi_valid"] = oi_valid
    data["oi_source"] = oi_source
    return data, None


# ==========================================
# 4. 4-Layer Decision Engine
# ==========================================
def run_4layer_decision_engine(chain_data, orb_data, g_info):
    spot = chain_data["spot"]
    atm = chain_data["atm"]
    step = chain_data["info"]["step"]
    df = chain_data["df"]
    oi_valid = chain_data.get("oi_valid", True)

    if not oi_valid:
        return {
            "decision": "NO_TRADE",
            "score": "0/3",
            "reason": "कल का क्लोज़िंग OI उपलब्ध नहीं — गलत Change in OI से बचने के लिए नो-ट्रेड।",
            "details": ["⏳ L2 (OI): कल का क्लोज़िंग OI उपलब्ध नहीं (N/A)"]
        }

    today_open = orb_data.get("open", spot) if orb_data.get("ok") else spot
    signed_move_pct = ((spot - today_open) / today_open) * 100.0 if (today_open > 0 and spot > 0) else 0.0
    morning_move_pct = abs(signed_move_pct)
    orb_range_pct = ((orb_data["high"] - orb_data["low"]) / spot) * 100.0 if orb_data.get("ok") else 0.35
    gamma_iv = float(g_info.get("gamma_iv_risk", g_info.get("gamma_iv_score", 35)))

    if morning_move_pct >= 1.0:
        dir_txt = "ऊपर (UP)" if signed_move_pct > 0 else "नीचे (DOWN)"
        return {
            "decision": "NO_TRADE",
            "score": "0/3",
            "reason": f"सुबह ({today_open:,.1f}) से अब तक {morning_move_pct:.2f}% {dir_txt} की बड़ी चाल आ चुकी है (ब्रेकआउट निकल चुका है)।"
        }
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
            details.append(f"✅ L1 (Price): Spot ({spot:,.1f}) VWAP ({vwap:,.1f}) के ऊपर & Range High के पास (Bullish)")
        elif spot < vwap and pos_in_rng <= 0.30:
            votes_down += 1
            details.append(f"✅ L1 (Price): Spot ({spot:,.1f}) VWAP ({vwap:,.1f}) के नीचे & Range Low के पास (Bearish)")
        elif orb_range_pct <= 0.45:
            votes_side += 1
            details.append(f"✅ L1 (Price): छोटी रेंज ({orb_range_pct:.2f}%) और VWAP के पास (Sideways)")
        else:
            details.append(f"⚠️ L1 (Price): दिशा स्पष्ट नहीं (VWAP: {vwap:,.1f}, Range: {orb_range_pct:.2f}%)")

    up3 = int(df[(df["STRIKE"] > atm) & (df["STRIKE"] <= atm + 3 * step)]["CALL CHG OI"].sum())
    dn3 = int(df[(df["STRIKE"] < atm) & (df["STRIKE"] >= atm - 3 * step)]["PUT CHG OI"].sum())
    if dn3 > 0 and dn3 >= up3 * 1.25:
        votes_up += 1
        details.append(f"✅ L2 (OI): Put Chg OI ({dn3:+,}) > Call ({up3:+,}) +25% (Bullish)")
    elif up3 > 0 and up3 >= dn3 * 1.25:
        votes_down += 1
        details.append(f"✅ L2 (OI): Call Chg OI ({up3:+,}) > Put ({dn3:+,}) +25% (Bearish)")
    elif (up3 > 0 or dn3 > 0) and abs(up3 - dn3) <= max(up3, dn3) * 0.25:
        votes_side += 1
        details.append(f"✅ L2 (OI): दोनों ओर बराबर Call ({up3:+,}) व Put ({dn3:+,}) राइटिंग (Sideways)")
    else:
        details.append(f"⚠️ L2 (OI): OI डेटा में टकराव (Call: {up3:+,} vs Put: {dn3:+,})")

    g_dir = g_info.get("direction", "WAIT")
    if g_dir == "UP":
        votes_up += 1
        details.append(f"✅ L3 (Greeks): Net Delta & IV Bullish (Gamma-IV: {gamma_iv:.1f}/100)")
    elif g_dir == "DOWN":
        votes_down += 1
        details.append(f"✅ L3 (Greeks): Net Delta & IV Bearish (Gamma-IV: {gamma_iv:.1f}/100)")
    elif g_dir == "SIDEWAYS" and gamma_iv < 55:
        votes_side += 1
        details.append(f"✅ L3 (Greeks): शांत बाज़ार & तेज़ Theta गलन (Gamma-IV: {gamma_iv:.1f}/100)")
    else:
        details.append(f"⚠️ L3 (Greeks): स्पष्ट सहमति नहीं (Gamma-IV: {gamma_iv:.1f}/100)")

    if votes_up >= 2 and votes_down == 0:
        return {"decision": "UP", "score": f"{votes_up}/3", "reason": "पक्की तेज़ी (High-Probability Bullish)", "details": details}
    elif votes_down >= 2 and votes_up == 0:
        return {"decision": "DOWN", "score": f"{votes_down}/3", "reason": "पक्की मंदी (High-Probability Bearish)", "details": details}
    elif votes_side >= 2 and gamma_iv < 55:
        return {"decision": "SIDEWAYS", "score": f"{votes_side}/3", "reason": "पक्का साइडवेज (Range-Bound Decay)", "details": details}
    else:
        return {"decision": "NO_TRADE", "score": "टकराव", "reason": "डेटा आपस में टकरा रहा है — आज पूंजी बचाना ही मुनाफ़ा है (No-Trade)।", "details": details}


# ==========================================
# 5. OTM स्ट्राइक चयन
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
            {"action": "BUY", "strike": buy_k, "side": "PE", "entry_ltp": ltp_of(buy_k, "PE"), "last_valid_ltp": ltp_of(buy_k, "PE"), "closed": False},
            {"action": "SELL", "strike": sell_k, "side": "PE", "entry_ltp": ltp_of(sell_k, "PE"), "last_valid_ltp": ltp_of(sell_k, "PE"), "closed": False, "leg_sl_px": round(ltp_of(sell_k, "PE") * 1.38, 2)},
        ]
        name = f"Pro Bull Put Spread ({'ATM' if dte==0 else 'OTM 0.25Δ'})"
    elif decision == "DOWN":
        sell_k = atm + otm_offset
        buy_k = sell_k + hedge
        legs = [
            {"action": "BUY", "strike": buy_k, "side": "CE", "entry_ltp": ltp_of(buy_k, "CE"), "last_valid_ltp": ltp_of(buy_k, "CE"), "closed": False},
            {"action": "SELL", "strike": sell_k, "side": "CE", "entry_ltp": ltp_of(sell_k, "CE"), "last_valid_ltp": ltp_of(sell_k, "CE"), "closed": False, "leg_sl_px": round(ltp_of(sell_k, "CE") * 1.38, 2)},
        ]
        name = f"Pro Bear Call Spread ({'ATM' if dte==0 else 'OTM 0.25Δ'})"
    else:
        sell_ce = atm + (0 if dte == 0 else step)
        sell_pe = atm - (0 if dte == 0 else step)
        buy_ce = sell_ce + hedge
        buy_pe = sell_pe - hedge
        legs = [
            {"action": "BUY", "strike": buy_ce, "side": "CE", "entry_ltp": ltp_of(buy_ce, "CE"), "last_valid_ltp": ltp_of(buy_ce, "CE"), "closed": False},
            {"action": "BUY", "strike": buy_pe, "side": "PE", "entry_ltp": ltp_of(buy_pe, "PE"), "last_valid_ltp": ltp_of(buy_pe, "PE"), "closed": False},
            {"action": "SELL", "strike": sell_ce, "side": "CE", "entry_ltp": ltp_of(sell_ce, "CE"), "last_valid_ltp": ltp_of(sell_ce, "CE"), "closed": False, "leg_sl_px": round(ltp_of(sell_ce, "CE") * 1.35, 2)},
            {"action": "SELL", "strike": sell_pe, "side": "PE", "entry_ltp": ltp_of(sell_pe, "PE"), "last_valid_ltp": ltp_of(sell_pe, "PE"), "closed": False, "leg_sl_px": round(ltp_of(sell_pe, "PE") * 1.35, 2)},
        ]
        name = "Pro Strangle/Straddle + Leg-wise 35% SL"

    net_credit_pts = sum(l["entry_ltp"] if l["action"] == "SELL" else -l["entry_ltp"] for l in legs)
    return name, legs, round(net_credit_pts, 2)


# ==========================================
# 6. Session State & UI
# ==========================================
if "pro_history" not in st.session_state:
    st.session_state["pro_history"] = load_pro_history()
if "pro_active" not in st.session_state:
    st.session_state["pro_active"] = load_pro_active()
if "pro_capital" not in st.session_state:
    pnl_sum = sum(float(x.get("लाभ / हानि (₹)", 0)) for x in st.session_state["pro_history"] if x.get("मोड") == "Paper")
    st.session_state["pro_capital"] = 200000.0 + pnl_sum
if "pro_spot_track" not in st.session_state:
    st.session_state["pro_spot_track"] = {}

st.title("🧠 Smart 4-Layer Pro Algo Terminal")
st.caption("0.25 Delta OTM स्ट्राइक | Leg-wise 35% SL | VWAP + ORB + 3-Strike OI कन्फर्मेशन | सही Expiry/DTE व Gamma-IV स्कोर")

jwt, akey = ensure_angel_session()
if not jwt:
    st.error("🚨 Angel One SmartAPI कनेक्ट नहीं है! सुरक्षा नियम के अनुसार बिना असली डेटा के यह सिस्टम ट्रेड नहीं लेगा।")
    st.stop()

pro_tab1, pro_tab2 = st.tabs(["🚀 Smart 4-Layer Live/Paper टर्मिनल", "📊 Pro सिस्टम बैकटेस्ट (सही DTE व Lot Size)"])

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

    pro_live_ok = True
    if "LIVE" in p_mode:
        st.warning("⚠️ LIVE मोड चालू है! असली पैसे से ऑर्डर लगेंगे।")
        pro_live_ok = st.checkbox("✅ मैं पुष्टि करता हूँ कि असली पैसे से ऑर्डर लगें", key="pro_live_confirm")

    cfg_pro = load_algo_settings()
    p_auto = st.toggle(
        "⏰ 10:00 AM Smart Auto-Entry चालू रखें (केवल हाई-प्रोबेबिलिटी दिनों में ट्रेड)",
        value=bool(cfg_pro.get("pro_auto")),
        key="pro_auto_widget",
    )
    set_toggle_setting("pro_auto", p_auto)

    @st.fragment(run_every=10)
    def run_pro_terminal(is_unlocked):
        ist = pytz.timezone("Asia/Kolkata")
        now_ist = datetime.now(ist)
        today_str = now_ist.strftime("%Y-%m-%d")
        tg_cmd = pro_tg_cmd()

        chain_data, err = fetch_live_chain_strict(jwt, akey, p_idx)
        if err or not chain_data:
            st.error(f"⚠️ लाइव डेटा रुका हुआ है ({err}) — सुरक्षा नियम के तहत ट्रेड होल्ड पर है।")
            return

        live_spot = chain_data["spot"]
        orb_data = fetch_intraday_vwap_orb(jwt, akey, p_idx, live_spot)

        trk_key = f"{today_str}_{p_idx}"
        if not orb_data.get("ok"):
            if trk_key not in st.session_state["pro_spot_track"]:
                st.session_state["pro_spot_track"][trk_key] = {"open": live_spot, "high": live_spot, "low": live_spot}
            else:
                st.session_state["pro_spot_track"][trk_key]["high"] = max(st.session_state["pro_spot_track"][trk_key]["high"], live_spot)
                st.session_state["pro_spot_track"][trk_key]["low"] = min(st.session_state["pro_spot_track"][trk_key]["low"], live_spot)
            trk = st.session_state["pro_spot_track"][trk_key]
            orb_data = {
                "ok": True, "spot": live_spot,
                "vwap": round((trk["high"] + trk["low"] + live_spot) / 3.0, 2),
                "open": trk["open"], "high": trk["high"], "low": trk["low"]
            }

        g_info = evaluate_greeks_regime(p_idx, live_spot, chain_data["df"], chain_data["exp_str"], oi_valid=chain_data.get("oi_valid", True))
        verdict = run_4layer_decision_engine(chain_data, orb_data, g_info)

        active = st.session_state.get("pro_active")
        if active:
            st.subheader("🔥 चालू Pro पोजीशन (Leg-wise SL + Profit Lock + 65% Target)")
            legs = active["legs"]
            df_now = chain_data["df"]
            total_pnl_rs = 0.0
            any_leg_sl_triggered = False

            for lg in legs:
                if lg.get("closed"):
                    total_pnl_rs += lg.get("realized_rs", 0.0)
                    continue
                r = df_now[df_now["STRIKE"] == lg["strike"]]
                col = "CALL LTP" if lg["side"] == "CE" else "PUT LTP"
                if not r.empty and float(r[col].iloc[0]) > 0:
                    curr_px = float(r[col].iloc[0])
                    lg["last_valid_ltp"] = curr_px
                else:
                    curr_px = float(lg.get("last_valid_ltp", lg["entry_ltp"]))
                lg["curr_px"] = curr_px
                pts = (lg["entry_ltp"] - curr_px) if lg["action"] == "SELL" else (curr_px - lg["entry_ltp"])
                leg_rs = pts * active["qty"]
                total_pnl_rs += leg_rs

                if lg["action"] == "SELL" and curr_px >= lg.get("leg_sl_px", 999999):
                    if "LIVE" in active["mode"]:
                        ok_l, err_l = place_order_angel(jwt, akey, active["exch"], lg["symbol"], lg["token"], "BUY", active["qty"])
                        if not ok_l:
                            pro_tg_send(f"🚨 <b>LEG SL ORDER FAILED ({lg['symbol']}):</b> {err_l}")
                            continue
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
                pro_tg_send(f"📊 <b>PRO ALGO STATUS</b>\nIndex: {active['index']}\nMTM: <b>₹{total_pnl_rs:+,.0f}</b> | Target: ₹{target_rs:,.0f}")

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("रणनीति", active["strategy"], active["index"])
            m2.metric("लाइव MTM", f"₹{total_pnl_rs:+,.0f}", f"Profit Lock: {'ON 🔒' if active.get('profit_locked') else 'OFF'}")
            m3.metric("65% टारगेट", f"₹{target_rs:,.0f}", f"Lock @ ₹{lock_trigger_rs:,.0f}")
            m4.metric("अधिकतम SL (38%)", f"-₹{int(active['hard_sl_rs']):,}")

            if exit_reason or st.button("🚨 पूरी Pro पोजीशन बंद करें", type="primary", use_container_width=True):
                final_reason = exit_reason if exit_reason else "Manual Square-Off"
                if "LIVE" in active["mode"]:
                    for lg in [x for x in legs if not x.get("closed") and x["action"] == "SELL"]:
                        ok_x, err_x = place_order_angel(jwt, akey, active["exch"], lg["symbol"], lg["token"], "BUY", active["qty"])
                        if not ok_x:
                            pro_tg_send(f"🚨 <b>PRO EXIT FAILED ({lg['symbol']}):</b> {err_x} — तुरंत ब्रोकर ऐप से बंद करें!")
                            st.error(f"एग्जिट विफल: {err_x}")
                            return
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
            disp_open = orb_data.get("open", live_spot)
            move_txt = f"{((live_spot - disp_open) / disp_open) * 100.0:+.2f}%" if disp_open > 0 else "0.00%"
            st.info(
                f"📍 **{p_idx} Spot:** `{live_spot:,.2f}` | **Session Base:** `{disp_open:,.2f}` (`{move_txt}`) | "
                f"**VWAP:** `{orb_data.get('vwap', 'N/A')}` | **Expiry:** `{chain_data['exp_str']}` (**`{chain_data['dte']} DTE`**) | "
                f"**Gamma-IV:** `{g_info.get('gamma_iv_score', 0)}/100` | **पेपर बैलेंस:** `₹{st.session_state['pro_capital']:,.0f}`"
            )

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

                st.success(f"🎯 **सिग्नल ({verdict['score']}):** {verdict['reason']} ➔ **{strat_name}** (Lot Size: {lot_qty}) | नेट क्रेडिट: `₹{max_credit_rs:,.0f}` | 38% SL: `₹{hard_sl_rs:,.0f}`")
                for lg in plan_legs:
                    st.write(f"• **{lg['action']}** {lg['strike']} {lg['side']} @ `₹{lg['entry_ltp']}`")

                already_traded_today = is_strategy_traded_today("pro", today_str)
                auto_trigger = (p_auto and not already_traded_today and now_ist.hour == 10 and now_ist.minute <= 3)

                if auto_trigger or st.button(f"🚀 {strat_name} एग्जीक्यूट करें ({p_lots} Lot)", disabled=("LIVE" in p_mode and not is_unlocked), use_container_width=True):
                    if any(float(x.get("entry_ltp", 0)) <= 0 for x in plan_legs):
                        st.error("किसी स्ट्राइक का भाव 0 आ रहा है — एंट्री रोकी गई।")
                        return
                    ordered = [x for x in plan_legs if x["action"] == "BUY"] + [x for x in plan_legs if x["action"] == "SELL"]
                    done_legs = []
                    for lg in ordered:
                        ci = chain_data["cmap"].get((lg["strike"], lg["side"]), {"symbol": "", "token": "0"})
                        lg["symbol"], lg["token"] = ci["symbol"], ci["token"]
                        if "LIVE" in p_mode:
                            ok_o, err_o = place_order_angel(jwt, akey, chain_data["info"]["opt_exch"], lg["symbol"], lg["token"], lg["action"], total_qty)
                            if not ok_o:
                                for prev in reversed(done_legs):
                                    place_order_angel(jwt, akey, chain_data["info"]["opt_exch"], prev["symbol"], prev["token"], "SELL" if prev["action"] == "BUY" else "BUY", total_qty)
                                st.error(f"ऑर्डर विफल (रोलबैक पूरा): {err_o}")
                                return
                            done_legs.append(lg)
                            time.sleep(0.35)

                    new_tr = {
                        "mode": p_mode, "index": p_idx, "strategy": strat_name, "score": verdict["score"],
                        "lots": p_lots, "qty": total_qty, "exch": chain_data["info"]["opt_exch"],
                        "legs": ordered, "entry_time": now_ist.strftime("%I:%M:%S %p"),
                        "max_credit_rs": max_credit_rs, "hard_sl_rs": hard_sl_rs, "profit_locked": False
                    }
                    st.session_state["pro_active"] = new_tr
                    save_pro_active(new_tr)
                    mark_strategy_traded_today("pro", today_str)
                    pro_tg_send(f"🚀 <b>PRO ALGO ENTRY ({p_idx})</b>\n{strat_name}\nCredit: ₹{max_credit_rs:,.0f} | Max SL: -₹{int(hard_sl_rs):,}")
                    st.rerun()

        if st.session_state["pro_history"]:
            st.markdown("### 📜 Pro Algo ट्रेड बुक (अलग हिस्ट्री)")
            st.dataframe(pd.DataFrame(st.session_state["pro_history"]), use_container_width=True, hide_index=True)

    run_pro_terminal(pro_live_ok)

# ==========================================
# TAB 2: PRO BACKTEST (सही DTE व Lot Size सहित)
# ==========================================
with pro_tab2:
    st.subheader("📊 Smart 4-Layer Pro सिस्टम बैकटेस्ट (सही DTE व Lot Size)")
    st.caption("इसमें छुट्टियों के हिसाब से सही 0 DTE / 1 DTE, Scrip Master का असली Lot Size, 1% नो-ट्रेड फ़िल्टर और Leg-wise SL शामिल है।")

    b1, b2, b3, b4 = st.columns(4)
    with b1:
        pbt_idx = st.selectbox("इंडेक्स:", ["Nifty 50", "Sensex", "Bank Nifty"], key="pbt_idx")
    with b2:
        pbt_days = st.selectbox("अवधि:", ["30 दिन (1 महीना)", "90 दिन (3 महीने)", "180 दिन (6 महीने)", "365 दिन (1 साल)"], index=1, key="pbt_days")
    with b3:
        pbt_lots = st.number_input("लॉट:", 1, 25, 1, key="pbt_lots")
    with b4:
        pbt_deduct_costs = st.checkbox("ब्रोकरेज + टैक्स + स्लिपेज घटाएं", value=True, key="pbt_costs")

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

    def _calc_pro_angel_charges(sell_turnover, buy_turnover, order_count):
        total_turnover = sell_turnover + buy_turnover
        brokerage = 20.0 * order_count
        stt = sell_turnover * 0.0015
        exch = total_turnover * 0.0003553
        sebi = total_turnover * 0.000001
        stamp = buy_turnover * 0.00003
        gst = (brokerage + exch + sebi) * 0.18
        return round(brokerage + stt + exch + sebi + stamp + gst, 2)

    if st.button("🚀 Pro बैकटेस्ट चलाएं", use_container_width=True):
        meta = get_index_meta(pbt_idx)
        base_iv = 0.135 if pbt_idx == "Nifty 50" else (0.165 if pbt_idx == "Bank Nifty" else 0.145)
        days_n = int(pbt_days.split(" ")[0])
        ist = pytz.timezone("Asia/Kolkata")
        end_dt = datetime.now(ist)
        rem = days_n
        candles = []
        headers = {
            "Authorization": f"Bearer {jwt}", "Content-Type": "application/json", "Accept": "application/json",
            "X-UserType": "USER", "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": akey
        }
        with st.spinner("1-मिनट ऐतिहासिक डेटा लोड हो रहा है..."):
            while rem > 0:
                ch = min(30, rem)
                st_dt = end_dt - timedelta(days=ch)
                r = requests.post(
                    "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData",
                    json={"exchange": meta["exch"], "symboltoken": meta["token"], "interval": "ONE_MINUTE",
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
            df["ts_str"] = df["ts"].astype(str)
            df["Date"] = df["ts_str"].str.slice(0, 10)
            df["Time"] = df["ts_str"].str.slice(11, 16)
            df = df.drop_duplicates(subset=["Date", "Time"]).sort_values(["Date", "Time"])

            unique_dates = [datetime.strptime(d, "%Y-%m-%d").date() for d in sorted(df["Date"].unique())]
            dte_map = build_backtest_dte_map(unique_dates, pbt_idx)

            logs = []
            skipped_days = 0
            total_costs_paid = 0.0
            total_qty = meta["lot"] * pbt_lots
            step, hedge = meta["step"], meta["hedge"]
            slip_mult = 1.0 if pbt_deduct_costs else 0.0

            for dt_str, day_df in df.groupby("Date"):
                morn = day_df[(day_df["Time"] >= "09:15") & (day_df["Time"] <= "09:59")].copy()
                sess = day_df[(day_df["Time"] >= "10:00") & (day_df["Time"] <= "15:15")].copy()
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

                if move_pct >= 1.0 or (0.45 < rng_pct < 0.85 and 0.30 < pos_rng < 0.70):
                    skipped_days += 1
                    continue

                dt_obj = datetime.strptime(dt_str, "%Y-%m-%d").date()
                dte = dte_map.get(dt_obj, 1)
                iv = min(0.30, max(base_iv * 0.85, (rng_pct / 100.0) * 16.0))
                t0 = (dte + (315.0 / 375.0)) / 365.0
                otm_off = 0 if dte == 0 else (2 * step)

                if rng_pct <= 0.45:
                    mode_name = "Sideways Strangle (Leg SL)"
                    s_ce, s_pe = atm + (0 if dte == 0 else step), atm - (0 if dte == 0 else step)
                    b_ce, b_pe = s_ce + hedge, s_pe - hedge
                    raw_sce, raw_spe = _bs_px(s1000, s_ce, t0, iv, "CE"), _bs_px(s1000, s_pe, t0, iv, "PE")
                    raw_bce, raw_bpe = _bs_px(s1000, b_ce, t0, iv, "CE"), _bs_px(s1000, b_pe, t0, iv, "PE")

                    e_sce = round(raw_sce - (max(0.4, raw_sce * 0.004) * slip_mult), 2)
                    e_spe = round(raw_spe - (max(0.4, raw_spe * 0.004) * slip_mult), 2)
                    e_bce = round(raw_bce + (max(0.4, raw_bce * 0.012) * slip_mult), 2)
                    e_bpe = round(raw_bpe + (max(0.4, raw_bpe * 0.012) * slip_mult), 2)

                    ce_closed, pe_closed = False, False
                    ce_sl_px, pe_sl_px = e_sce * 1.35, e_spe * 1.35
                    x_sce, x_spe = e_sce, e_spe
                    status = "3:15 Exit ✅"
                    n_bars = len(sess)

                    for i_b, (_, row) in enumerate(sess.iterrows()):
                        tc = (dte + max(15.0, 315.0 * (1 - i_b / max(1, n_bars - 1))) / 375.0) / 365.0
                        c_sce = _bs_px(float(row["High"]), s_ce, tc, iv, "CE")
                        c_spe = _bs_px(float(row["Low"]), s_pe, tc, iv, "PE")
                        if not ce_closed and c_sce >= ce_sl_px:
                            ce_closed = True
                            x_sce = c_sce + (max(0.4, c_sce * 0.008) * slip_mult)
                            pe_sl_px = e_spe
                        if not pe_closed and c_spe >= pe_sl_px:
                            pe_closed = True
                            x_spe = c_spe + (max(0.4, c_spe * 0.008) * slip_mult)
                            ce_sl_px = e_sce
                        if ce_closed and pe_closed:
                            status = "Both Leg SL ❌"
                            break

                    close_s = float(sess["Close"].iloc[-1])
                    t_end = (dte + 15.0 / 375.0) / 365.0
                    if not ce_closed:
                        raw_xce = _bs_px(close_s, s_ce, t_end, iv, "CE")
                        x_sce = raw_xce + (max(0.4, raw_xce * 0.004) * slip_mult)
                    if not pe_closed:
                        raw_xpe = _bs_px(close_s, s_pe, t_end, iv, "PE")
                        x_spe = raw_xpe + (max(0.4, raw_xpe * 0.004) * slip_mult)

                    raw_xbce, raw_xbpe = _bs_px(close_s, b_ce, t_end, iv, "CE"), _bs_px(close_s, b_pe, t_end, iv, "PE")
                    x_bce = max(0.05, raw_xbce - (max(0.4, raw_xbce * 0.012) * slip_mult))
                    x_bpe = max(0.05, raw_xbpe - (max(0.4, raw_xbpe * 0.012) * slip_mult))

                    gross_rs = ((e_sce - x_sce) + (e_spe - x_spe) + (x_bce - e_bce) + (x_bpe - e_bpe)) * total_qty
                    charges = _calc_pro_angel_charges((e_sce + e_spe + x_bce + x_bpe) * total_qty, (e_bce + e_bpe + x_sce + x_spe) * total_qty, 8) if pbt_deduct_costs else 0.0
                    total_costs_paid += charges
                    net_rs = round(gross_rs - charges, 0)
                else:
                    is_up = (pos_rng >= 0.70)
                    mode_name = f"{'Bull Put' if is_up else 'Bear Call'} (0.25Δ OTM)"
                    side = "PE" if is_up else "CE"
                    sk = (atm - otm_off) if is_up else (atm + otm_off)
                    bk = (sk - hedge) if is_up else (sk + hedge)
                    raw_es, raw_eb = _bs_px(s1000, sk, t0, iv, side), _bs_px(s1000, bk, t0, iv, side)
                    es = round(raw_es - (max(0.4, raw_es * 0.004) * slip_mult), 2)
                    eb = round(raw_eb + (max(0.4, raw_eb * 0.012) * slip_mult), 2)

                    credit_rs = max(500.0, (es - eb) * total_qty)
                    hard_sl = max(1200.0 * pbt_lots, credit_rs * 0.38)
                    target = credit_rs * 0.65
                    status = "3:15 Exit ✅"
                    n_bars = len(sess)
                    xs, xb = es, eb

                    for i_b, (_, row) in enumerate(sess.iterrows()):
                        tc = (dte + max(15.0, 315.0 * (1 - i_b / max(1, n_bars - 1))) / 375.0) / 365.0
                        w_s = float(row["Low"]) if is_up else float(row["High"])
                        b_s = float(row["High"]) if is_up else float(row["Low"])
                        w_cs, w_cb = _bs_px(w_s, sk, tc, iv, side), _bs_px(w_s, bk, tc, iv, side)
                        b_cs, b_cb = _bs_px(b_s, sk, tc, iv, side), _bs_px(b_s, bk, tc, iv, side)
                        worst_mtm = ((es - w_cs) + (w_cb - eb)) * total_qty
                        best_mtm = ((es - b_cs) + (b_cb - eb)) * total_qty
                        if worst_mtm <= -hard_sl:
                            status = "38% Credit SL ❌"
                            xs = w_cs + (max(0.4, w_cs * 0.008) * slip_mult)
                            xb = max(0.05, w_cb - (max(0.4, w_cb * 0.012) * slip_mult))
                            break
                        if best_mtm >= target:
                            status = "65% Target 🎯"
                            xs = b_cs + (max(0.4, b_cs * 0.004) * slip_mult)
                            xb = max(0.05, b_cb - (max(0.4, b_cb * 0.012) * slip_mult))
                            break

                    if status == "3:15 Exit ✅":
                        cs = float(sess["Close"].iloc[-1])
                        te = (dte + 15.0 / 375.0) / 365.0
                        raw_xs, raw_xb = _bs_px(cs, sk, te, iv, side), _bs_px(cs, bk, te, iv, side)
                        xs = raw_xs + (max(0.4, raw_xs * 0.004) * slip_mult)
                        xb = max(0.05, raw_xb - (max(0.4, raw_xb * 0.012) * slip_mult))

                    gross_rs = ((es - xs) + (xb - eb)) * total_qty
                    charges = _calc_pro_angel_charges((es + xb) * total_qty, (eb + xs) * total_qty, 4) if pbt_deduct_costs else 0.0
                    total_costs_paid += charges
                    net_rs = round(gross_rs - charges, 0)

                logs.append({
                    "तारीख": dt_str, "DTE": f"{dte} DTE", "रणनीति": mode_name,
                    "नतीजा": status, "ब्रोकरेज+टैक्स (₹)": round(charges, 0), "शुद्ध लाभ/हानि (₹)": net_rs
                })

            res_df = pd.DataFrame(logs)
            if not res_df.empty:
                res_df["कुल पूंजी वक्र (₹)"] = res_df["शुद्ध लाभ/हानि (₹)"].cumsum()
                wins = len(res_df[res_df["शुद्ध लाभ/हानि (₹)"] > 0])
                wr = round((wins / len(res_df)) * 100, 1)
                tot_p = res_df["शुद्ध लाभ/हानि (₹)"].sum()
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("कुल शुद्ध लाभ (Net)", f"₹{tot_p:,.0f}", f"{len(res_df)} ट्रेड लिए")
                m2.metric("सटीकता (Win Rate)", f"{wr}%", f"{wins} जीत / {len(res_df)-wins} हार")
                m3.metric("बचाए गए No-Trade दिन", f"{skipped_days} दिन", f"Lot Size: {meta['lot']} Qty")
                m4.metric("कुल ब्रोकरेज + टैक्स", f"-₹{int(total_costs_paid):,}")
                st.line_chart(res_df.set_index("तारीख")["कुल पूंजी (₹)"])
                st.dataframe(res_df, use_container_width=True, hide_index=True)
