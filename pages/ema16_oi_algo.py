import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
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

try:
    st.set_page_config(page_title="16 EMA + OI 40-Delta Algo", layout="wide")
except Exception:
    pass

EMA_HISTORY_FILE = "ema16_oi_trades_history.csv"
EMA_ACTIVE_FILE = "ema16_oi_active_trade.json"


# ==========================================
# GitHub Storage Helpers
# ==========================================
def ema_load_history():
    raw_csv, _, status = gh_read_text(EMA_HISTORY_FILE)
    if status == "OK" and raw_csv is not None:
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
    df.to_csv(EMA_HISTORY_FILE, index=False)
    csv_text = df.to_csv(index=False) if not df.empty else "तारीख,मोड,इंडेक्स,टाइमफ्रेम,दिशा,स्प्रेड,लॉट,एंट्री समय,एग्जिट समय,कारण,लाभ / हानि (₹)\n"
    gh_write_text(EMA_HISTORY_FILE, csv_text, "Update 16EMA OI trade history")


def ema_load_active():
    raw_json, _, status = gh_read_text(EMA_ACTIVE_FILE)
    if status == "OK" and raw_json:
        try:
            data = json.loads(raw_json)
            if isinstance(data, dict) and data.get("legs"):
                return data
        except Exception:
            pass
    return None


def ema_save_active(tr_dict):
    gh_write_text(EMA_ACTIVE_FILE, json.dumps(tr_dict if tr_dict else {}, ensure_ascii=False), "Update 16EMA active trade")


# ==========================================
# Telegram Alert & Safe Remote Control
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
        last_off = get_saved_tg_offset()
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        res = requests.get(url, params={"offset": last_off + 1, "timeout": 1}, timeout=3).json()
        if not res.get("ok"):
            return None
        cmd = None
        max_u = last_off
        now_ts = time.time()
        for upd in res.get("result", []):
            uid = int(upd.get("update_id", 0))
            if uid > max_u:
                max_u = uid
            m = upd.get("message", {})
            if now_ts - float(m.get("date", now_ts)) > 90:
                continue
            if str(m.get("chat", {}).get("id", "")) == allowed_id:
                txt = m.get("text", "").strip().upper()
                if txt in ["/EXIT", "EXIT", "STOP", "CLOSE"]:
                    cmd = "EXIT"
                elif txt in ["/STATUS", "STATUS", "MTM", "PNL"]:
                    cmd = "STATUS"
        if max_u > last_off:
            save_tg_offset(max_u)
        return cmd
    except Exception:
        return None


# ==========================================
# Angel One SmartAPI Order & Market Data
# ==========================================
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
        "squareoff": "0", "stoploss": "0", "quantity": str(qty)
    }
    last_err = "Rejected"
    for _ in range(3):
        try:
            res = requests.post(url, json=payload, headers=headers, timeout=10).json()
            if res.get("status"):
                return True, res.get("data", {}).get("orderid", "Placed")
            last_err = res.get("message", "Rejected")
        except Exception as e:
            last_err = str(e)
        time.sleep(0.4)
    return False, last_err


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


