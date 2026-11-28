import json
import math
import os
import time
from datetime import datetime, timedelta
from io import StringIO

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pytz
import requests
import streamlit as st

from oi_base import (
    build_backtest_dte_map,
    compute_accurate_chg_oi,
    ensure_angel_session,
    get_exact_t_years,
    get_index_meta,
    get_live_expiry_info,
    get_saved_tg_offset,
    get_scrip_master_df,
    get_top_tickers_snapshot,
    gh_list_oc_logs,
    gh_read_text,
    gh_write_text,
    is_strategy_traded_today,
    load_algo_settings,
    load_recorded_oc_dataframe,
    mark_strategy_traded_today,
    save_tg_offset,
    update_algo_settings_batch,
)

# ==========================================
# 1. पेज कॉन्फ़िगरेशन व 16-EMA जैसा साफ कार्ड लुक
# ==========================================
st.set_page_config(
    page_title="Option Seller Algo Terminal",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
.block-container {
    padding-top: 1rem;
    padding-bottom: 2rem;
}
.top-idx-card {
    background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
    border: 1px solid #334155;
    border-radius: 12px;
    padding: 12px 14px;
    color: #f8fafc;
    margin-bottom: 8px;
}
.top-idx-name {
    font-size: 12px;
    color: #94a3b8;
    font-weight: 600;
}
.top-idx-ltp {
    font-size: 19px;
    font-weight: 700;
    color: #ffffff;
    margin: 2px 0;
}
.top-idx-up {
    font-size: 12px;
    color: #34d399;
    font-weight: 600;
}
.top-idx-dn {
    font-size: 12px;
    color: #f87171;
    font-weight: 600;
}
.hero-signal-card {
    background: linear-gradient(90deg, #1e293b, #0f172a);
    padding: 16px 20px;
    border-radius: 12px;
    border-left: 6px solid #38bdf8;
    color: #ffffff;
    margin-bottom: 12px;
}
</style>
""",
    unsafe_allow_html=True,
)

PAPER_HISTORY_FILE = "paper_trades_history.csv"
ACTIVE_MULTI_FILE = "active_trades_multi.json"
EMA_MULTI_ACTIVE_FILE = "ema16_oi_multi_active.json"
PRO_MULTI_ACTIVE_FILE = "pro_multi_active.json"
OC_RECORD_FILE = "recorded_oc_history.csv"


# ==========================================
# 2. GitHub स्टोरेज व Telegram हेल्पर्स
# ==========================================
def load_paper_history():
    raw_csv, _, status = gh_read_text(PAPER_HISTORY_FILE)
    if status == "OK" and raw_csv is not None:
        try:
            df = pd.read_csv(StringIO(raw_csv))
            df.to_csv(PAPER_HISTORY_FILE, index=False)
            return df.to_dict("records")
        except Exception:
            return []
    if os.path.exists(PAPER_HISTORY_FILE):
        try:
            return pd.read_csv(PAPER_HISTORY_FILE).to_dict("records")
        except Exception:
            return []
    return []


def save_paper_history(hist_list):
    df = pd.DataFrame(hist_list)
    df.to_csv(PAPER_HISTORY_FILE, index=False)
    csv_text = (
        df.to_csv(index=False)
        if not df.empty
        else "तारीख,मोड,इंडेक्स,रणनीति,स्प्रेड,लॉट,एंट्री समय,एग्जिट समय,कारण,लाभ / हानि (₹)\n"
    )
    gh_write_text(PAPER_HISTORY_FILE, csv_text, "Update paper trade history")


def load_active_multi_map():
    raw_json, _, status = gh_read_text(ACTIVE_MULTI_FILE)
    if status == "OK" and raw_json:
        try:
            obj = json.loads(raw_json)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return {}


def save_active_multi_map(m_map):
    gh_write_text(
        ACTIVE_MULTI_FILE,
        json.dumps(m_map or {}, ensure_ascii=False, indent=2),
        "Update multi active trades",
    )


def _load_json_dict_gh(fname):
    raw_json, _, status = gh_read_text(fname)
    if status == "OK" and raw_json:
        try:
            obj = json.loads(raw_json)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return {}


def send_telegram_message(message_text):
    try:
        token = st.secrets.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = str(st.secrets.get("TELEGRAM_CHAT_ID", "")).strip()
        if not token or not chat_id:
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message_text, "parse_mode": "HTML"},
            timeout=5,
        ).json()
        return bool(resp.get("ok"))
    except Exception:
        return False


def poll_telegram_exit_only():
    """केवल /EXIT कमांड जांचता है; /STATUS वर्कर एक साथ सभी पोजीशन्स के साथ भेजता है।"""
    try:
        token = st.secrets.get("TELEGRAM_BOT_TOKEN", "").strip()
        allowed_id = str(st.secrets.get("TELEGRAM_CHAT_ID", "")).strip()
        if not token or not allowed_id:
            return False
        last_offset = get_saved_tg_offset()
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        resp = requests.get(url, params={"offset": last_offset + 1, "timeout": 1}, timeout=3).json()
        if not resp.get("ok"):
            return False
        exit_requested = False
        max_upd_id = last_offset
        now_ts = time.time()
        for upd in resp.get("result", []):
            u_id = int(upd.get("update_id", 0))
            if u_id > max_upd_id:
                max_upd_id = u_id
            msg = upd.get("message", {})
            msg_ts = float(msg.get("date", now_ts))
            if now_ts - msg_ts > 120:
                continue
            chat_id = str(msg.get("chat", {}).get("id", ""))
            txt = msg.get("text", "").strip().upper()
            if chat_id == allowed_id and txt in ["/EXIT", "EXIT", "STOP", "CLOSE"]:
                exit_requested = True
        if max_upd_id > last_offset:
            save_tg_offset(max_upd_id)
        return exit_requested
    except Exception:
        return False


# ==========================================
# 3. Black-Scholes ग्रीक्स व Option Chain डेटा
# ==========================================
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x):
    return (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * x * x)


def _bs_price(spot, strike, t_years, iv, opt_type="CE", r=0.065):
    if spot <= 0 or strike <= 0:
        return 0.0
    if t_years <= 1e-6 or iv <= 1e-4:
        return max(0.0, spot - strike) if opt_type == "CE" else max(0.0, strike - spot)
    vol_sqrt = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / vol_sqrt
    d2 = d1 - vol_sqrt
    if opt_type == "CE":
        return max(0.0, spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2))
    return max(0.0, strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1))


def _bs_greeks(spot, strike, t_years, iv, opt_type="CE", r=0.065):
    if spot <= 0 or strike <= 0:
        return {"delta": 0.0, "theta": 0.0, "gamma": 0.0, "vega": 0.0, "iv": round(iv * 100.0, 2)}
    t_safe = max(t_years, 1.0 / (365.0 * 24.0))
    iv_safe = max(iv, 0.05)
    vol_sqrt = iv_safe * math.sqrt(t_safe)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv_safe * iv_safe) * t_safe) / vol_sqrt
    d2 = d1 - vol_sqrt
    pdf_d1 = _norm_pdf(d1)
    gamma = pdf_d1 / (spot * vol_sqrt)
    vega = (spot * pdf_d1 * math.sqrt(t_safe)) / 100.0
    if opt_type == "CE":
        delta = _norm_cdf(d1)
        theta = (-(spot * pdf_d1 * iv_safe) / (2.0 * math.sqrt(t_safe)) - r * strike * math.exp(-r * t_safe) * _norm_cdf(d2)) / 365.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (-(spot * pdf_d1 * iv_safe) / (2.0 * math.sqrt(t_safe)) + r * strike * math.exp(-r * t_safe) * _norm_cdf(-d2)) / 365.0
    return {
        "delta": round(delta, 3),
        "theta": round(theta, 2),
        "gamma": round(gamma, 6),
        "vega": round(vega, 2),
        "iv": round(iv_safe * 100.0, 2),
    }


def _implied_volatility(option_price, spot, strike, t_years, opt_type="CE", r=0.065):
    intrinsic = max(0.0, spot - strike) if opt_type == "CE" else max(0.0, strike - spot)
    if option_price <= intrinsic + 0.05 or spot <= 0 or strike <= 0 or t_years <= 1e-6:
        return 0.14
    low, high = 0.02, 2.0
    for _ in range(35):
        mid = 0.5 * (low + high)
        px = _bs_price(spot, strike, t_years, mid, opt_type, r)
        if px > option_price:
            high = mid
        else:
            low = mid
    return round(0.5 * (low + high), 4)


def place_angel_order(jwt_token, api_key, exchange, symbol, token, transaction_type, qty):
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
        "X-PrivateKey": api_key,
    }
    payload = {
        "variety": "NORMAL",
        "tradingsymbol": symbol,
        "symboltoken": str(token),
        "transactiontype": transaction_type,
        "exchange": exchange,
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": "0",
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(qty),
    }
    last_err = "Order Rejected"
    for _ in range(3):
        try:
            res = requests.post(url, json=payload, headers=headers, timeout=10).json()
            if res.get("status"):
                return True, res.get("data", {}).get("orderid", "Placed")
            last_err = res.get("message", "Order Rejected")
        except Exception as e:
            last_err = str(e)
        time.sleep(0.4)
    return False, last_err


@st.cache_data(ttl=5)
def fetch_angel_live_ltp(jwt_token, api_key, exchange, tokens_tuple):
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
        "X-PrivateKey": api_key,
    }
    valid_tokens = [str(t) for t in tokens_tuple if str(t) and str(t) != "0"]
    if not valid_tokens:
        return {}
    try:
        res = requests.post(
            url,
            json={"mode": "LTP", "exchangeTokens": {exchange: valid_tokens}},
            headers=headers,
            timeout=5,
        ).json()
        if res.get("status"):
            return {str(item["symbolToken"]): float(item["ltp"]) for item in res["data"]["fetched"]}
    except Exception:
        pass
    return {}


@st.cache_data(ttl=8)
def _fetch_angel_live_oc_raw(jwt_token, api_key, selected_idx):
    meta = get_index_meta(selected_idx)
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00",
        "X-PrivateKey": api_key,
    }
    quote_url = "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/"

    try:
        spot_res = requests.post(
            quote_url,
            json={"mode": "LTP", "exchangeTokens": {meta["exch"]: [meta["token"]]}},
            headers=headers,
            timeout=5,
        ).json()
        if not spot_res.get("status"):
            return None, None, "Unknown", 1, {}, "Spot Quote Error"
        spot_price = float(spot_res["data"]["fetched"][0]["ltp"])
    except Exception as e:
        return None, None, "Unknown", 1, {}, str(e)

    step = meta["step"]
    atm_strike = round(spot_price / step) * step
    exp_str, trading_dte, _ = get_live_expiry_info(selected_idx)
    if exp_str == "Unknown":
        return None, spot_price, "Unknown", 1, {}, "Expiry Not Found"

    try:
        master_df = get_scrip_master_df()
        opt_df = master_df[
            (master_df["name"] == meta["name"])
            & (master_df["exch_seg"] == meta["opt_exch"])
            & (master_df["instrumenttype"] == "OPTIDX")
        ].copy()
        opt_df["expiry_dt"] = pd.to_datetime(opt_df["expiry"], format="%d%b%Y", errors="coerce")
        opt_df = opt_df.dropna(subset=["expiry_dt"])
        target_exp_dt = pd.to_datetime(exp_str, format="%d-%b-%Y")
        exp_df = opt_df[opt_df["expiry_dt"] == target_exp_dt].copy()
        exp_df["clean_strike"] = (exp_df["strike"].astype(float) / 100.0).astype(int)
        exp_df["opt_type"] = exp_df["symbol"].str[-2:]

        span = meta["hedge"] + (8 * step)
        min_s = atm_strike - span
        max_s = atm_strike + span
        target_df = exp_df[(exp_df["clean_strike"] >= min_s) & (exp_df["clean_strike"] <= max_s)].copy()
        tokens = target_df["token"].astype(str).tolist()
        if not tokens:
            return None, spot_price, exp_str, trading_dte, {}, "Tokens Not Found"

        contract_map = {}
        for _, r in target_df.iterrows():
            contract_map[(int(r["clean_strike"]), r["opt_type"])] = {
                "symbol": r["symbol"],
                "token": str(r["token"]),
                "exch": meta["opt_exch"],
            }

        fetched_map = {}
        for i in range(0, len(tokens), 50):
            batch = tokens[i : i + 50]
            try:
                q_res = requests.post(
                    quote_url,
                    json={"mode": "FULL", "exchangeTokens": {meta["opt_exch"]: batch}},
                    headers=headers,
                    timeout=5,
                ).json()
                if q_res.get("status") and q_res.get("data", {}).get("fetched"):
                    for item in q_res["data"]["fetched"]:
                        fetched_map[str(item["symbolToken"])] = item
            except Exception:
                pass

        if not fetched_map:
            return None, spot_price, exp_str, trading_dte, {}, "Quotes Empty"

        t_years = get_exact_t_years(exp_str)
        rows = []
        for st_val in sorted(target_df["clean_strike"].unique()):
            ce_tok = contract_map.get((st_val, "CE"), {}).get("token", "")
            pe_tok = contract_map.get((st_val, "PE"), {}).get("token", "")
            ce_q = fetched_map.get(ce_tok, {})
            pe_q = fetched_map.get(pe_tok, {})

            ce_ltp = float(ce_q.get("ltp", 0.0) or 0.0)
            pe_ltp = float(pe_q.get("ltp", 0.0) or 0.0)
            ce_oi = int(float(ce_q.get("opnInterest", 0) or 0))
            pe_oi = int(float(pe_q.get("opnInterest", 0) or 0))
            ce_vol = int(float(ce_q.get("tradeVolume", 0) or 0))
            pe_vol = int(float(pe_q.get("tradeVolume", 0) or 0))
            api_c_chg = int(float(ce_q.get("netChangeOpenInterest", 0) or 0))
            api_p_chg = int(float(pe_q.get("netChangeOpenInterest", 0) or 0))

            ce_iv_est = _implied_volatility(ce_ltp, spot_price, st_val, t_years, "CE")
            pe_iv_est = _implied_volatility(pe_ltp, spot_price, st_val, t_years, "PE")
            ce_g = _bs_greeks(spot_price, st_val, t_years, ce_iv_est, "CE")
            pe_g = _bs_greeks(spot_price, st_val, t_years, pe_iv_est, "PE")

            rows.append(
                {
                    "CE_TOKEN": ce_tok,
                    "PE_TOKEN": pe_tok,
                    "CALL VOL": ce_vol,
                    "CALL OI": ce_oi,
                    "API_CALL_CHG": api_c_chg,
                    "CALL IV": ce_g["iv"],
                    "CALL DELTA": ce_g["delta"],
                    "CALL THETA": ce_g["theta"],
                    "CALL GAMMA": ce_g["gamma"],
                    "CALL LTP": ce_ltp,
                    "STRIKE": st_val,
                    "PUT LTP": pe_ltp,
                    "PUT GAMMA": pe_g["gamma"],
                    "PUT THETA": pe_g["theta"],
                    "PUT DELTA": pe_g["delta"],
                    "PUT IV": pe_g["iv"],
                    "PUT OI": pe_oi,
                    "API_PUT_CHG": api_p_chg,
                    "PUT VOL": pe_vol,
                    "DTE": max(0.2, float(trading_dte)),
                }
            )
        return pd.DataFrame(rows), spot_price, exp_str, trading_dte, contract_map, None
    except Exception as e:
        return None, spot_price, exp_str, trading_dte, {}, str(e)


def fetch_angel_live_oc(jwt_token, api_key, selected_idx):
    raw_df, spot_price, exp_str, trading_dte, contract_map, err = _fetch_angel_live_oc_raw(
        jwt_token, api_key, selected_idx
    )
    if err or raw_df is None or raw_df.empty:
        return None, spot_price, exp_str, trading_dte, contract_map, False, "N/A", err

    df_acc, oi_valid, oi_source = compute_accurate_chg_oi(selected_idx, raw_df)
    display_cols = [
        "CALL VOL",
        "CALL CHG OI",
        "CALL OI",
        "CALL IV",
        "CALL DELTA",
        "CALL THETA",
        "CALL GAMMA",
        "CALL LTP",
        "STRIKE",
        "PUT LTP",
        "PUT GAMMA",
        "PUT THETA",
        "PUT DELTA",
        "PUT IV",
        "PUT OI",
        "PUT CHG OI",
        "PUT VOL",
        "DTE",
    ]
    df_final = df_acc[display_cols].copy()
    return df_final, spot_price, exp_str, trading_dte, contract_map, oi_valid, oi_source, None


# ==========================================
# 4. स्ट्रैटेजी चयन, 10:00 AM ऑटो-एंट्री व एग्जिट
# ==========================================
def calculate_option_charges(sell_turnover, buy_turnover):
    total_turnover = sell_turnover + buy_turnover
    brokerage = 20.0 * 4
    stt = sell_turnover * 0.0015
    exch = total_turnover * 0.0003553
    sebi = total_turnover * 0.000001
    stamp = buy_turnover * 0.00003
    gst = (brokerage + exch + sebi) * 0.18
    return round(brokerage + stt + exch + sebi + stamp + gst, 2)


def get_strategy_recommendation(selected_idx, spot_price, full_df, oi_valid=True):
    meta = get_index_meta(selected_idx)
    step = meta["step"]
    hedge_pts = meta["hedge"]
    atm = round(spot_price / step) * step

    if not oi_valid or full_df is None or full_df.empty:
        return "WAIT", "⏳ OI डेटा की प्रतीक्षा", "कल का क्लोज़िंग OI उपलब्ध नहीं — ट्रेड होल्ड पर है।", []

    up_3 = full_df[(full_df["STRIKE"] > atm) & (full_df["STRIKE"] <= atm + 3 * step)]
    dn_3 = full_df[(full_df["STRIKE"] < atm) & (full_df["STRIKE"] >= atm - 3 * step)]
    call_chg_3 = int(up_3["CALL CHG OI"].sum()) if not up_3.empty else 0
    put_chg_3 = int(dn_3["PUT CHG OI"].sum()) if not dn_3.empty else 0

    def _ltp(k_val, col):
        row = full_df[full_df["STRIKE"] == k_val]
        return float(row[col].iloc[0]) if not row.empty else 0.0

    if put_chg_3 > 0 and put_chg_3 >= call_chg_3 * 1.25:
        buy_k = atm - hedge_pts
        legs = [
            {"action": "BUY", "strike": buy_k, "side": "PE", "entry_ltp": _ltp(buy_k, "PUT LTP")},
            {"action": "SELL", "strike": atm, "side": "PE", "entry_ltp": _ltp(atm, "PUT LTP")},
        ]
        return "Bull Put Spread", f"Sell {atm} PE / Buy {buy_k} PE", f"Put Chg OI ({put_chg_3:+,}) मज़बूत — Bull Put Spread", legs

    if call_chg_3 > 0 and call_chg_3 >= put_chg_3 * 1.25:
        buy_k = atm + hedge_pts
        legs = [
            {"action": "BUY", "strike": buy_k, "side": "CE", "entry_ltp": _ltp(buy_k, "CALL LTP")},
            {"action": "SELL", "strike": atm, "side": "CE", "entry_ltp": _ltp(atm, "CALL LTP")},
        ]
        return "Bear Call Spread", f"Sell {atm} CE / Buy {buy_k} CE", f"Call Chg OI ({call_chg_3:+,}) भारी — Bear Call Spread", legs

    if call_chg_3 > 0 or put_chg_3 > 0:
        b_ce = atm + hedge_pts
        b_pe = atm - hedge_pts
        legs = [
            {"action": "BUY", "strike": b_ce, "side": "CE", "entry_ltp": _ltp(b_ce, "CALL LTP")},
            {"action": "BUY", "strike": b_pe, "side": "PE", "entry_ltp": _ltp(b_pe, "PUT LTP")},
            {"action": "SELL", "strike": atm, "side": "CE", "entry_ltp": _ltp(atm, "CALL LTP")},
            {"action": "SELL", "strike": atm, "side": "PE", "entry_ltp": _ltp(atm, "PUT LTP")},
        ]
        return "Hedged Short Straddle", f"Straddle {atm} CE+PE / Hedge ±{hedge_pts}", f"रेंज/साइडवेज संकेत — Hedged Short Straddle", legs

    return "WAIT", "⏳ WAIT", "स्पष्ट OI सिग्नल नहीं मिला", []


def execute_main_entry(mode_str, selected_idx, strat_name, summary, legs, lots, lot_qty, contract_map, trigger_src):
    ist = pytz.timezone("Asia/Kolkata")
    now_str = datetime.now(ist).strftime("%I:%M:%S %p")
    total_qty = lots * lot_qty
    exch = "BFO" if selected_idx == "Sensex" else "NFO"

    if not legs or any(float(x.get("entry_ltp", 0)) <= 0 for x in legs):
        return False, f"{selected_idx}: किसी लेग का भाव 0 है — एंट्री रोकी गई।"

    enriched = []
    for lg in legs:
        ci = contract_map.get((int(lg["strike"]), lg["side"]), {"symbol": "", "token": "0", "exch": exch})
        enriched.append(
            {
                "action": lg["action"],
                "strike": int(lg["strike"]),
                "side": lg["side"],
                "entry_ltp": float(lg["entry_ltp"]),
                "last_valid_ltp": float(lg["entry_ltp"]),
                "symbol": ci["symbol"],
                "token": ci["token"],
                "exch": ci["exch"],
            }
        )
    ordered = [x for x in enriched if x["action"] == "BUY"] + [x for x in enriched if x["action"] == "SELL"]

    if "LIVE" in mode_str.upper():
        jwt, akey = ensure_angel_session()
        if not jwt or not akey:
            return False, "Angel One सेशन सक्रिय नहीं है!"
        placed_done = []
        for lg in ordered:
            if not lg["token"] or lg["token"] == "0":
                return False, f"टोकन नहीं मिला ({lg['strike']} {lg['side']})"
            ok_o, err_o = place_angel_order(jwt, akey, lg["exch"], lg["symbol"], lg["token"], lg["action"], total_qty)
            if not ok_o:
                for prev in reversed(placed_done):
                    rev_side = "SELL" if prev["action"] == "BUY" else "BUY"
                    place_angel_order(jwt, akey, prev["exch"], prev["symbol"], prev["token"], rev_side, total_qty)
                return False, f"ऑर्डर विफल ({lg['symbol']}): {err_o}"
            placed_done.append(lg)
            time.sleep(0.35)

    active_map = st.session_state.get("active_multi_trades", load_active_multi_map())
    active_map[selected_idx] = {
        "mode": mode_str,
        "index": selected_idx,
        "strategy": strat_name,
        "summary": summary,
        "lots": lots,
        "qty": total_qty,
        "exch": exch,
        "legs": ordered,
        "entry_time": now_str,
        "sl_rs": -1700.0 * lots,
        "tsl_active": False,
        "last_mtm": 0.0,
    }
    st.session_state["active_multi_trades"] = active_map
    save_active_multi_map(active_map)

    legs_txt = "\n".join([f"• <b>{l['action']}</b> {l['strike']} {l['side']} @ ₹{l['entry_ltp']}" for l in ordered])
    send_telegram_message(
        f"🚀 <b>MAIN ALGO ENTRY ({trigger_src})</b>\n"
        f"📊 <b>Index:</b> {selected_idx}\n"
        f"📌 <b>Strategy:</b> {strat_name} ({summary})\n{legs_txt}\n"
        f"📦 <b>Qty:</b> {lots} Lot ({total_qty}) | ⏰ {now_str}"
    )
    return True, f"{selected_idx}: {strat_name} एंट्री सफल!"


def close_main_index_trade(idx_name, curr_px_map, reason):
    active_map = st.session_state.get("active_multi_trades", load_active_multi_map())
    tr = active_map.get(idx_name)
    if not tr:
        return False

    ist = pytz.timezone("Asia/Kolkata")
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    exit_time = datetime.now(ist).strftime("%I:%M:%S %p")
    legs = tr["legs"]

    if "LIVE" in str(tr.get("mode", "")).upper():
        jwt, akey = ensure_angel_session()
        if not jwt or not akey:
            send_telegram_message(f"🚨 <b>EXIT FAILED ({idx_name}):</b> Angel One लॉगिन नहीं है!")
            return False
        for lg in legs:
            if lg["action"] == "SELL":
                ok_x, err_x = place_angel_order(jwt, akey, lg["exch"], lg["symbol"], lg["token"], "BUY", tr["qty"])
                if not ok_x:
                    send_telegram_message(f"🚨 <b>EXIT FAILED ({lg['symbol']}):</b> {err_x}")
                    return False
                time.sleep(0.35)
        for lg in legs:
            if lg["action"] == "BUY":
                place_angel_order(jwt, akey, lg["exch"], lg["symbol"], lg["token"], "SELL", tr["qty"])
                time.sleep(0.35)

    pnl_pts = 0.0
    for i, lg in enumerate(legs):
        px = float(curr_px_map.get(i, lg.get("last_valid_ltp", lg["entry_ltp"])))
        pnl_pts += (lg["entry_ltp"] - px) if lg["action"] == "SELL" else (px - lg["entry_ltp"])
    pnl_rs = round(pnl_pts * float(tr["qty"]), 2)

    mode_short = "Paper" if "PAPER" in str(tr.get("mode", "")).upper() else "LIVE"
    if mode_short == "Paper" and "paper_capital" in st.session_state:
        st.session_state["paper_capital"] += pnl_rs

    hist = st.session_state.get("paper_trade_history", load_paper_history())
    hist.insert(
        0,
        {
            "तारीख": today_str,
            "मोड": mode_short,
            "इंडेक्स": idx_name,
            "रणनीति": tr.get("strategy", ""),
            "स्प्रेड": tr.get("summary", ""),
            "लॉट": tr.get("lots", 1),
            "एंट्री समय": tr.get("entry_time", ""),
            "एग्जिट समय": exit_time,
            "कारण": reason,
            "लाभ / हानि (₹)": pnl_rs,
        },
    )
    st.session_state["paper_trade_history"] = hist
    save_paper_history(hist)

    del active_map[idx_name]
    st.session_state["active_multi_trades"] = active_map
    save_active_multi_map(active_map)

    send_telegram_message(
        f"🏁 <b>MAIN TRADE CLOSED ({mode_short} - {idx_name})</b>\n"
        f"📌 {tr.get('summary', '')} | 📝 {reason}\n💵 <b>Final P&L:</b> <b>₹{pnl_rs:+,.0f}</b>"
    )
    return True


def _calc_any_position_mtm(tr, oc_df):
    if oc_df is None or oc_df.empty:
        return round(float(tr.get("last_mtm", 0.0)), 2)
    pts = 0.0
    for lg in tr.get("legs", []):
        if lg.get("closed"):
            pts += float(lg.get("realized_pts", 0.0))
            continue
        row = oc_df[oc_df["STRIKE"] == int(lg["strike"])]
        col = "CALL LTP" if lg["side"] == "CE" else "PUT LTP"
        px = (
            float(row[col].iloc[0])
            if (not row.empty and float(row[col].iloc[0]) > 0)
            else float(lg.get("last_valid_ltp", lg["entry_ltp"]))
        )
        lg["last_valid_ltp"] = px
        pts += (lg["entry_ltp"] - px) if lg["action"] == "SELL" else (px - lg["entry_ltp"])
    mtm = round(pts * float(tr.get("qty", 0)), 2)
    tr["last_mtm"] = mtm
    return mtm


# ==========================================
# 5. सेशन स्टेट व ऊपरी इंडेक्स बैनर
# ==========================================
if "paper_trade_history" not in st.session_state:
    st.session_state["paper_trade_history"] = load_paper_history()
if "paper_capital" not in st.session_state:
    p_sum = sum(
        float(x.get("लाभ / हानि (₹)", 0))
        for x in st.session_state["paper_trade_history"]
        if x.get("मोड") == "Paper"
    )
    st.session_state["paper_capital"] = 200000.0 + p_sum
if "active_multi_trades" not in st.session_state:
    st.session_state["active_multi_trades"] = load_active_multi_map()

ist_tz = pytz.timezone("Asia/Kolkata")
now_ist_top = datetime.now(ist_tz)
snap_top = get_top_tickers_snapshot()

t_cols = st.columns(4)
for i, label in enumerate(["Nifty 50", "Sensex", "Bank Nifty", "India VIX"]):
    info_t = snap_top.get(label, {"ltp": 0.0, "chg": 0.0, "pct": 0.0})
    is_pos = float(info_t.get("chg", 0.0)) >= 0
    cls_d = "top-idx-up" if is_pos else "top-idx-dn"
    sgn = "+" if is_pos else ""
    with t_cols[i]:
        st.markdown(
            f"""
            <div class="top-idx-card">
                <div class="top-idx-name">{label}</div>
                <div class="top-idx-ltp">{float(info_t.get('ltp', 0.0)):,.2f}</div>
                <div class="{cls_d}">{sgn}{float(info_t.get('chg', 0.0)):,.2f} ({sgn}{float(info_t.get('pct', 0.0)):.2f}%)</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

st.caption(f"⚡ **Option Seller Algo Terminal** | 🕒 `{now_ist_top.strftime('%d-%b-%Y %I:%M:%S %p IST')}` | 🛡️ SL: `₹1,700/Lot` | Hedge: Nifty `500`, Bank Nifty `1000`, Sensex `1500`")

tab1, tab2, tab3 = st.tabs(
    [
        "📊 Live Option Chain & 10:00 AM Auto Algo",
        "📈 Position & Greeks Monitor",
        "🧪 Backtest (Original Saved OC + Historical)",
    ]
)


# ==========================================
# TAB 1: Live Option Chain & 10:00 AM Auto Algo
# ==========================================
with tab1:
    cfg_main = load_algo_settings()
    top1, top2, top3, top4 = st.columns([2, 1.2, 1.2, 1])
    with top1:
        main_mode = st.radio(
            "ट्रेडिंग मोड:",
            ["🟡 PAPER TRADING (वर्चुअल प्रैक्टिस)", "🔴 LIVE ALGO (असली पैसा)"],
            index=(1 if cfg_main.get("main_live_mode") else 0),
            horizontal=True,
            key="main_mode_radio",
        )
    with top2:
        selected_idx = st.selectbox("इंडेक्स चुनें:", ["Nifty 50", "Sensex", "Bank Nifty"], key="main_idx_sel")
    with top3:
        main_lots = st.number_input("लॉट (Lots):", min_value=1, max_value=25, value=int(cfg_main.get("main_lots", 1)), key="main_lots_num")
    with top4:
        auto_10am = st.toggle("⏰ 10:00 AM Auto", value=bool(cfg_main.get("main_auto_10am")), key="main_10am_tgl")

    main_target_idxs = st.multiselect(
        "10:00 AM ऑटो-ट्रेड इंडेक्स (Nifty + Sensex साथ चल सकते हैं):",
        ["Nifty 50", "Sensex", "Bank Nifty"],
        default=cfg_main.get("main_indices", ["Nifty 50", "Sensex"]),
        key="main_10am_idxs",
    )

    is_live_mode = "LIVE ALGO" in main_mode
    live_confirmed = True
    if is_live_mode:
        st.warning("⚠️ LIVE ALGO मोड चालू है! असली ऑर्डर लगेंगे।")
        live_confirmed = st.checkbox("✅ मैं पुष्टि करता हूँ कि असली पैसे से ऑर्डर लगें", key="main_live_ok")

    update_algo_settings_batch(
        {
            "main_auto_10am": bool(auto_10am),
            "main_live_mode": bool(is_live_mode),
            "main_indices": main_target_idxs or ["Nifty 50", "Sensex"],
            "main_lots": int(main_lots),
        }
    )

    @st.fragment(run_every=10)
    def render_main_live_fragment(cur_idx, mode_label, lots_val, auto_flag, target_list, live_ok):
        now_ist = datetime.now(ist_tz)
        today_str = now_ist.strftime("%Y-%m-%d")
        curr_mins = now_ist.hour * 60 + now_ist.minute
        exit_cmd = poll_telegram_exit_only()

        jwt, akey = ensure_angel_session()
        if not jwt or not akey:
            st.error("🚨 Angel One SmartAPI सेशन उपलब्ध नहीं है — कृपया API Secrets जांचें।")
            return

        active_map = st.session_state.get("active_multi_trades", {})
        chains_cache = {}

        for idx_k in list(active_map.keys()):
            tr = active_map[idx_k]
            oc_k, _, _, _, _, _, _, _ = fetch_angel_live_oc(jwt, akey, idx_k)
            if oc_k is not None:
                chains_cache[idx_k] = oc_k
            curr_px_map = {}
            for i_l, lg in enumerate(tr.get("legs", [])):
                col = "CALL LTP" if lg["side"] == "CE" else "PUT LTP"
                if oc_k is not None and not oc_k.empty:
                    r_m = oc_k[oc_k["STRIKE"] == int(lg["strike"])]
                    if not r_m.empty and float(r_m[col].iloc[0]) > 0:
                        lg["last_valid_ltp"] = float(r_m[col].iloc[0])
                curr_px_map[i_l] = float(lg.get("last_valid_ltp", lg["entry_ltp"]))

            mtm_val = _calc_any_position_mtm(tr, oc_k)
            lots_tr = int(tr.get("lots", 1))
            if not tr.get("tsl_active", False) and mtm_val >= (1200.0 * lots_tr):
                tr["tsl_active"] = True
                tr["sl_rs"] = 200.0 * lots_tr
                save_active_multi_map(active_map)
                send_telegram_message(f"🔒 <b>TSL LOCKED ({idx_k}):</b> SL अब +₹{int(200 * lots_tr):,} पर है।")

            cur_sl = float(tr.get("sl_rs", -1700.0 * lots_tr))
            if exit_cmd:
                if close_main_index_trade(idx_k, curr_px_map, "Telegram /exit 📲"):
                    st.rerun()
            elif mtm_val <= cur_sl:
                if close_main_index_trade(idx_k, curr_px_map, "TSL Hit 🔒" if tr.get("tsl_active") else "SL Hit (-₹1,700/Lot) 🛑"):
                    st.rerun()
            elif curr_mins >= (15 * 60 + 15):
                if close_main_index_trade(idx_k, curr_px_map, "3:15 PM Square-Off ⏰"):
                    st.rerun()

        if active_map:
            st.markdown(f"### 🔥 चालू Main पोजीशनें ({len(active_map)} सक्रिय)")
            for idx_k in list(active_map.keys()):
                tr = active_map[idx_k]
                oc_k = chains_cache.get(idx_k)
                mtm_val = _calc_any_position_mtm(tr, oc_k)
                curr_px_map = {
                    i_l: float(lg.get("last_valid_ltp", lg["entry_ltp"]))
                    for i_l, lg in enumerate(tr.get("legs", []))
                }
                with st.container(border=True):
                    c1, c2, c3, c4 = st.columns([2.2, 1.1, 1.1, 1])
                    c1.markdown(f"**📌 {idx_k}** — {tr.get('strategy', '')} (`{tr.get('summary', '')}`)")
                    c2.metric("लाइव MTM", f"₹{mtm_val:+,.0f}", f"Qty: {tr.get('qty', 0)}")
                    c3.metric("सक्रिय SL", f"₹{float(tr.get('sl_rs', -1700)):+,.0f}", "TSL 🔒" if tr.get("tsl_active") else "Initial SL")
                    if c4.button(f"🚨 {idx_k} बंद करें", key=f"main_close_{idx_k}", type="primary", use_container_width=True):
                        if close_main_index_trade(idx_k, curr_px_map, "Manual Square-Off"):
                            st.rerun()

        if auto_flag and now_ist.hour == 10 and now_ist.minute <= 5:
            if not is_live_mode or live_ok:
                for t_idx in target_list:
                    if t_idx in active_map or is_strategy_traded_today("main", today_str, t_idx):
                        continue
                    t_meta = get_index_meta(t_idx)
                    t_oc, t_sp, _, _, t_cmap, t_oi_ok, _, _ = fetch_angel_live_oc(jwt, akey, t_idx)
                    if t_oc is not None and t_oi_ok and t_sp:
                        sn, ss, s_reason, s_legs = get_strategy_recommendation(t_idx, t_sp, t_oc, oi_valid=True)
                        if s_legs:
                            ok_auto, _ = execute_main_entry(
                                mode_label,
                                t_idx,
                                sn,
                                ss,
                                s_legs,
                                lots_val,
                                t_meta["lot"],
                                t_cmap,
                                "10:00 AM Auto",
                            )
                            if ok_auto:
                                mark_strategy_traded_today("main", today_str, t_idx)
                                st.rerun()

        meta = get_index_meta(cur_idx)
        oc_df, spot_p, exp_s, dte_v, cmap_v, oi_ok, oi_src, oc_err = fetch_angel_live_oc(jwt, akey, cur_idx)
        if oc_df is None or oc_df.empty or not spot_p:
            st.error(f"⚠️ {cur_idx} का लाइव ऑप्शन चेन डेटा उपलब्ध नहीं है ({oc_err or 'Empty'})")
            return

        atm_v = round(spot_p / meta["step"]) * meta["step"]
        sn, ss, s_reason, s_legs = get_strategy_recommendation(cur_idx, spot_p, oc_df, oi_valid=oi_ok)
        bg_hero = "linear-gradient(90deg, #064e3b, #022c22)" if "Bull" in sn else ("linear-gradient(90deg, #7f1d1d, #450a0a)" if "Bear" in sn else "linear-gradient(90deg, #1e293b, #0f172a)")
        bd_hero = "#10b981" if "Bull" in sn else ("#ef4444" if "Bear" in sn else "#38bdf8")

        st.markdown(
            f"""
            <div class="hero-signal-card" style="background:{bg_hero}; border-left-color:{bd_hero};">
                <div style="font-size:13px; color:#cbd5e1;">📍 <b>{cur_idx}</b> Spot: <b>{spot_p:,.2f}</b> | ATM: <b>{atm_v}</b> | Expiry: <b>{exp_s} ({dte_v} DTE)</b> | Lot: <b>{meta['lot']}</b> | OI Source: <b>{oi_src}</b></div>
                <div style="font-size:17px; font-weight:700; color:#fde047; margin:6px 0;">{sn} — {s_reason}</div>
                <div style="font-size:13px; color:#e2e8f0;">सेटअप: <b>{ss}</b> | हेज दूरी: <b>{meta['hedge']} pts</b> | पेपर बैलेंस: <b>₹{st.session_state['paper_capital']:,.0f}</b></div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if cur_idx not in active_map and s_legs:
            if st.button(
                f"🚀 {cur_idx} में {sn} ({ss}) चलाएं ({lots_val} Lot)",
                disabled=(is_live_mode and not live_ok),
                use_container_width=True,
            ):
                ok_m, msg_m = execute_main_entry(
                    mode_label, cur_idx, sn, ss, s_legs, lots_val, meta["lot"], cmap_v, "Manual"
                )
                if ok_m:
                    mark_strategy_traded_today("main", today_str, cur_idx)
                    st.rerun()
                else:
                    st.error(msg_m)

        view_oc = oc_df.copy()
        view_oc["STRIKE"] = view_oc["STRIKE"].apply(lambda x: f"⭐ {int(x)} (ATM)" if int(x) == atm_v else f"{int(x)}")
        st.dataframe(view_oc, use_container_width=True, hide_index=True, height=440)

        if st.session_state["paper_trade_history"]:
            st.markdown("### 📜 ट्रेड हिस्ट्री")
            st.dataframe(pd.DataFrame(st.session_state["paper_trade_history"]), use_container_width=True, hide_index=True)

    render_main_live_fragment(selected_idx, main_mode, main_lots, auto_10am, main_target_idxs, live_confirmed)


# ==========================================
# TAB 2: सभी सक्रिय पोजीशनों का एक साथ अवलोकन
# ==========================================
with tab2:
    st.subheader("📈 सभी सक्रिय पोजीशनें (Main + 16 EMA + Pro)")
    jwt2, akey2 = ensure_angel_session()
    m_all = _load_json_dict_gh(ACTIVE_MULTI_FILE)
    e_all = _load_json_dict_gh(EMA_MULTI_ACTIVE_FILE)
    p_all = _load_json_dict_gh(PRO_MULTI_ACTIVE_FILE)

    combined_cards = []
    for grp_name, d_map in [("Main 10AM", m_all), ("16 EMA + OI", e_all), ("Pro Algo", p_all)]:
        for k_idx, tr_obj in d_map.items():
            idx_real = tr_obj.get("index", k_idx)
            oc_obj = None
            if jwt2 and akey2:
                oc_obj, _, _, _, _, _, _, _ = fetch_angel_live_oc(jwt2, akey2, idx_real)
            mtm_c = _calc_any_position_mtm(tr_obj, oc_obj)
            combined_cards.append(
                {
                    "engine": grp_name,
                    "index": idx_real,
                    "strategy": tr_obj.get("summary", tr_obj.get("strategy", "")),
                    "lots": tr_obj.get("lots", 1),
                    "qty": tr_obj.get("qty", 0),
                    "mtm": mtm_c,
                    "sl": tr_obj.get("sl_rs", -1700),
                }
            )

    if not combined_cards:
        st.info("अभी कोई सक्रिय पोजीशन नहीं चल रही है।")
    else:
        total_all_mtm = sum(float(x["mtm"]) for x in combined_cards)
        st.metric("कुल संयुक्त लाइव MTM", f"₹{total_all_mtm:+,.0f}", f"{len(combined_cards)} खुली पोजीशन")
        for item in combined_cards:
            with st.container(border=True):
                c1, c2, c3 = st.columns([2.4, 1.2, 1.2])
                c1.markdown(f"**{item['engine']} • {item['index']}** — {item['strategy']}")
                c2.metric("MTM", f"₹{item['mtm']:+,.0f}", f"Qty: {item['qty']}")
                c3.metric("SL सीमा", f"₹{float(item['sl']):+,.0f}", f"Lots: {item['lots']}")


# ==========================================
# TAB 3: Backtest (ओरिजिनल सेव्ड GitHub OC डेटा + ऐतिहासिक कैंडल)
# ==========================================
with tab3:
    st.subheader("🧪 बैकटेस्ट — ओरिजिनल सेव्ड GitHub डेटा व ऐतिहासिक स्पॉट डेटा")
    bt_source = st.radio(
        "बैकटेस्ट डेटा स्रोत चुनें:",
        [
            "📁 ओरिजिनल सेव्ड GitHub डेटा (oc_logs / recorded_oc_history.csv)",
            "🕯️ ऐतिहासिक 1-मिनट स्पॉट कैंडल डेटा (Black-Scholes)",
        ],
        horizontal=True,
        key="bt_src_radio",
    )

    b1, b2, b3 = st.columns([1.4, 1.2, 1.2])
    with b1:
        bt_idx = st.selectbox("इंडेक्स:", ["Nifty 50", "Sensex", "Bank Nifty"], key="t3_idx")
    with b2:
        bt_lots = st.number_input("लॉट (Lots):", min_value=1, max_value=25, value=1, key="t3_lots")
    with b3:
        bt_days = st.selectbox("कैंडल बैकटेस्ट अवधि:", [30, 60, 90], index=0, key="t3_days")

    if "ओरिजिनल सेव्ड GitHub डेटा" in bt_source:
        saved_files = gh_list_oc_logs()
        st.caption(f"उपलब्ध दैनिक लॉग फ़ाइलें: `{', '.join(saved_files) if saved_files else 'कोई दैनिक CSV नहीं'}` | मुख्य लॉग: `{OC_RECORD_FILE}`")

        if st.button("🚀 ओरिजिनल सेव्ड डेटा पर बैकटेस्ट चलाएं", use_container_width=True):
            with st.spinner("GitHub से सेव्ड ऑप्शन चेन डेटा लोड हो रहा है..."):
                rec_df = load_recorded_oc_dataframe(selected_idx=bt_idx)
            if rec_df.empty:
                st.warning(f"{bt_idx} के लिए सेव्ड ऑप्शन चेन डेटा नहीं मिला।")
            else:
                meta = get_index_meta(bt_idx)
                step = meta["step"]
                hedge_pts = meta["hedge"]
                lot_q = meta["lot"]
                total_q = lot_q * bt_lots

                needed_cols = {"STRIKE", "CALL CHG OI", "PUT CHG OI", "CALL LTP", "PUT LTP"}
                if not needed_cols.issubset(set(rec_df.columns)):
                    st.error("सेव्ड CSV में ज़रूरी कॉलम (STRIKE, CALL CHG OI, PUT CHG OI, CALL LTP, PUT LTP) नहीं मिले।")
                else:
                    rec_df["STRIKE"] = pd.to_numeric(rec_df["STRIKE"], errors="coerce")
                    rec_df["CALL CHG OI"] = pd.to_numeric(rec_df["CALL CHG OI"], errors="coerce").fillna(0)
                    rec_df["PUT CHG OI"] = pd.to_numeric(rec_df["PUT CHG OI"], errors="coerce").fillna(0)
                    rec_df["CALL LTP"] = pd.to_numeric(rec_df["CALL LTP"], errors="coerce").fillna(0.0)
                    rec_df["PUT LTP"] = pd.to_numeric(rec_df["PUT LTP"], errors="coerce").fillna(0.0)
                    rec_df = rec_df.dropna(subset=["STRIKE"])
                    rec_df["STRIKE"] = rec_df["STRIKE"].astype(int)

                    if "Date" not in rec_df.columns:
                        rec_df["Date"] = datetime.now(ist_tz).strftime("%Y-%m-%d")
                    if "Time" not in rec_df.columns:
                        rec_df["Time"] = "10:00"

                    rec_trades = []
                    for dt_val, d_group in rec_df.groupby("Date"):
                        d_group = d_group.sort_values("Time")
                        times_list = sorted(d_group["Time"].astype(str).unique())
                        if not times_list:
                            continue
                        ent_t = next((t for t in times_list if t >= "09:45"), times_list[0])
                        snap_ent = d_group[d_group["Time"].astype(str) == ent_t]
                        if snap_ent.empty:
                            continue

                        if "Spot" in snap_ent.columns and float(snap_ent["Spot"].dropna().iloc[0] if not snap_ent["Spot"].dropna().empty else 0) > 0:
                            spot_e = float(snap_ent["Spot"].dropna().iloc[0])
                        else:
                            spot_e = float(snap_ent["STRIKE"].median())

                        atm_e = round(spot_e / step) * step
                        sn_e, ss_e, _, legs_e = get_strategy_recommendation(bt_idx, spot_e, snap_ent, oi_valid=True)
                        if not legs_e or any(float(x.get("entry_ltp", 0)) <= 0 for x in legs_e):
                            continue

                        later_times = [t for t in times_list if t > ent_t]
                        exit_t = later_times[-1] if later_times else ent_t
                        snap_ex = d_group[d_group["Time"].astype(str) == exit_t]
                        reason_e = "Session End"
                        pnl_pts = 0.0

                        for lg in legs_e:
                            col_p = "CALL LTP" if lg["side"] == "CE" else "PUT LTP"
                            row_ex = snap_ex[snap_ex["STRIKE"] == int(lg["strike"])]
                            ex_px = float(row_ex[col_p].iloc[0]) if not row_ex.empty else float(lg["entry_ltp"])
                            pnl_pts += (float(lg["entry_ltp"]) - ex_px) if lg["action"] == "SELL" else (ex_px - float(lg["entry_ltp"]))

                        pnl_rs = round(pnl_pts * total_q, 2)
                        sl_cap = -1700.0 * bt_lots
                        if pnl_rs <= sl_cap:
                            pnl_rs = sl_cap
                            reason_e = "SL Cap (-₹1,700/Lot) 🛑"

                        rec_trades.append(
                            {
                                "तारीख": str(dt_val),
                                "एंट्री": ent_t,
                                "एग्जिट": exit_t,
                                "रणनीति": sn_e,
                                "स्प्रेड": ss_e,
                                "कारण": reason_e,
                                "P&L (₹)": pnl_rs,
                            }
                        )

                    if rec_trades:
                        out_rec = pd.DataFrame(rec_trades)
                        out_rec["संचयी P&L (₹)"] = out_rec["P&L (₹)"].cumsum()
                        m1, m2 = st.columns(2)
                        m1.metric("कुल P&L (Original OC)", f"₹{out_rec['P&L (₹)'].sum():+,.0f}", f"{len(out_rec)} दिन")
                        m2.metric("Win Rate", f"{round((len(out_rec[out_rec['P&L (₹)'] > 0]) / len(out_rec)) * 100, 1)}%")
                        st.line_chart(out_rec.set_index("तारीख")["संचयी P&L (₹)"])
                        st.dataframe(out_rec, use_container_width=True, hide_index=True)
                    else:
                        st.info("सेव्ड डेटा में मान्य ट्रेड सेटअप नहीं मिला।")
    else:
        if st.button("🚀 ऐतिहासिक कैंडल बैकटेस्ट चलाएं", use_container_width=True):
            jwt3, akey3 = ensure_angel_session()
            if not jwt3 or not akey3:
                st.error("Angel One लॉगिन आवश्यक है।")
            else:
                meta3 = get_index_meta(bt_idx)
                end_dt3 = datetime.now(ist_tz)
                rem3 = int(bt_days)
                raw_c = []
                hdr3 = {
                    "Authorization": f"Bearer {jwt3}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-UserType": "USER",
                    "X-SourceID": "WEB",
                    "X-ClientLocalIP": "127.0.0.1",
                    "X-ClientPublicIP": "127.0.0.1",
                    "X-MACAddress": "00:00:00:00:00:00",
                    "X-PrivateKey": akey3,
                }
                with st.spinner("ऐतिहासिक कैंडल्स लोड हो रही हैं..."):
                    while rem3 > 0:
                        step_d = min(30, rem3)
                        st_dt3 = end_dt3 - timedelta(days=step_d)
                        r3 = requests.post(
                            "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData",
                            json={
                                "exchange": meta3["exch"],
                                "symboltoken": meta3["token"],
                                "interval": "ONE_MINUTE",
                                "fromdate": st_dt3.strftime("%Y-%m-%d 09:15"),
                                "todate": end_dt3.strftime("%Y-%m-%d 15:30"),
                            },
                            headers=hdr3,
                            timeout=15,
                        ).json()
                        if r3.get("status") and r3.get("data"):
                            raw_c.extend(r3["data"])
                        time.sleep(0.15)
                        end_dt3 = st_dt3 - timedelta(minutes=1)
                        rem3 -= step_d

                if not raw_c:
                    st.error("ऐतिहासिक कैंडल डेटा नहीं मिला।")
                else:
                    cdf = pd.DataFrame(raw_c, columns=["ts", "Open", "High", "Low", "Close", "Vol"])
                    cdf["Date"] = cdf["ts"].astype(str).str.slice(0, 10)
                    cdf["Time"] = cdf["ts"].astype(str).str.slice(11, 16)
                    cdf = cdf.drop_duplicates(subset=["Date", "Time"]).sort_values(["Date", "Time"])
                    dt_objs = [datetime.strptime(x, "%Y-%m-%d").date() for x in sorted(cdf["Date"].unique())]
                    dte_m = build_backtest_dte_map(dt_objs, bt_idx)
                    tot_q3 = meta3["lot"] * bt_lots
                    iv3 = 0.14 if bt_idx == "Nifty 50" else 0.15
                    bt_rows = []

                    for d_s, g_df in cdf.groupby("Date"):
                        bar10 = g_df[g_df["Time"] >= "10:00"]
                        if bar10.empty:
                            continue
                        sp10 = float(bar10["Open"].iloc[0])
                        atm10 = round(sp10 / meta3["step"]) * meta3["step"]
                        dte_d = dte_m.get(datetime.strptime(d_s, "%Y-%m-%d").date(), 1)
                        t_y = max(0.05, dte_d + 0.5) / 365.0
                        c_px = _bs_price(sp10, atm10, t_y, iv3, "CE")
                        p_px = _bs_price(sp10, atm10, t_y, iv3, "PE")
                        h_c = _bs_price(sp10, atm10 + meta3["hedge"], t_y, iv3, "CE")
                        h_p = _bs_price(sp10, atm10 - meta3["hedge"], t_y, iv3, "PE")
                        ent_cr = (c_px + p_px - h_c - h_p) * tot_q3

                        cl_s = float(g_df["Close"].iloc[-1])
                        t_ex = max(0.02, dte_d + 0.1) / 365.0
                        ex_val = (
                            _bs_price(cl_s, atm10, t_ex, iv3, "CE")
                            + _bs_price(cl_s, atm10, t_ex, iv3, "PE")
                            - _bs_price(cl_s, atm10 + meta3["hedge"], t_ex, iv3, "CE")
                            - _bs_price(cl_s, atm10 - meta3["hedge"], t_ex, iv3, "PE")
                        ) * tot_q3
                        pnl_c = round(ent_cr - ex_val, 0)
                        if pnl_c <= -1700.0 * bt_lots:
                            pnl_c = -1700.0 * bt_lots
                        bt_rows.append({"तारीख": d_s, "DTE": f"{dte_d} DTE", "ATM": atm10, "P&L (₹)": pnl_c})

                    if bt_rows:
                        res_b = pd.DataFrame(bt_rows)
                        res_b["कुल पूंजी (₹)"] = res_b["P&L (₹)"].cumsum()
                        st.metric("कुल P&L (Historical Candle)", f"₹{res_b['P&L (₹)'].sum():+,.0f}", f"{len(res_b)} दिन")
                        st.line_chart(res_b.set_index("तारीख")["कुल पूंजी (₹)"])
                        st.dataframe(res_b, use_container_width=True, hide_index=True)