@st.cache_data(ttl=45)
def ema_fetch_intraday_candles(jwt, api_key, selected_idx, tf_mins, days_back=4):
    """केवल असली Angel One कैंडल्स लाता है और उन्हें 3-Min / 5-Min + 16 EMA में बदलता है"""
    if not jwt or not api_key:
        return pd.DataFrame()
    meta = get_index_meta(selected_idx)
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)

    headers = {
        "Authorization": f"Bearer {jwt}", "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": api_key.strip()
    }
    hist_url = "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData"

    if days_back <= 60:
        api_interval = "ONE_MINUTE"
        chunk_limit = 30
    else:
        api_interval = "THREE_MINUTE" if tf_mins == 3 else "FIVE_MINUTE"
        chunk_limit = 60 if tf_mins == 3 else 90

    all_candles = []
    remaining = days_back
    end_dt = now_ist

    while remaining > 0:
        step_days = min(chunk_limit, remaining)
        start_dt = end_dt - timedelta(days=step_days)
        payload = {
            "exchange": meta["exch"],
            "symboltoken": meta["token"],
            "interval": api_interval,
            "fromdate": start_dt.strftime("%Y-%m-%d 09:15"),
            "todate": end_dt.strftime("%Y-%m-%d 15:30")
        }
        try:
            res = requests.post(hist_url, json=payload, headers=headers, timeout=15).json()
            if res.get("status") and res.get("data"):
                all_candles.extend(res["data"])
            else:
                break
            time.sleep(0.18)
        except Exception:
            break
        end_dt = start_dt - timedelta(minutes=1)
        remaining -= step_days

    if not all_candles:
        return pd.DataFrame()

    df_raw = pd.DataFrame(all_candles, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
    df_raw["Timestamp"] = pd.to_datetime(df_raw["Timestamp"])
    if df_raw["Timestamp"].dt.tz is None:
        df_raw["Timestamp"] = df_raw["Timestamp"].dt.tz_localize("Asia/Kolkata")
    else:
        df_raw["Timestamp"] = df_raw["Timestamp"].dt.tz_convert("Asia/Kolkata")
    df_raw = df_raw.drop_duplicates(subset=["Timestamp"]).sort_values("Timestamp").set_index("Timestamp")

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
    """असली Angel One Option Chain + सटीक Change in OI लाता है"""
    meta = get_index_meta(selected_idx)
    headers = {
        "Authorization": f"Bearer {jwt}", "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": api_key.strip()
    }
    quote_url = "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/"

    try:
        sp_res = requests.post(quote_url, json={"mode": "LTP", "exchangeTokens": {meta["exch"]: [meta["token"]]}}, headers=headers, timeout=5).json()
        if not sp_res.get("status"):
            return None, None, "Unknown", 1, {}, False, "N/A", "Spot Error"
        spot_price = float(sp_res["data"]["fetched"][0]["ltp"])
    except Exception as e:
        return None, None, "Unknown", 1, {}, False, "N/A", str(e)

    step = meta["step"]
    atm = round(spot_price / step) * step
    exp_str, trading_dte, _ = get_live_expiry_info(selected_idx)
    if exp_str == "Unknown":
        return None, spot_price, "Unknown", 1, {}, False, "N/A", "Expiry Not Found"

    master_df = get_scrip_master_df()
    opt_df = master_df[
        (master_df["name"] == meta["name"]) &
        (master_df["exch_seg"] == meta["opt_exch"]) &
        (master_df["instrumenttype"] == "OPTIDX")
    ].copy()
    opt_df["expiry_dt"] = pd.to_datetime(opt_df["expiry"], format="%d%b%Y", errors="coerce")
    opt_df = opt_df.dropna(subset=["expiry_dt"])
    exp_df = opt_df[opt_df["expiry_dt"] == pd.to_datetime(exp_str, format="%d-%b-%Y")].copy()
    exp_df["clean_strike"] = (exp_df["strike"].astype(float) / 100.0).astype(int)
    exp_df["opt_type"] = exp_df["symbol"].str[-2:]

    span = meta["hedge"] + (6 * step)
    target_df = exp_df[(exp_df["clean_strike"] >= atm - span) & (exp_df["clean_strike"] <= atm + span)].copy()
    tokens = target_df["token"].astype(str).tolist()
    if not tokens:
        return None, spot_price, exp_str, trading_dte, {}, False, "N/A", "Tokens Empty"

    contract_map = {
        (int(r["clean_strike"]), r["opt_type"]): {"symbol": r["symbol"], "token": str(r["token"]), "exch": meta["opt_exch"]}
        for _, r in target_df.iterrows()
    }

    fetched_map = {}
    for i in range(0, len(tokens), 50):
        batch = tokens[i:i+50]
        try:
            q_res = requests.post(quote_url, json={"mode": "FULL", "exchangeTokens": {meta["opt_exch"]: batch}}, headers=headers, timeout=5).json()
            if q_res.get("status") and q_res.get("data", {}).get("fetched"):
                for item in q_res["data"]["fetched"]:
                    fetched_map[str(item["symbolToken"])] = item
        except Exception:
            pass

    if not fetched_map:
        return None, spot_price, exp_str, trading_dte, {}, False, "N/A", "Quotes Error"

    raw_rows = []
    for k in sorted(target_df["clean_strike"].unique()):
        ce_tok = contract_map.get((k, "CE"), {}).get("token", "")
        pe_tok = contract_map.get((k, "PE"), {}).get("token", "")
        ce_q = fetched_map.get(ce_tok, {})
        pe_q = fetched_map.get(pe_tok, {})
        raw_rows.append({
            "CE_TOKEN": ce_tok,
            "PE_TOKEN": pe_tok,
            "STRIKE": k,
            "CALL LTP": float(ce_q.get("ltp", 0.0) or 0.0),
            "PUT LTP": float(pe_q.get("ltp", 0.0) or 0.0),
            "CALL OI": int(float(ce_q.get("opnInterest", 0) or 0)),
            "PUT OI": int(float(pe_q.get("opnInterest", 0) or 0)),
            "API_CALL_CHG": int(float(ce_q.get("netChangeOpenInterest", 0) or 0)),
            "API_PUT_CHG": int(float(pe_q.get("netChangeOpenInterest", 0) or 0)),
            "DTE": max(0.2, float(trading_dte)),
        })

    df_acc, oi_valid, oi_source = compute_accurate_chg_oi(selected_idx, pd.DataFrame(raw_rows))
    return df_acc, spot_price, exp_str, trading_dte, contract_map, oi_valid, oi_source, None


# ==========================================
# Black-Scholes Delta, Pricing & Charges
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
    brokerage = 20.0 * 4
    stt = sell_turnover * 0.0015
    exch = total_to * 0.0003553
    sebi = total_to * 0.000001
    stamp = buy_turnover * 0.00003
    gst = (brokerage + exch + sebi) * 0.18
    return round(brokerage + stt + exch + sebi + stamp + gst, 2)


def pick_40_delta_spread(direction, selected_idx, spot_price, full_df):
    meta = get_index_meta(selected_idx)
    step, hedge_pts = meta["step"], meta["hedge"]
    dte_days = float(full_df["DTE"].iloc[0]) if ("DTE" in full_df.columns and not full_df.empty) else 1.0
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
# 16 EMA Pullback + ATM±3 Change in OI सिग्नल इंजन
# ==========================================
def evaluate_ema16_oi_setup(candle_df, spot_price, full_oc_df, selected_idx, tf_mins, oi_valid=True):
    meta = get_index_meta(selected_idx)
    step = meta["step"]
    atm = round(spot_price / step) * step

    if not oi_valid:
        return {
            "signal": "WAIT",
            "oi_bias": "N/A",
            "call_chg_3": 0,
            "put_chg_3": 0,
            "reason": "कल का क्लोज़िंग OI उपलब्ध नहीं — गलत Change in OI से बचने के लिए सिग्नल रुका है।",
            "touch_info": None
        }

    up_3 = full_oc_df[(full_oc_df["STRIKE"] > atm) & (full_oc_df["STRIKE"] <= atm + 3 * step)]
    dn_3 = full_oc_df[(full_oc_df["STRIKE"] < atm) & (full_oc_df["STRIKE"] >= atm - 3 * step)]
    call_chg_3 = int(up_3["CALL CHG OI"].sum()) if not up_3.empty else 0
    put_chg_3 = int(dn_3["PUT CHG OI"].sum()) if not dn_3.empty else 0

    # कम से कम 15% मज़बूत अंतर आवश्यक (1 शेयर के फ़र्क से गलत सिग्नल न बने)
    if put_chg_3 > 0 and put_chg_3 >= call_chg_3 * 1.15:
        oi_bias = "BULLISH"
    elif call_chg_3 > 0 and call_chg_3 >= put_chg_3 * 1.15:
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
    if len(today_df) < 4:
        return {
            "signal": "WAIT",
            "oi_bias": oi_bias,
            "call_chg_3": call_chg_3,
            "put_chg_3": put_chg_3,
            "reason": "पर्याप्त बंद कैंडल्स का इंतज़ार हो रहा है...",
            "touch_info": None
        }

    latest_bar = today_df.iloc[-1]
    latest_time = today_df.index[-1].strftime("%H:%M")
    lookback = today_df.iloc[-6:-1]

    if oi_bias == "BULLISH":
        for idx_t in range(len(lookback) - 1, -1, -1):
            t_bar = lookback.iloc[idx_t]
            t_time = lookback.index[idx_t].strftime("%H:%M")
            touched_ema = (t_bar["Low"] <= t_bar["EMA16"]) and (t_bar["High"] > t_bar["EMA16"])
            if touched_ema and latest_bar["Close"] > t_bar["High"]:
                return {
                    "signal": "BULLISH",
                    "oi_bias": oi_bias,
                    "call_chg_3": call_chg_3,
                    "put_chg_3": put_chg_3,
                    "reason": f"✅ BULLISH ENTRY! {t_time} कैंडल ने 16 EMA ({t_bar['EMA16']:.1f}) को टच किया और {latest_time} कैंडल ऊपर Close ({latest_bar['Close']:.1f}) हुई!",
                    "touch_info": {"touch_time": t_time, "touch_high": t_bar["High"], "close_px": latest_bar["Close"]}
                }

    if oi_bias == "BEARISH":
        for idx_t in range(len(lookback) - 1, -1, -1):
            t_bar = lookback.iloc[idx_t]
            t_time = lookback.index[idx_t].strftime("%H:%M")
            touched_ema = (t_bar["High"] >= t_bar["EMA16"]) and (t_bar["Low"] < t_bar["EMA16"])
            if touched_ema and latest_bar["Close"] < t_bar["Low"]:
                return {
                    "signal": "BEARISH",
                    "oi_bias": oi_bias,
                    "call_chg_3": call_chg_3,
                    "put_chg_3": put_chg_3,
                    "reason": f"🔴 BEARISH ENTRY! {t_time} कैंडल ने 16 EMA ({t_bar['EMA16']:.1f}) को टच किया और {latest_time} कैंडल नीचे Close ({latest_bar['Close']:.1f}) हुई!",
                    "touch_info": {"touch_time": t_time, "touch_low": t_bar["Low"], "close_px": latest_bar["Close"]}
                }

    return {
        "signal": "WAIT",
        "oi_bias": oi_bias,
        "call_chg_3": call_chg_3,
        "put_chg_3": put_chg_3,
        "reason": f"इंतज़ार: OI दिशा = {oi_bias} | 16 EMA टच और कैंडल क्लोज़ कन्फर्मेशन की प्रतीक्षा है।",
        "touch_info": None
    }


# ==========================================
# ट्रेड एंट्री और सुरक्षित एग्जिट इंजन
# ==========================================
def ema_execute_entry(mode, selected_idx, tf_mins, direction, strat_name, summary, legs, lots, lot_qty, contract_map, trigger_src):
    ist = pytz.timezone("Asia/Kolkata")
    now_str = datetime.now(ist).strftime("%I:%M:%S %p")
    total_qty = lots * lot_qty
    exch = "BFO" if selected_idx == "Sensex" else "NFO"

    if any(float(lg.get("entry_ltp", 0)) <= 0 for lg in legs):
        return False, "किसी स्ट्राइक का लाइव भाव 0 आ रहा है — एंट्री रोकी गई।"

    enriched = []
    for lg in legs:
        c_info = contract_map.get((lg["strike"], lg["side"]), {"symbol": "", "token": "0", "exch": exch})
        enriched.append({
            "action": lg["action"], "strike": lg["strike"], "side": lg["side"],
            "entry_ltp": lg["entry_ltp"], "last_valid_ltp": lg["entry_ltp"],
            "symbol": c_info["symbol"], "token": c_info["token"], "exch": c_info["exch"]
        })
    ordered = [x for x in enriched if x["action"] == "BUY"] + [x for x in enriched if x["action"] == "SELL"]

    if "LIVE ALGO" in mode:
        jwt, akey = ensure_angel_session()
        if not jwt:
            return False, "Angel One लॉगिन नहीं है!"
        done = []
        for lg in ordered:
            if not lg["token"] or lg["token"] == "0":
                return False, f"असली कॉन्ट्रैक्ट टोकन नहीं मिला ({lg['strike']} {lg['side']})"
            ok, msg = ema_place_order(jwt, akey, lg["exch"], lg["symbol"], lg["token"], lg["action"], total_qty)
            if not ok:
                for prev in reversed(done):
                    ema_place_order(jwt, akey, prev["exch"], prev["symbol"], prev["token"], "SELL" if prev["action"] == "BUY" else "BUY", total_qty)
                return False, f"ऑर्डर विफल ({lg['symbol']}): {msg}"
            done.append(lg)
            time.sleep(0.35)

    new_tr = {
        "mode": mode, "index": selected_idx, "tf": f"{tf_mins}m",
        "direction": direction, "strategy": strat_name, "summary": summary,
        "lots": lots, "qty": total_qty, "exch": exch, "legs": ordered,
        "entry_time": now_str, "sl_rs": -1700.0 * lots, "tsl_active": False
    }
    st.session_state["ema_active_trade"] = new_tr
    ema_save_active(new_tr)

    legs_txt = "\n".join([f"• <b>{l['action']}</b> {l['strike']} {l['side']} @ ₹{l['entry_ltp']}" for l in ordered])
    ema_send_tg(
        f"🚀 <b>16 EMA + OI ALGO ENTRY ({trigger_src})</b>\n"
        f"📊 <b>Index:</b> {selected_idx} ({tf_mins}-Min)\n"
        f"🧭 <b>Direction:</b> {direction} ({strat_name})\n{legs_txt}\n"
        f"📦 <b>Qty:</b> {lots} Lot ({total_qty}) | ⏰ {now_str}"
    )
    return True, f"{strat_name} एंट्री सफल!"


def ema_close_trade(curr_ltp_map, reason):
    tr = st.session_state.get("ema_active_trade")
    if not tr:
        return False
    ist = pytz.timezone("Asia/Kolkata")
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    exit_time = datetime.now(ist).strftime("%I:%M:%S %p")
    legs = tr["legs"]

    if "LIVE ALGO" in tr["mode"]:
        jwt, akey = ensure_angel_session()
        if not jwt:
            ema_send_tg("🚨 <b>16 EMA EXIT FAILED:</b> Angel One लॉगिन बंद है, तुरंत मैनुअल स्क्वेयर-ऑफ करें!")
            return False
        for lg in legs:
            if lg["action"] == "SELL":
                ok_x, err_x = ema_place_order(jwt, akey, lg["exch"], lg["symbol"], lg["token"], "BUY", tr["qty"])
                if not ok_x:
                    ema_send_tg(f"🚨 <b>16 EMA EXIT FAILED ({lg['symbol']}):</b> {err_x}")
                    return False
                time.sleep(0.35)
        for lg in legs:
            if lg["action"] == "BUY":
                ema_place_order(jwt, akey, lg["exch"], lg["symbol"], lg["token"], "SELL", tr["qty"])
                time.sleep(0.35)

    pnl_pts = 0.0
    for i, lg in enumerate(legs):
        px = curr_ltp_map.get(i, lg.get("last_valid_ltp", lg["entry_ltp"]))
        pnl_pts += (lg["entry_ltp"] - px) if lg["action"] == "SELL" else (px - lg["entry_ltp"])
    pnl_rs = round(pnl_pts * tr["qty"], 2)

    if "PAPER" in tr["mode"]:
        st.session_state["ema_paper_capital"] += pnl_rs

    mode_short = "Paper" if "PAPER" in tr["mode"] else "LIVE"
    st.session_state["ema_trade_history"].insert(0, {
        "तारीख": today_str, "मोड": mode_short, "इंडेक्स": tr["index"],
        "टाइमफ्रेम": tr.get("tf", "5m"), "दिशा": tr.get("direction", "-"),
        "स्प्रेड": tr["summary"], "लॉट": tr["lots"], "एंट्री समय": tr["entry_time"],
        "एग्जिट समय": exit_time, "कारण": reason, "लाभ / हानि (₹)": pnl_rs
    })
    ema_save_history(st.session_state["ema_trade_history"])
    ema_save_active(None)
    st.session_state["ema_active_trade"] = None

    ema_send_tg(
        f"🏁 <b>16 EMA + OI TRADE CLOSED ({mode_short})</b>\n"
        f"📊 <b>Index:</b> {tr['index']} | 📌 {tr['summary']}\n"
        f"📝 <b>Reason:</b> {reason} | 💵 <b>Final P&L:</b> <b>₹{pnl_rs:+,.0f}</b>"
    )
    return True


# ==========================================
# Session State & UI
# ==========================================
if "ema_trade_history" not in st.session_state:
    st.session_state["ema_trade_history"] = ema_load_history()
if "ema_paper_capital" not in st.session_state:
    pnl_sum = sum(float(x.get("लाभ / हानि (₹)", 0)) for x in st.session_state["ema_trade_history"] if x.get("मोड") == "Paper")
    st.session_state["ema_paper_capital"] = 200000.0 + pnl_sum
if "ema_active_trade" not in st.session_state:
    st.session_state["ema_active_trade"] = ema_load_active()

st.title("📈 16 EMA Pullback + ATM±3 OI एल्गो (40 Delta Spread)")
st.caption("सुबह 9:45 से 12:00 PM (दिन में 1 ट्रेड) | 3-Min / 5-Min 16 EMA टच + क्लोज कन्फर्म | SL ₹1,700 | TSL +₹200 (@₹1,200) | OI Reversal Exit | 2:59 PM Square-Off")

tab_live, tab_bt = st.tabs(["🤖 Live / Paper ऑटो एल्गो + 16 EMA चार्ट", "📊 16 EMA + OI बैकटेस्ट (सही DTE व Lot Size)"])

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

    cfg_ema = load_algo_settings()
    t_auto1, t_auto2 = st.columns([1.6, 2.4])
    with t_auto1:
        auto_arm = st.toggle(
            "🤖 ऑटो-एंट्री चालू रखें (9:45 AM – 12:00 PM | दिन में 1 ट्रेड)",
            value=bool(cfg_ema.get("ema_auto_arm")),
            key="ema_auto_arm_widget"
        )
        set_toggle_setting("ema_auto_arm", auto_arm)
    with t_auto2:
        meta_preview = get_index_meta(ema_idx)
        st.info(f"💰 **पेपर बैलेंस:** `₹{st.session_state['ema_paper_capital']:,.0f}` | 📦 **Lot Size:** `{meta_preview['lot']} Qty` | 🛑 **SL:** `-₹{1700*ema_lots:,}` | 🎯 **TSL:** `+₹{200*ema_lots:,}`")

    @st.fragment(run_every=10)
    def render_ema16_live(mode_sel, selected_idx, tf_m, lots_cnt, is_unlocked, is_armed):
        ist = pytz.timezone("Asia/Kolkata")
        now_ist = datetime.now(ist)
        now_str = now_ist.strftime("%I:%M:%S %p")
        today_str = now_ist.strftime("%Y-%m-%d")
        curr_mins = now_ist.hour * 60 + now_ist.minute

        tg_cmd = ema_check_tg_cmd()
        meta = get_index_meta(selected_idx)
        lot_qty, hedge_pts = meta["lot"], meta["hedge"]

        jwt, akey = ensure_angel_session()
        if not jwt or not akey:
            st.error("🚨 Angel One SmartAPI कनेक्ट नहीं है — बिना असली डेटा के यह पेज ट्रेड नहीं लेगा।")
            return

        candle_df = ema_fetch_intraday_candles(jwt, akey, selected_idx, tf_m, days_back=4)
        full_oc_df, spot_price, exp_str, trading_dte, contract_map, oi_valid, oi_source, oc_err = ema_fetch_live_oc(
            jwt, akey, selected_idx
        )

        if full_oc_df is None or full_oc_df.empty or not spot_price or candle_df.empty:
            st.error(f"⚠️ लाइव डेटा रुका हुआ है ({oc_err or 'Candle Empty'}) — बिना असली डेटा के कोई सिग्नल या एंट्री नहीं बनेगी।")
            return

        setup = evaluate_ema16_oi_setup(candle_df, spot_price, full_oc_df, selected_idx, tf_m, oi_valid=oi_valid)
        oi_bias = setup["oi_bias"]
        call_chg_3 = setup["call_chg_3"]
        put_chg_3 = setup["put_chg_3"]

        active_tr = st.session_state.get("ema_active_trade")
        if active_tr is not None:
            st.markdown("### 🔥 चालू पोजीशन (Live MTM, Trailing SL & OI Reversal Guard)")
            legs = active_tr["legs"]
            tokens_tup = tuple(l["token"] for l in legs if l.get("token") and l["token"] != "0")
            ltp_map = ema_get_live_ltp(jwt, akey, active_tr["exch"], tokens_tup) if tokens_tup else {}

            curr_px_map = {}
            live_pnl_pts = 0.0
            for i, lg in enumerate(legs):
                if lg["token"] in ltp_map and ltp_map[lg["token"]] > 0:
                    px = ltp_map[lg["token"]]
                    lg["last_valid_ltp"] = px
                else:
                    px = float(lg.get("last_valid_ltp", lg["entry_ltp"]))
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
                ema_send_tg(f"🔒 <b>TRAILING SL LOCKED!</b> प्रॉफिट ₹{live_pnl_rs:,.0f} पार — SL अब <b>+₹{int(tsl_lock_rs):,}</b> पर सुरक्षित है।")

            cur_sl_rs = float(active_tr.get("sl_rs", -1700.0 * lots_tr))
            tr_dir = active_tr.get("direction", "BULLISH")
            oi_flipped = oi_valid and ((tr_dir == "BULLISH" and oi_bias == "BEARISH") or (tr_dir == "BEARISH" and oi_bias == "BULLISH"))

            if tg_cmd == "STATUS":
                ema_send_tg(f"📊 <b>16 EMA STATUS</b>\n📌 {active_tr['summary']}\n💵 MTM: <b>₹{live_pnl_rs:+,.0f}</b> | SL: ₹{cur_sl_rs:+,.0f}")

            if tg_cmd == "EXIT":
                if ema_close_trade(curr_px_map, "Telegram /exit Command 📲"):
                    st.rerun()
            elif live_pnl_rs <= cur_sl_rs:
                exit_lbl = f"Trailing SL Hit (+₹{int(cur_sl_rs):,}) 🔒" if active_tr.get("tsl_active") else f"Initial SL Hit (-₹{int(abs(cur_sl_rs)):,}) 🛑"
                if ema_close_trade(curr_px_map, exit_lbl):
                    st.rerun()
            elif oi_flipped:
                if ema_close_trade(curr_px_map, f"ATM±3 OI Reversed ({tr_dir} ➔ {oi_bias}) 🔄"):
                    st.rerun()
            elif curr_mins >= (14 * 60 + 59):
                if ema_close_trade(curr_px_map, "2:59 PM Auto Square-Off ⏰"):
                    st.rerun()

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("इंडेक्स & दिशा", f"{active_tr['index']} ({tr_dir})", active_tr["strategy"])
            m2.metric("लाइव MTM लाभ / हानि", f"₹{live_pnl_rs:+,.0f}", f"Qty: {active_tr['qty']}")
            m3.metric("सक्रिय Stop-Loss", f"₹{cur_sl_rs:+,.0f}", "TSL Locked 🔒" if active_tr.get("tsl_active") else "Initial SL (-₹1700/lot)")
            m4.metric("ATM±3 OI स्थिति", oi_bias, f"Put {put_chg_3:+,} | Call {call_chg_3:+,}" if oi_valid else "N/A")

            lcols = st.columns(len(legs))
            for i, lg in enumerate(legs):
                cpx = curr_px_map[i]
                lpnl = round(((lg["entry_ltp"] - cpx) if lg["action"] == "SELL" else (cpx - lg["entry_ltp"])) * active_tr["qty"], 0)
                lcols[i].metric(f"{lg['action']} {lg['strike']} {lg['side']}", f"₹{cpx}", f"Entry ₹{lg['entry_ltp']} | ₹{lpnl:+,.0f}")

            if st.button("🚨 अभी पोजीशन बंद करें (Square-Off)", type="primary", use_container_width=True):
                if ema_close_trade(curr_px_map, "Manual Square-Off"):
                    st.rerun()

        else:
            in_entry_window = (9 * 60 + 45) <= curr_mins <= (12 * 60)
            already_traded_today = is_strategy_traded_today("ema", today_str)

            preview_dir = setup["signal"] if setup["signal"] in ["BULLISH", "BEARISH"] else (oi_bias if oi_bias in ["BULLISH", "BEARISH"] else "BULLISH")
            s_name, s_sum, p_legs, net_cr = pick_40_delta_spread(preview_dir, selected_idx, spot_price, full_oc_df)

            bg_c = "linear-gradient(90deg, #064e3b, #022c22)" if setup["signal"] == "BULLISH" else ("linear-gradient(90deg, #7f1d1d, #450a0a)" if setup["signal"] == "BEARISH" else "linear-gradient(90deg, #1e293b, #0f172a)")
            bd_c = "#10b981" if setup["signal"] == "BULLISH" else ("#ef4444" if setup["signal"] == "BEARISH" else "#f59e0b")

            win_badge = "🟢 एंट्री विंडो खुली है (9:45–12:00)" if in_entry_window else "⏳ एंट्री विंडो बंद (केवल 9:45 AM – 12:00 PM)"
            if already_traded_today:
                win_badge = "✅ आज का 1 ट्रेड पूरा हो चुका है"

            oi_line = (
                f"नीचे 3 Put Chg OI = <b>{put_chg_3:+,}</b> &nbsp;vs&nbsp; ऊपर 3 Call Chg OI = <b>{call_chg_3:+,}</b> &nbsp;➔&nbsp; <b>OI सिग्नल: {oi_bias}</b>"
                if oi_valid else "<b>N/A (कल का क्लोज़िंग OI उपलब्ध नहीं)</b>"
            )

            st.markdown(f"""
            <div style="background:{bg_c}; padding:16px 20px; border-radius:12px; border-left:6px solid {bd_c}; color:white; margin-bottom:12px;">
                <div style="font-size:13px; color:#e2e8f0;">⏱️ {now_str} | 📍 {selected_idx} Spot: <b>{spot_price:,.2f}</b> | 📅 Expiry: <b>{exp_str} ({trading_dte} DTE)</b> | 🕯️ <b>{tf_m}-Min</b> | {win_badge}</div>
                <div style="font-size:14px; margin-top:6px;">📊 <b>ATM±3 Change in OI ({oi_source}):</b> {oi_line}</div>
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
                        mark_strategy_traded_today("ema", today_str)
                        st.success(f"🚀 {msg}")
                        st.rerun()

            b1, b2 = st.columns(2)
            with b1:
                bul_name, bul_sum, bul_legs, _ = pick_40_delta_spread("BULLISH", selected_idx, spot_price, full_oc_df)
                if st.button(f"🟢 मैनुअल BULLISH एंट्री ({bul_sum})", disabled=("LIVE ALGO" in mode_sel and not is_unlocked), use_container_width=True):
                    ok, msg = ema_execute_entry(mode_sel, selected_idx, tf_m, "BULLISH", bul_name, bul_sum, bul_legs, lots_cnt, lot_qty, contract_map, "Manual")
                    if ok:
                        mark_strategy_traded_today("ema", today_str)
                        st.rerun()
            with b2:
                ber_name, ber_sum, ber_legs, _ = pick_40_delta_spread("BEARISH", selected_idx, spot_price, full_oc_df)
                if st.button(f"🔴 मैनुअल BEARISH एंट्री ({ber_sum})", disabled=("LIVE ALGO" in mode_sel and not is_unlocked), use_container_width=True):
                    ok, msg = ema_execute_entry(mode_sel, selected_idx, tf_m, "BEARISH", ber_name, ber_sum, ber_legs, lots_cnt, lot_qty, contract_map, "Manual")
                    if ok:
                        mark_strategy_traded_today("ema", today_str)
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

with tab_bt:
    st.subheader("📊 16 EMA Pullback + 40 Delta Spread बैकटेस्ट (सही DTE व Lot Size)")
    st.info("ℹ️ यह बैकटेस्ट असली ऐतिहासिक स्पॉट कैंडल व 16 EMA पर चलता है और छुट्टियों के अनुसार सही 0 DTE / 1 DTE गिनता है।")

    PERIOD_OPTIONS = {
        "1 महीना (30 दिन)": 30,
        "2 महीना (60 दिन)": 60,
        "3 महीना (90 दिन)": 90,
        "6 महीना (180 दिन)": 180,
        "1 साल (365 दिन)": 365,
    }

    bc1, bc2, bc3, bc4 = st.columns([1.3, 1.2, 1.5, 1])
    with bc1:
        bt_idx = st.selectbox("इंडेक्स", ["Nifty 50", "Sensex"], key="ema_bt_idx")
    with bc2:
        bt_tf = st.selectbox("टाइमफ्रेम", ["5 Minute", "3 Minute"], key="ema_bt_tf")
    with bc3:
        bt_period_lbl = st.selectbox("बैकटेस्ट अवधि चुनें", list(PERIOD_OPTIONS.keys()), index=1, key="ema_bt_days")
    with bc4:
        bt_lots = st.number_input("लॉट", min_value=1, max_value=25, value=1, key="ema_bt_lots")

    apply_costs = st.checkbox("✅ Brokerage + Slippage + Tax घटाकर दिखाएं", value=True, key="ema_bt_cost_chk")

    if st.button("🚀 16 EMA बैकटेस्ट चलाएं", use_container_width=True):
        tf_m_bt = 5 if "5" in bt_tf else 3
        days_n = PERIOD_OPTIONS[bt_period_lbl]
        jwt, akey = ensure_angel_session()
        if not jwt:
            st.error("Angel One लॉगिन आवश्यक है।")
        else:
            with st.spinner("ऐतिहासिक कैंडल्स लोड हो रही हैं..."):
                df_c = ema_fetch_intraday_candles(jwt, akey, bt_idx, tf_m_bt, days_back=days_n)
                if df_c.empty:
                    st.error("डेटा लोड नहीं हो पाया।")
                else:
                    meta = get_index_meta(bt_idx)
                    lot_qty, step, hedge_pts = meta["lot"], meta["step"], meta["hedge"]
                    total_qty = lot_qty * bt_lots
                    iv_base = 0.14 if bt_idx == "Nifty 50" else 0.15
                    slip_mult = 1.0 if apply_costs else 0.0

                    df_c["Date"] = df_c.index.date
                    dte_map = build_backtest_dte_map(sorted(df_c["Date"].unique()), bt_idx)
                    actual_days = df_c["Date"].nunique()
                    logs = []

                    for dt, day_bars in df_c.groupby("Date"):
                        day_bars = day_bars.between_time("09:15", "14:59")
                        if len(day_bars) < 8:
                            continue
                        open_915 = float(day_bars["Open"].iloc[0])
                        days_to_exp = dte_map.get(dt, 1)
                        t_base = (days_to_exp + 0.5) / 365.0

                        for i in range(3, len(day_bars)):
                            bar_ts = day_bars.index[i]
                            mins_now = bar_ts.hour * 60 + bar_ts.minute
                            if mins_now < (9 * 60 + 45) or mins_now > (12 * 60):
                                continue
                            cur_bar = day_bars.iloc[i]
                            prev_bars = day_bars.iloc[max(0, i-4):i]

                            day_chg_pct = ((cur_bar["Close"] - open_915) / open_915) * 100.0
                            trend_dir = "BULLISH" if day_chg_pct >= 0.15 else ("BEARISH" if day_chg_pct <= -0.15 else "NEUTRAL")

                            matched_dir = None
                            if trend_dir == "BULLISH":
                                for j in range(len(prev_bars)-1, -1, -1):
                                    tb = prev_bars.iloc[j]
                                    if tb["Low"] <= tb["EMA16"] and tb["High"] > tb["EMA16"] and cur_bar["Close"] > tb["High"]:
                                        matched_dir = "BULLISH"
                                        break
                            elif trend_dir == "BEARISH":
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
                                raw_sell = calc_bs_price(spot_ent, best_k, t_base, iv_base, side)
                                raw_buy = calc_bs_price(spot_ent, hedge_k, t_base, iv_base, side)
                                sell_ent = max(0.5, raw_sell - (max(0.35, raw_sell * 0.004) * slip_mult))
                                buy_ent = raw_buy + (max(0.35, raw_buy * 0.01) * slip_mult)

                                sl_limit = -1700.0 * bt_lots
                                tsl_locked = False
                                exit_reason = "2:59 PM Exit ⏰"
                                s_exit, b_exit = sell_ent, buy_ent

                                rem_bars = day_bars.iloc[i+1:]
                                total_rem = max(1, len(rem_bars))
                                for step_idx, (_, ex_bar) in enumerate(rem_bars.iterrows()):
                                    t_now = t_base * max(0.05, 1.0 - (0.35 * (step_idx / total_rem)))
                                    # High और Low दोनों पर इंट्रा-कैंडल SL जाँचें
                                    worst_sp = float(ex_bar["Low"]) if matched_dir == "BULLISH" else float(ex_bar["High"])
                                    best_sp = float(ex_bar["High"]) if matched_dir == "BULLISH" else float(ex_bar["Low"])

                                    best_mtm = ((sell_ent - calc_bs_price(best_sp, best_k, t_now, iv_base, side)) + (calc_bs_price(best_sp, hedge_k, t_now, iv_base, side) - buy_ent)) * total_qty
                                    if not tsl_locked and best_mtm >= (1200.0 * bt_lots):
                                        tsl_locked = True
                                        sl_limit = 200.0 * bt_lots

                                    s_w = calc_bs_price(worst_sp, best_k, t_now, iv_base, side)
                                    b_w = calc_bs_price(worst_sp, hedge_k, t_now, iv_base, side)
                                    worst_mtm = ((sell_ent - s_w) + (b_w - buy_ent)) * total_qty

                                    s_exit = calc_bs_price(float(ex_bar["Close"]), best_k, t_now, iv_base, side)
                                    b_exit = calc_bs_price(float(ex_bar["Close"]), hedge_k, t_now, iv_base, side)

                                    if worst_mtm <= sl_limit:
                                        s_exit, b_exit = s_w, b_w
                                        exit_reason = f"TSL Hit (+₹{int(sl_limit):,}) 🔒" if tsl_locked else f"SL Hit (-₹{int(abs(sl_limit)):,}) 🛑"
                                        break

                                s_ext_f = s_exit + (max(0.35, s_exit * 0.004) * slip_mult)
                                b_ext_f = max(0.05, b_exit - (max(0.35, b_exit * 0.01) * slip_mult))
                                after_slip = ((sell_ent - s_ext_f) + (b_ext_f - buy_ent)) * total_qty
                                brok = calc_spread_charges((sell_ent + b_ext_f) * total_qty, (s_ext_f + buy_ent) * total_qty) if apply_costs else 0.0

                                logs.append({
                                    "तारीख": str(dt), "DTE": f"{days_to_exp} DTE", "समय": bar_ts.strftime("%H:%M"),
                                    "दिशा": matched_dir, "स्प्रेड": f"Sell {best_k} {side} / Buy {hedge_k} {side}",
                                    "नतीजा": exit_reason, "Final P&L (₹)": round(after_slip - brok, 0)
                                })
                                break

                    st.caption(f"📌 **वास्तविक उपलब्ध डेटा:** `{actual_days} ट्रेडिंग दिन` | **Lot Size:** `{lot_qty} Qty`")
                    if logs:
                        res_df = pd.DataFrame(logs)
                        res_df["कुल पूंजी (₹)"] = res_df["Final P&L (₹)"].cumsum()
                        m1, m2, m3 = st.columns(3)
                        m1.metric("Final P&L", f"₹{res_df['Final P&L (₹)'].sum():+,.0f}", f"{len(res_df)} ट्रेड")
                        m2.metric("Win Rate", f"{round((len(res_df[res_df['Final P&L (₹)'] > 0]) / len(res_df)) * 100, 1)}%")
                        m3.metric("Max Drawdown", f"-₹{int((res_df['कुल पूंजी (₹)'].cummax() - res_df['कुल पूंजी (₹)']).max()):,}")
                        st.line_chart(res_df.set_index("तारीख")["कुल पूंजी (₹)"])
                        st.dataframe(res_df, use_container_width=True, hide_index=True)
                    else:
                        st.info("चुनी गई अवधि में कोई सेटअप नहीं मिला।")
