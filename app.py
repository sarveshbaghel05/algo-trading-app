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

st.set_page_config(page_title="Option Seller Algo Terminal", layout="wide")

try:
    from pages.otm_iv_indicator import evaluate_otm_iv_from_df
except ImportError:
    from otm_iv_indicator import evaluate_otm_iv_from_df

try:
    from pages.greek_regime_indicator import evaluate_greeks_regime
except ImportError:
    from greek_regime_indicator import evaluate_greeks_regime


# ==========================================
# Telegram Alert & Safe Remote Control
# ==========================================
def send_telegram_alert(message_text):
    try:
        bot_token = st.secrets.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = str(st.secrets.get("TELEGRAM_CHAT_ID", "")).strip()
        if not bot_token or not chat_id:
            return False, "Telegram Secrets सेव नहीं हैं।"
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        res = requests.post(url, json={"chat_id": chat_id, "text": message_text, "parse_mode": "HTML"}, timeout=5).json()
        return bool(res.get("ok")), res.get("description", "Sent")
    except Exception as e:
        return False, str(e)


def check_telegram_commands():
    try:
        bot_token = st.secrets.get("TELEGRAM_BOT_TOKEN", "").strip()
        allowed_chat_id = str(st.secrets.get("TELEGRAM_CHAT_ID", "")).strip()
        if not bot_token or not allowed_chat_id:
            return None
        last_offset = get_saved_tg_offset()
        url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
        res = requests.get(url, params={"offset": last_offset + 1, "timeout": 1}, timeout=3).json()
        if not res.get("ok"):
            return None
        detected_cmd = None
        max_uid = last_offset
        now_ts = time.time()
        for update in res.get("result", []):
            upd_id = int(update.get("update_id", 0))
            if upd_id > max_uid:
                max_uid = upd_id
            msg = update.get("message", {})
            msg_age = now_ts - float(msg.get("date", now_ts))
            if msg_age > 90:
                continue
            sender_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "").strip().upper()
            if sender_id == allowed_chat_id:
                if text in ["/EXIT", "EXIT", "STOP", "CLOSE"]:
                    detected_cmd = "EXIT"
                elif text in ["/STATUS", "STATUS", "MTM", "PNL"]:
                    detected_cmd = "STATUS"
        if max_uid > last_offset:
            save_tg_offset(max_uid)
        return detected_cmd
    except Exception:
        return None


# ==========================================
# Angel One SmartAPI Order & Live Option Chain
# ==========================================
def place_angel_order(jwt_token, api_key, exchange, trading_symbol, symbol_token, transaction_type, quantity):
    url = "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/placeOrder"
    headers = {
        "Authorization": f"Bearer {jwt_token}", "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": api_key.strip()
    }
    payload = {
        "variety": "NORMAL", "tradingsymbol": trading_symbol, "symboltoken": str(symbol_token),
        "transactiontype": transaction_type, "exchange": exchange, "ordertype": "MARKET",
        "producttype": "INTRADAY", "duration": "DAY", "price": "0",
        "squareoff": "0", "stoploss": "0", "quantity": str(quantity)
    }
    for attempt in range(3):
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
def get_live_ltp_for_tokens(jwt_token, api_key, exchange, tokens_tuple):
    url = "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/"
    headers = {
        "Authorization": f"Bearer {jwt_token}", "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": api_key.strip()
    }
    valid_tokens = [str(t) for t in tokens_tuple if str(t) and str(t) != "0"]
    if not valid_tokens:
        return {}
    try:
        res = requests.post(url, json={"mode": "LTP", "exchangeTokens": {exchange: valid_tokens}}, headers=headers, timeout=5).json()
        if res.get("status"):
            return {str(item["symbolToken"]): float(item["ltp"]) for item in res["data"]["fetched"]}
    except Exception:
        pass
    return {}


@st.cache_data(ttl=6)
def fetch_angel_live_oc_raw(jwt_token, api_key, selected_idx, s_range):
    meta = get_index_meta(selected_idx)
    headers = {
        "Authorization": f"Bearer {jwt_token}", "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": api_key.strip()
    }
    quote_url = "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/"

    try:
        spot_res = requests.post(
            quote_url,
            json={"mode": "LTP", "exchangeTokens": {meta["exch"]: [meta["token"]]}},
            headers=headers, timeout=5
        ).json()
        if not spot_res.get("status"):
            return None, None, "Unknown", 1, {}, f"Spot API Error: {spot_res.get('message', '')}"
        spot_price = float(spot_res["data"]["fetched"][0]["ltp"])
    except Exception as e:
        return None, None, "Unknown", 1, {}, str(e)

    step = meta["step"]
    atm_strike = round(spot_price / step) * step
    exp_str, trading_dte, _ = get_live_expiry_info(selected_idx)
    if exp_str == "Unknown":
        return None, spot_price, "Unknown", 1, {}, "एक्सपायरी तारीख नहीं मिली"

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

    fetch_span = max(s_range * step, meta["hedge"] + step, 4 * step)
    target_df = exp_df[(exp_df["clean_strike"] >= atm_strike - fetch_span) & (exp_df["clean_strike"] <= atm_strike + fetch_span)].copy()
    tokens = target_df["token"].astype(str).tolist()
    if not tokens:
        return None, spot_price, exp_str, trading_dte, {}, "स्ट्राइक टोकन नहीं मिले"

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
        return None, spot_price, exp_str, trading_dte, {}, "ऑप्शन कोट्स नहीं मिले"

    raw_rows = []
    for st_val in sorted(target_df["clean_strike"].unique()):
        ce_tok = contract_map.get((st_val, "CE"), {}).get("token", "")
        pe_tok = contract_map.get((st_val, "PE"), {}).get("token", "")
        ce_q = fetched_map.get(ce_tok, {})
        pe_q = fetched_map.get(pe_tok, {})
        raw_rows.append({
            "CE_TOKEN": ce_tok,
            "PE_TOKEN": pe_tok,
            "CALL VOL": int(float(ce_q.get("tradeVolume", 0) or 0)),
            "CALL OI": int(float(ce_q.get("opnInterest", 0) or 0)),
            "API_CALL_CHG": int(float(ce_q.get("netChangeOpenInterest", 0) or 0)),
            "CALL LTP": float(ce_q.get("ltp", 0.0) or 0.0),
            "STRIKE": st_val,
            "PUT LTP": float(pe_q.get("ltp", 0.0) or 0.0),
            "PUT OI": int(float(pe_q.get("opnInterest", 0) or 0)),
            "API_PUT_CHG": int(float(pe_q.get("netChangeOpenInterest", 0) or 0)),
            "PUT VOL": int(float(pe_q.get("tradeVolume", 0) or 0)),
        })
    return pd.DataFrame(raw_rows), spot_price, exp_str, trading_dte, contract_map, None


def record_daily_oc_snapshot(selected_idx, spot_price, exp_str, df_oc):
    """तारीख के हिसाब से अलग फ़ाइल में सेव करें ताकि 1MB सीमा से पुराना डेटा न मिटे"""
    try:
        ist = pytz.timezone("Asia/Kolkata")
        now_ist = datetime.now(ist)
        if now_ist.weekday() >= 5:
            return
        curr_mins = now_ist.hour * 60 + now_ist.minute
        if curr_mins < (9 * 60 + 15) or curr_mins > (15 * 60 + 30):
            return
        today_str = now_ist.strftime("%Y-%m-%d")
        min_key = f"{selected_idx}_{today_str}_{now_ist.strftime('%H:%M')}"
        if st.session_state.get("last_rec_min_key") == min_key:
            return

        day_file = f"oc_history_{today_str}.csv"
        save_df = df_oc[["STRIKE", "CALL VOL", "CALL CHG OI", "CALL OI", "CALL LTP", "PUT LTP", "PUT OI", "PUT CHG OI", "PUT VOL"]].copy()
        save_df.insert(0, "Time", now_ist.strftime("%H:%M"))
        save_df.insert(1, "Index", selected_idx)
        save_df.insert(2, "Spot", round(spot_price, 2))
        save_df.insert(3, "Expiry", exp_str)

        write_hdr = not os.path.exists(day_file)
        save_df.to_csv(day_file, mode="a", header=write_hdr, index=False)
        st.session_state["last_rec_min_key"] = min_key

        if time.time() - st.session_state.get("last_oc_sync_ts", 0) >= 900:
            with open(day_file, "r", encoding="utf-8") as f:
                gh_write_text(f"oc_logs/{day_file}", f.read(), f"Sync OC log {today_str}")
            st.session_state["last_oc_sync_ts"] = time.time()
    except Exception:
        pass


def fetch_angel_live_oc(jwt_token, api_key, selected_idx, s_range):
    raw_df, spot_price, exp_str, trading_dte, contract_map, err = fetch_angel_live_oc_raw(jwt_token, api_key, selected_idx, s_range)
    if raw_df is None or raw_df.empty:
        return None, spot_price, exp_str, trading_dte, contract_map, False, "N/A", err

    df_acc, oi_valid, oi_source = compute_accurate_chg_oi(selected_idx, raw_df)
    final_cols = ["CALL VOL", "CALL CHG OI", "CALL OI", "CALL LTP", "STRIKE", "PUT LTP", "PUT OI", "PUT CHG OI", "PUT VOL"]
    final_df = df_acc[final_cols]
    if oi_valid:
        record_daily_oc_snapshot(selected_idx, spot_price, exp_str, final_df)
    return final_df, spot_price, exp_str, trading_dte, contract_map, oi_valid, oi_source, None


# ==========================================
# Background Multi-Strategy Paper Auto-Runner
# (तीनों रणनीतियों में अपने आप पेपर ट्रेड चलाने वाला साझा इंजन)
# ==========================================
def run_background_paper_engines(jwt, akey):
    """आप किसी भी पेज पर हों, जिन रणनीतियों के टॉगल ON हैं उनके पेपर ट्रेड पीछे से अपने आप चलेंगे"""
    if not jwt or not akey:
        return
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    if now_ist.weekday() >= 5:
        return
    curr_mins = now_ist.hour * 60 + now_ist.minute
    if curr_mins < (9 * 60 + 15) or curr_mins > (15 * 60 + 25):
        return

    # हर 12 सेकंड में एक बार ही बैकग्राउंड चेक चलाएं
    if time.time() - st.session_state.get("bg_runner_last_ts", 0) < 12:
        return
    st.session_state["bg_runner_last_ts"] = time.time()

    cfg = load_algo_settings()
    today_str = now_ist.strftime("%Y-%m-%d")
    now_str = now_ist.strftime("%I:%M:%S %p")

    # 1. Main Terminal (10:00 AM Auto Paper)
    if cfg.get("main_auto_10am") and not is_strategy_traded_today("main", today_str):
        if now_ist.hour == 10 and now_ist.minute <= 3:
            raw_act, _, _ = gh_read_text("active_trade_state.json")
            has_act = bool(raw_act and len(raw_act.strip()) > 5 and raw_act.strip() != "{}")
            if not has_act:
                sel_idx = st.session_state.get("algo_idx", "Nifty 50")
                lots_cnt = int(st.session_state.get("algo_lots", 1))
                meta = get_index_meta(sel_idx)
                df_oc, spot, exp_s, _, cmap, oi_ok, _, _ = fetch_angel_live_oc(jwt, akey, sel_idx, 5)
                if df_oc is not None and oi_ok and spot:
                    g_inf = evaluate_greeks_regime(sel_idx, spot, df_oc, exp_s, oi_valid=True)
                    m_dir = g_inf.get("direction", "WAIT")
                    if m_dir in ["UP", "DOWN", "SIDEWAYS"]:
                        atm = round(spot / meta["step"]) * meta["step"]
                        s_name, s_sum, legs, _ = build_strategy_plan(m_dir, sel_idx, atm, meta["hedge"], df_oc)
                        execute_multi_leg_entry(
                            "🟡 PAPER TRADING (वर्चुअल प्रैक्टिस)", sel_idx, s_name, s_sum,
                            legs, lots_cnt, meta["lot"], cmap, trigger_src="Auto 10:00 AM (Background)"
                        )
                        mark_strategy_traded_today("main", today_str)

    # 2. 16 EMA + OI (9:45 AM – 12:00 PM Auto Paper)
    if cfg.get("ema_auto_arm") and not is_strategy_traded_today("ema", today_str):
        if (9 * 60 + 45) <= curr_mins <= (12 * 60):
            try:
                from pages.ema16_oi_algo import (
                    ema_fetch_intraday_candles,
                    evaluate_ema16_oi_setup,
                    pick_40_delta_spread,
                    ema_load_active,
                    ema_save_active,
                )
                if not ema_load_active():
                    e_idx = st.session_state.get("ema_idx_sel", "Nifty 50")
                    e_tf = 5 if "5" in st.session_state.get("ema_tf_sel", "5 Minute") else 3
                    e_lots = int(st.session_state.get("ema_lots_sel", 1))
                    meta = get_index_meta(e_idx)
                    df_oc, spot, exp_s, dte_v, cmap, oi_ok, _, _ = fetch_angel_live_oc(jwt, akey, e_idx, 6)
                    c_df = ema_fetch_intraday_candles(jwt, akey, e_idx, e_tf, days_back=3)
                    if df_oc is not None and oi_ok and not c_df.empty and spot:
                        df_oc_dte = df_oc.copy()
                        df_oc_dte["DTE"] = max(0.2, float(dte_v))
                        setup = evaluate_ema16_oi_setup(c_df, spot, df_oc_dte, e_idx, e_tf, oi_valid=True)
                        if setup["signal"] in ["BULLISH", "BEARISH"]:
                            s_nm, s_sm, p_lgs, _ = pick_40_delta_spread(setup["signal"], e_idx, spot, df_oc_dte)
                            total_q = meta["lot"] * e_lots
                            ordered = [x for x in p_lgs if x["action"] == "BUY"] + [x for x in p_lgs if x["action"] == "SELL"]
                            for lg in ordered:
                                ci = cmap.get((lg["strike"], lg["side"]), {"symbol": "", "token": "0", "exch": meta["opt_exch"]})
                                lg.update({"symbol": ci["symbol"], "token": ci["token"], "exch": ci["exch"]})
                            new_tr = {
                                "mode": "🟡 PAPER TRADING (वर्चुअल प्रैक्टिस)", "index": e_idx, "tf": f"{e_tf}m",
                                "direction": setup["signal"], "strategy": s_nm, "summary": s_sm,
                                "lots": e_lots, "qty": total_q, "exch": meta["opt_exch"],
                                "legs": ordered, "entry_time": now_str, "sl_rs": -1700.0 * e_lots, "tsl_active": False
                            }
                            ema_save_active(new_tr)
                            st.session_state["ema_active_trade"] = new_tr
                            mark_strategy_traded_today("ema", today_str)
                            send_telegram_alert(f"🚀 <b>16 EMA PAPER AUTO-ENTRY ({e_idx})</b>\n{s_sm}\nTime: {now_str}")
            except Exception:
                pass

    # 3. Smart 4-Layer Pro Algo (10:00 AM Auto Paper)
    if cfg.get("pro_auto") and not is_strategy_traded_today("pro", today_str):
        if now_ist.hour == 10 and now_ist.minute <= 3:
            try:
                from pages.pro_algo import (
                    fetch_intraday_vwap_orb,
                    run_4layer_decision_engine,
                    build_pro_legs,
                    load_pro_active,
                    save_pro_active,
                )
                if not load_pro_active():
                    p_idx = st.session_state.get("pro_idx", "Nifty 50")
                    p_lots = int(st.session_state.get("pro_lots", 1))
                    meta = get_index_meta(p_idx)
                    df_oc, spot, exp_s, dte_v, cmap, oi_ok, _, _ = fetch_angel_live_oc(jwt, akey, p_idx, 6)
                    if df_oc is not None and oi_ok and spot:
                        orb_d = fetch_intraday_vwap_orb(jwt, akey, p_idx, spot)
                        g_inf = evaluate_greeks_regime(p_idx, spot, df_oc, exp_s, oi_valid=True)
                        c_data = {
                            "df": df_oc, "spot": spot, "atm": round(spot / meta["step"]) * meta["step"],
                            "exp_str": exp_s, "dte": dte_v, "cmap": cmap, "info": meta, "oi_valid": True
                        }
                        verdict = run_4layer_decision_engine(c_data, orb_d, g_inf)
                        if verdict["decision"] in ["UP", "DOWN", "SIDEWAYS"]:
                            s_nm, p_lgs, net_cr = build_pro_legs(verdict["decision"], c_data)
                            total_q = meta["lot"] * p_lots
                            max_cr_rs = round(net_cr * total_q, 0)
                            hard_sl = max(1200.0 * p_lots, round(max_cr_rs * 0.38, 0))
                            ordered = [x for x in p_lgs if x["action"] == "BUY"] + [x for x in p_lgs if x["action"] == "SELL"]
                            for lg in ordered:
                                ci = cmap.get((lg["strike"], lg["side"]), {"symbol": "", "token": "0"})
                                lg["symbol"], lg["token"] = ci["symbol"], ci["token"]
                            new_pro = {
                                "mode": "🟡 Paper Trading", "index": p_idx, "strategy": s_nm, "score": verdict["score"],
                                "lots": p_lots, "qty": total_q, "exch": meta["opt_exch"], "legs": ordered,
                                "entry_time": now_str, "max_credit_rs": max_cr_rs, "hard_sl_rs": hard_sl, "profit_locked": False
                            }
                            save_pro_active(new_pro)
                            st.session_state["pro_active"] = new_pro
                            mark_strategy_traded_today("pro", today_str)
                            send_telegram_alert(f"🚀 <b>PRO ALGO PAPER AUTO-ENTRY ({p_idx})</b>\n{s_nm}\nTime: {now_str}")
            except Exception:
                pass


# ==========================================
# Persistent Trade History & Active Trade
# ==========================================
HISTORY_FILE = "paper_trades_history.csv"
ACTIVE_TRADE_FILE = "active_trade_state.json"


def load_trade_history():
    raw_csv, _, status = gh_read_text(HISTORY_FILE)
    if status == "OK" and raw_csv is not None:
        st.session_state["gh_sync_status"] = "☁️ GitHub Synced ✅"
        try:
            df = pd.read_csv(StringIO(raw_csv))
            df.to_csv(HISTORY_FILE, index=False)
            return df.to_dict("records")
        except Exception:
            return []
    st.session_state["gh_sync_status"] = "☁️ GitHub Ready ✅"
    if os.path.exists(HISTORY_FILE):
        try:
            return pd.read_csv(HISTORY_FILE).to_dict("records")
        except Exception:
            return []
    return []


def save_trade_history(history_list):
    df = pd.DataFrame(history_list)
    df.to_csv(HISTORY_FILE, index=False)
    csv_text = df.to_csv(index=False) if not df.empty else "तारीख,मोड,इंडेक्स,स्प्रेड,लॉट,एंट्री समय,एग्जिट समय,कारण,लाभ / हानि (₹)\n"
    ok, msg = gh_write_text(HISTORY_FILE, csv_text, "Update trade history")
    st.session_state["gh_sync_status"] = f"☁️ {msg}" if ok else f"⚠️ {msg}"


def load_active_trade():
    raw_json, _, status = gh_read_text(ACTIVE_TRADE_FILE)
    if status == "OK" and raw_json:
        try:
            data = json.loads(raw_json)
            if isinstance(data, dict) and (data.get("legs") or data.get("sell_strike")):
                return data
        except Exception:
            pass
    return None


def save_active_trade(trade_dict):
    text_data = json.dumps(trade_dict if trade_dict else {}, ensure_ascii=False)
    gh_write_text(ACTIVE_TRADE_FILE, text_data, "Update active trade state")


def build_strategy_plan(direction, selected_idx, atm_strike, hedge_pts, full_df):
    def _get_ltp(strike_val, side_type):
        row = full_df[full_df["STRIKE"] == strike_val]
        col = "CALL LTP" if side_type == "CE" else "PUT LTP"
        return float(row[col].iloc[0]) if not row.empty else 0.0

    if direction == "UP":
        buy_k, sell_k = atm_strike - hedge_pts, atm_strike
        legs = [
            {"action": "BUY", "strike": buy_k, "side": "PE", "entry_ltp": _get_ltp(buy_k, "PE")},
            {"action": "SELL", "strike": sell_k, "side": "PE", "entry_ltp": _get_ltp(sell_k, "PE")},
        ]
        strat_name = "Bull Put Spread (तेज़ी का ट्रेड)"
        summary = f"Sell {sell_k} PE / Buy {buy_k} PE"
    elif direction == "DOWN":
        buy_k, sell_k = atm_strike + hedge_pts, atm_strike
        legs = [
            {"action": "BUY", "strike": buy_k, "side": "CE", "entry_ltp": _get_ltp(buy_k, "CE")},
            {"action": "SELL", "strike": sell_k, "side": "CE", "entry_ltp": _get_ltp(sell_k, "CE")},
        ]
        strat_name = "Bear Call Spread (मंदी का ट्रेड)"
        summary = f"Sell {sell_k} CE / Buy {buy_k} CE"
    else:
        buy_ce, buy_pe = atm_strike + hedge_pts, atm_strike - hedge_pts
        legs = [
            {"action": "BUY", "strike": buy_ce, "side": "CE", "entry_ltp": _get_ltp(buy_ce, "CE")},
            {"action": "BUY", "strike": buy_pe, "side": "PE", "entry_ltp": _get_ltp(buy_pe, "PE")},
            {"action": "SELL", "strike": atm_strike, "side": "CE", "entry_ltp": _get_ltp(atm_strike, "CE")},
            {"action": "SELL", "strike": atm_strike, "side": "PE", "entry_ltp": _get_ltp(atm_strike, "PE")},
        ]
        strat_name = "Straddle with Hedge / Iron Fly (साइडवेज़ ट्रेड)"
        summary = f"Straddle {atm_strike} CE+PE Sell / Hedge {buy_ce} CE & {buy_pe} PE Buy"

    net_credit_pts = sum(lg["entry_ltp"] if lg["action"] == "SELL" else -lg["entry_ltp"] for lg in legs)
    return strat_name, summary, legs, round(net_credit_pts, 2)


def execute_multi_leg_entry(mode, selected_idx, strat_name, summary, legs, lots, lot_qty, contract_map, trigger_src="Manual"):
    ist = pytz.timezone("Asia/Kolkata")
    now_str = datetime.now(ist).strftime("%I:%M:%S %p")
    total_qty = lots * lot_qty
    exch = "BFO" if selected_idx == "Sensex" else "NFO"

    # शून्य भाव (0 LTP) वाले अधूरे लेग्स पर एंट्री रोकें
    if any(float(lg.get("entry_ltp", 0)) <= 0 for lg in legs):
        return False, "किसी स्ट्राइक का लाइव भाव (LTP) 0 आ रहा है — सुरक्षा हेतु एंट्री रोकी गई।"

    enriched_legs = []
    for lg in legs:
        c_info = contract_map.get((lg["strike"], lg["side"]), {"symbol": "", "token": "0", "exch": exch})
        enriched_legs.append({
            "action": lg["action"], "strike": lg["strike"], "side": lg["side"],
            "entry_ltp": lg["entry_ltp"], "last_valid_ltp": lg["entry_ltp"],
            "symbol": c_info["symbol"], "token": c_info["token"], "exch": c_info["exch"]
        })

    ordered_legs = [x for x in enriched_legs if x["action"] == "BUY"] + [x for x in enriched_legs if x["action"] == "SELL"]

    if "LIVE ALGO" in mode:
        jwt, akey = ensure_angel_session()
        if not jwt:
            return False, "Angel One लॉगिन नहीं है!"
        executed_so_far = []
        for lg in ordered_legs:
            if not lg["token"] or lg["token"] == "0":
                return False, f"असली कॉन्ट्रैक्ट टोकन नहीं मिला ({lg['strike']} {lg['side']})"
            ok_ord, msg_ord = place_angel_order(jwt, akey, lg["exch"], lg["symbol"], lg["token"], lg["action"], total_qty)
            if not ok_ord:
                for prev in reversed(executed_so_far):
                    rev_act = "SELL" if prev["action"] == "BUY" else "BUY"
                    place_angel_order(jwt, akey, prev["exch"], prev["symbol"], prev["token"], rev_act, total_qty)
                send_telegram_alert(f"⚠️ <b>ORDER REJECTED ({selected_idx})</b>\n{lg['action']} {lg['strike']} {lg['side']} विफल: {msg_ord}")
                return False, f"ऑर्डर विफल (रोलबैक पूरा): {msg_ord}"
            executed_so_far.append(lg)
            time.sleep(0.35)

    sl_limit = 1700.0 * lots
    new_trade = {
        "mode": mode, "index": selected_idx, "strategy": strat_name, "summary": summary,
        "lots": lots, "qty": total_qty, "exch": exch, "legs": ordered_legs,
        "entry_time": now_str, "sl_rs": sl_limit
    }
    st.session_state["active_trade"] = new_trade
    save_active_trade(new_trade)

    mode_tag = "🟡 PAPER TRADE" if "PAPER" in mode else "🔴 LIVE ALGO TRADE"
    legs_lines = "\n".join([f"• <b>{l['action']}</b> {l['strike']} {l['side']} @ ₹{l['entry_ltp']}" for l in ordered_legs])
    send_telegram_alert(
        f"🚀 <b>{mode_tag} EXECUTED ({trigger_src})</b>\n📊 <b>Index:</b> {selected_idx}\n"
        f"🧭 <b>Strategy:</b> {strat_name}\n{legs_lines}\n📦 <b>Qty:</b> {lots} Lot ({total_qty} Qty)\n"
        f"🛑 <b>Auto SL:</b> -₹{int(sl_limit):,} | ⏰ {now_str}"
    )
    return True, f"सफल! {strat_name} एग्जीक्यूट हो गया।"


def close_active_trade(curr_ltp_by_idx, reason="Manual Exit"):
    tr = st.session_state.get("active_trade")
    if not tr:
        return False

    ist = pytz.timezone("Asia/Kolkata")
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    exit_time = datetime.now(ist).strftime("%I:%M:%S %p")
    legs = tr.get("legs", [])

    if "LIVE ALGO" in tr["mode"]:
        jwt, akey = ensure_angel_session()
        if not jwt:
            send_telegram_alert("🚨 <b>EXIT FAILED:</b> Angel One सत्र समाप्त है, कृपया तुरंत मैनुअल स्क्वेयर-ऑफ करें!")
            return False
        sell_legs = [l for l in legs if l["action"] == "SELL"]
        buy_legs = [l for l in legs if l["action"] == "BUY"]
        for lg in sell_legs:
            ok_x, err_x = place_angel_order(jwt, akey, lg["exch"], lg["symbol"], lg["token"], "BUY", tr["qty"])
            if not ok_x:
                send_telegram_alert(f"🚨 <b>CRITICAL EXIT FAILED ({lg['symbol']}):</b> {err_x} — तुरंत ब्रोकर ऐप से बंद करें!")
                return False
            time.sleep(0.35)
        for lg in buy_legs:
            ok_x, err_x = place_angel_order(jwt, akey, lg["exch"], lg["symbol"], lg["token"], "SELL", tr["qty"])
            if not ok_x:
                send_telegram_alert(f"⚠️ <b>HEDGE EXIT FAILED ({lg['symbol']}):</b> {err_x}")
            time.sleep(0.35)

    pnl_pts = 0.0
    for i, lg in enumerate(legs):
        now_px = curr_ltp_by_idx.get(i, lg.get("last_valid_ltp", lg["entry_ltp"]))
        pnl_pts += (lg["entry_ltp"] - now_px) if lg["action"] == "SELL" else (now_px - lg["entry_ltp"])
    pnl_rs = round(pnl_pts * tr["qty"], 2)

    if "PAPER" in tr["mode"]:
        st.session_state["paper_capital"] += pnl_rs

    mode_short = "Paper" if "PAPER" in tr["mode"] else "LIVE"
    st.session_state["trade_history"].insert(0, {
        "तारीख": today_str, "मोड": mode_short, "इंडेक्स": tr["index"],
        "स्प्रेड": tr.get("summary", tr.get("strategy", "Spread")),
        "लॉट": tr["lots"], "एंट्री समय": tr["entry_time"], "एग्जिट समय": exit_time,
        "कारण": reason, "लाभ / हानि (₹)": pnl_rs
    })
    save_trade_history(st.session_state["trade_history"])
    save_active_trade(None)
    st.session_state["active_trade"] = None

    send_telegram_alert(
        f"🏁 <b>TRADE CLOSED ({mode_short})</b>\n📊 <b>Index:</b> {tr['index']}\n"
        f"📝 <b>Reason:</b> {reason}\n💵 <b>Final P&L:</b> <b>₹{pnl_rs:+,.0f}</b> | ⏰ {exit_time}"
    )
    return True


# ==========================================
# Session Init & Mode Router
# ==========================================
jwt_glob, akey_glob = ensure_angel_session()
run_background_paper_engines(jwt_glob, akey_glob)

if "gh_sync_status" not in st.session_state:
    st.session_state["gh_sync_status"] = "Checking..."
if "trade_history" not in st.session_state:
    st.session_state["trade_history"] = load_trade_history()
if "paper_capital" not in st.session_state:
    saved_pnl = sum(float(t.get("लाभ / हानि (₹)", 0)) for t in st.session_state["trade_history"] if t.get("मोड") == "Paper")
    st.session_state["paper_capital"] = 200000.0 + saved_pnl
if "active_trade" not in st.session_state:
    st.session_state["active_trade"] = load_active_trade()

mode = st.radio(
    "Select Mode:",
    ["🚀 Live/Paper Algo Terminal", "📈 16 EMA + OI Algo", "🧠 Smart 4-Layer Pro Algo", "📊 Manual Options Simulator"],
    horizontal=True
)
if mode == "📊 Manual Options Simulator":
    exec(open("pages/simulator.py", encoding="utf-8").read())
    st.stop()
elif mode == "🧠 Smart 4-Layer Pro Algo":
    exec(open("pages/pro_algo.py", encoding="utf-8").read())
    st.stop()
elif mode == "📈 16 EMA + OI Algo":
    exec(open("pages/ema16_oi_algo.py", encoding="utf-8").read())
    st.stop()

st.title("🛡️ ऑप्शन सेलर एल्गो टर्मिनल (2-Way Telegram + Multi-Algo Auto Paper)")

# तीनों रणनीतियों के स्थायी टॉगल बार (पेज बदलने पर भी कभी रीसेट नहीं होंगे)
cfg_now = load_algo_settings()
st.markdown("#### ⚙️ स्थायी ऑटो-पेपर-ट्रेड कंट्रोल (पेज बदलने पर भी चालू रहेंगे)")
tg_c1, tg_c2, tg_c3 = st.columns(3)
with tg_c1:
    t_main = st.toggle("1️⃣ Main Terminal (10:00 AM Auto)", value=bool(cfg_now.get("main_auto_10am")), key="pers_main_tog")
    set_toggle_setting("main_auto_10am", t_main)
with tg_c2:
    t_ema = st.toggle("2️⃣ 16 EMA + OI (9:45–12:00 Auto)", value=bool(cfg_now.get("ema_auto_arm")), key="pers_ema_tog")
    set_toggle_setting("ema_auto_arm", t_ema)
with tg_c3:
    t_pro = st.toggle("3️⃣ Smart 4-Layer Pro (10:00 AM Auto)", value=bool(cfg_now.get("pro_auto")), key="pers_pro_tog")
    set_toggle_setting("pro_auto", t_pro)

tab1, tab2, tab3 = st.tabs([
    "🤖 Paper & Live Algo ट्रेडिंग",
    "⛓️ लाइव ऑप्शन चेन (Angel One)",
    "📊 10:00 AM बैकटेस्ट (सही DTE)"
])

with tab1:
    m_col1, m_col2, m_col3 = st.columns([2, 1, 1])
    with m_col1:
        trade_mode = st.radio(
            "ट्रेडिंग मोड चुनें:",
            ["🟡 PAPER TRADING (वर्चुअल प्रैक्टिस)", "🔴 LIVE ALGO (असली पैसा)"],
            horizontal=True
        )
    with m_col2:
        algo_idx = st.selectbox("ट्रेडिंग इंडेक्स", ["Nifty 50", "Bank Nifty", "Sensex"], key="algo_idx")
    with m_col3:
        algo_lots = st.number_input("लॉट (Lots)", min_value=1, max_value=25, value=1, key="algo_lots")

    live_unlocked = True
    if "LIVE ALGO" in trade_mode:
        st.warning("⚠️ **सावधान:** LIVE ALGO मोड में आपके Angel One अकाउंट से असली पैसे के ऑर्डर लगेंगे।")
        live_unlocked = st.checkbox("✅ मैं पुष्टि करता हूँ कि असली पैसे से ऑर्डर लगाए जाएं (Confirm Live Trading)")

    st.info(f"💰 **पेपर बैलेंस:** `₹{st.session_state['paper_capital']:,.0f}` ({st.session_state['gh_sync_status']}) | 🛑 **Auto SL:** `₹{1700 * algo_lots:,.0f}` (`₹1,700/Lot`)")

    @st.fragment(run_every=10)
    def render_algo_dashboard(mode_sel, selected_idx, lots_cnt, is_unlocked):
        ist = pytz.timezone("Asia/Kolkata")
        now_ist = datetime.now(ist)
        now_str = now_ist.strftime("%I:%M:%S %p")
        today_str = now_ist.strftime("%Y-%m-%d")

        jwt, akey = ensure_angel_session()
        run_background_paper_engines(jwt, akey)
        tg_cmd = check_telegram_commands()

        meta = get_index_meta(selected_idx)
        step, hedge_pts, lot_qty = meta["step"], meta["hedge"], meta["lot"]

        if not jwt or not akey:
            st.error("🚨 Angel One SmartAPI लॉगिन नहीं है! सुरक्षा नियम के अनुसार बिना लाइव डेटा के ट्रेड बंद है।")
            return

        full_df, spot_price, expiry_label, trading_dte, contract_map, oi_valid, oi_source, err = fetch_angel_live_oc(
            jwt, akey, selected_idx, 5
        )
        if full_df is None or full_df.empty or not spot_price:
            st.error(f"⚠️ लाइव डेटा रुका हुआ है ({err}) — बिना असली डेटा के कोई ट्रेड या सिग्नल नहीं चलेगा।")
            return

        atm_strike = round(spot_price / step) * step
        g_info = evaluate_greeks_regime(selected_idx, spot_price, full_df, expiry_label, oi_valid=oi_valid)
        market_dir = g_info.get("direction", "WAIT")

        up_3_df = full_df[(full_df["STRIKE"] > atm_strike) & (full_df["STRIKE"] <= atm_strike + 3 * step)]
        down_3_df = full_df[(full_df["STRIKE"] < atm_strike) & (full_df["STRIKE"] >= atm_strike - 3 * step)]
        up_3_call_chg = int(up_3_df["CALL CHG OI"].sum()) if (oi_valid and not up_3_df.empty) else 0
        down_3_put_chg = int(down_3_df["PUT CHG OI"].sum()) if (oi_valid and not down_3_df.empty) else 0

        active_tr = st.session_state.get("active_trade")
        if active_tr is not None:
            st.markdown("### 🔥 चालू पोजीशन (Live MTM & Telegram Remote Control)")
            legs = active_tr.get("legs", [])
            tokens_tup = tuple(l["token"] for l in legs if l.get("token") and l["token"] != "0")
            ltp_map = get_live_ltp_for_tokens(jwt, akey, active_tr["exch"], tokens_tup) if tokens_tup else {}

            curr_ltp_by_idx = {}
            live_pnl_pts = 0.0
            missing_ltp = False
            for i, lg in enumerate(legs):
                if lg["token"] in ltp_map and ltp_map[lg["token"]] > 0:
                    c_px = ltp_map[lg["token"]]
                    lg["last_valid_ltp"] = c_px
                else:
                    # कभी भी random नंबर न जोड़ें — आखिरी असली भाव ही रखें
                    c_px = float(lg.get("last_valid_ltp", lg["entry_ltp"]))
                    missing_ltp = True
                curr_ltp_by_idx[i] = c_px
                live_pnl_pts += (lg["entry_ltp"] - c_px) if lg["action"] == "SELL" else (c_px - lg["entry_ltp"])

            if missing_ltp:
                st.warning("⚠️ एक या अधिक लेग का ताज़ा LTP अभी नहीं मिला — आखिरी असली भाव पर MTM दिख रहा है।")

            live_pnl_rs = round(live_pnl_pts * active_tr["qty"], 2)

            if tg_cmd == "STATUS":
                send_telegram_alert(f"📊 <b>LIVE STATUS ({active_tr['index']})</b>\nMTM: <b>₹{live_pnl_rs:+,.0f}</b> | SL: -₹{int(active_tr['sl_rs']):,}")

            if tg_cmd == "EXIT":
                if close_active_trade(curr_ltp_by_idx, reason="Telegram /exit Command 📲"):
                    st.rerun()
            elif live_pnl_rs <= -active_tr["sl_rs"]:
                if close_active_trade(curr_ltp_by_idx, reason=f"Auto SL Hit (-₹{int(active_tr['sl_rs'])})"):
                    st.rerun()
            elif now_ist.hour == 15 and now_ist.minute >= 15:
                if close_active_trade(curr_ltp_by_idx, reason="3:15 PM Auto Square-Off"):
                    st.rerun()

            m1, m2, m3 = st.columns(3)
            m1.metric("मोड & इंडेक्स", f"{active_tr['index']}", active_tr.get("strategy", ""))
            m2.metric("मात्रा (Qty)", f"{active_tr['lots']} Lot", f"{active_tr['qty']} Qty")
            m3.metric("लाइव MTM लाभ / हानि", f"₹{live_pnl_rs:+,.0f}", f"Auto SL: -₹{int(active_tr['sl_rs']):,}")

            leg_cols = st.columns(len(legs))
            for i, lg in enumerate(legs):
                c_px = curr_ltp_by_idx[i]
                leg_pnl = round(((lg["entry_ltp"] - c_px) if lg["action"] == "SELL" else (c_px - lg["entry_ltp"])) * active_tr["qty"], 0)
                leg_cols[i].metric(f"{lg['action']} {lg['strike']} {lg['side']}", f"₹{c_px}", f"Entry: ₹{lg['entry_ltp']} | ₹{leg_pnl:+,.0f}")

            if st.button("🚨 सभी लेग्स अभी बंद करें (Square-Off All)", type="primary", use_container_width=True):
                if close_active_trade(curr_ltp_by_idx, reason="Manual Square-Off"):
                    st.rerun()
        else:
            oi_disp = f"ऊपर 3 Call Chg OI: <b>{up_3_call_chg:+,}</b> vs नीचे 3 Put Chg OI: <b>{down_3_put_chg:+,}</b>" if oi_valid else "<b>Change in OI: N/A (कल का क्लोज़ OI उपलब्ध नहीं)</b>"
            st.markdown(f"""
            <div style="background: {g_info.get('bg_color', 'linear-gradient(90deg, #1e293b, #0f172a)')}; padding: 16px 20px; border-radius: 12px; border-left: 6px solid {g_info.get('border_color', '#38bdf8')}; color: white; margin-bottom: 12px;">
                <div style="font-size: 13px; color: #e2e8f0;">⏱️ {now_str} | 📍 {selected_idx} Spot: <b>{spot_price:,.2f}</b> | 📅 Expiry: <b>{expiry_label} ({trading_dte} DTE)</b> | 📦 Lot: <b>{lot_qty}</b> | 🛡️ Hedge: <b>{hedge_pts} pts</b></div>
                <div style="font-size: 13px; color: #cbd5e1; margin-top: 4px;">📊 {oi_disp} &nbsp;|&nbsp; स्रोत: <i>{oi_source}</i></div>
                <div style="font-size: 21px; font-weight: bold; margin: 6px 0;">{g_info.get('headline', '⏳ प्रतीक्षा')}</div>
                <div style="font-size: 15px; color: #fde047;">👉 {g_info.get('trade_plan', '')}</div>
            </div>
            """, unsafe_allow_html=True)

            if market_dir in ["UP", "DOWN", "SIDEWAYS"]:
                strat_name, strat_summary, plan_legs, net_credit_pts = build_strategy_plan(
                    market_dir, selected_idx, atm_strike, hedge_pts, full_df
                )
                est_credit_rs = round(net_credit_pts * lot_qty * lots_cnt, 0)
                st.caption(f"🎯 **तैयार सेटअप:** `{strat_summary}` | अनुमानित नेट क्रेडिट: `₹{est_credit_rs:,.0f}`")

                already_done = is_strategy_traded_today("main", today_str)
                if already_done:
                    st.info("✅ आज का Main Terminal ऑटो-ट्रेड पहले ही लग चुका है (दिन में 1 ट्रेड नियम)।")

                btn_label = f"🟡 1-Click PAPER TRADE — {strat_name} ({lots_cnt} Lot)" if "PAPER" in mode_sel else f"🔴 1-Click LIVE ORDER — {strat_name} ({lots_cnt} Lot)"
                btn_disabled = ("LIVE ALGO" in mode_sel and not is_unlocked)

                if st.button(btn_label, disabled=btn_disabled, use_container_width=True):
                    ok, msg = execute_multi_leg_entry(
                        mode_sel, selected_idx, strat_name, strat_summary, plan_legs,
                        lots_cnt, lot_qty, contract_map, trigger_src="1-Click Manual"
                    )
                    if ok:
                        mark_strategy_traded_today("main", today_str)
                        st.success(f"✅ {msg}")
                        st.rerun()
                    else:
                        st.error(f"❌ {msg}")
            else:
                st.warning("⏳ अभी स्पष्ट सिग्नल नहीं है (WAIT) — गलत एंट्री से बचने के लिए 1-Click बटन होल्ड पर है।")

        if st.session_state["trade_history"]:
            st.markdown("### 📜 ट्रेड बुक (Saved Trade History)")
            st.dataframe(pd.DataFrame(st.session_state["trade_history"]), use_container_width=True, hide_index=True)
            with st.expander("🗑️ हिस्ट्री साफ करें (Confirmation आवश्यक)"):
                confirm_del = st.checkbox("हाँ, मैं पक्का सारी ट्रेड हिस्ट्री मिटाना चाहता हूँ", key="confirm_del_hist")
                if st.button("हिस्ट्री हमेशा के लिए मिटाएं", disabled=not confirm_del):
                    st.session_state["trade_history"] = []
                    st.session_state["paper_capital"] = 200000.0
                    save_trade_history([])
                    st.rerun()

    render_algo_dashboard(trade_mode, algo_idx, algo_lots, live_unlocked)

with tab2:
    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        idx = st.selectbox("इंडेक्स चुनें", ["Nifty 50", "Bank Nifty", "Sensex"], key="live_idx")
    with c2:
        strike_range = st.slider("ATM के ऊपर और नीचे कितनी स्ट्राइक जोड़ें?", min_value=1, max_value=10, value=5)
    with c3:
        auto_refresh = st.toggle("⚡ स्मूथ लाइव रिफ्रेश (10s)", value=True, key="oc_refresh")

    @st.fragment(run_every=(10 if auto_refresh else None))
    def render_seller_chain(selected_idx, s_range):
        ist = pytz.timezone("Asia/Kolkata")
        now_str = datetime.now(ist).strftime("%I:%M:%S %p")
        jwt, akey = ensure_angel_session()
        if not jwt:
            st.error("🚨 Angel One लॉगिन सक्रिय नहीं है।")
            return

        full_df, spot_price, expiry_label, trading_dte, _, oi_valid, oi_source, err = fetch_angel_live_oc(
            jwt, akey, selected_idx, s_range
        )
        if full_df is None or full_df.empty:
            st.warning(f"⚠️ लाइव ऑप्शन चेन उपलब्ध नहीं है: {err}")
            return

        meta = get_index_meta(selected_idx)
        step = meta["step"]
        atm_strike = round(spot_price / step) * step
        filtered_df = full_df[(full_df["STRIKE"] >= atm_strike - s_range * step) & (full_df["STRIKE"] <= atm_strike + s_range * step)].copy()

        up_3_df = full_df[(full_df["STRIKE"] > atm_strike) & (full_df["STRIKE"] <= atm_strike + 3 * step)]
        down_3_df = full_df[(full_df["STRIKE"] < atm_strike) & (full_df["STRIKE"] >= atm_strike - 3 * step)]
        up_3_call_chg = int(up_3_df["CALL CHG OI"].sum()) if (oi_valid and not up_3_df.empty) else 0
        down_3_put_chg = int(down_3_df["PUT CHG OI"].sum()) if (oi_valid and not down_3_df.empty) else 0

        st.caption(
            f"🟢 **ANGEL ONE LIVE** | 📅 **Expiry:** `{expiry_label}` (**`{trading_dte} DTE`**) | ⏱️ **अपडेट:** `{now_str}` | 📍 **Spot:** `{spot_price:,.2f}` | 🎯 **ATM:** `{atm_strike}` | 📊 **OI स्रोत:** `{oi_source}`"
        )
        if oi_valid:
            st.markdown(f"📊 **ऊपर की 3 स्ट्राइक कुल Call Chg OI:** `{up_3_call_chg:+,}` &nbsp;&nbsp;|&nbsp;&nbsp; **नीचे की 3 स्ट्राइक कुल Put Chg OI:** `{down_3_put_chg:+,}`")
        else:
            st.warning("⚠️ कल का क्लोज़िंग OI उपलब्ध नहीं होने के कारण Change in OI `N/A` पर है।")

        iv_info = evaluate_otm_iv_from_df(selected_idx, spot_price, full_df, expiry_label, oi_valid=oi_valid)
        iv_c1, iv_c2, iv_c3 = st.columns(3)
        iv_c1.metric("Top-3 OTM Call IV", f"{iv_info['avg_otm_ce_iv']}%", f"{iv_info['ce_iv_change']:+}%", delta_color="inverse")
        iv_c2.metric("Bottom-3 OTM Put IV", f"{iv_info['avg_otm_pe_iv']}%", f"{iv_info['pe_iv_change']:+}%", delta_color="inverse")
        iv_c3.metric("IV ट्रेंड सिग्नल", iv_info["signal"])
        st.info(f"**{iv_info['spread_name']}** — {iv_info['message']}")

        safe_call_k = filtered_df.loc[filtered_df["CALL OI"].idxmax(), "STRIKE"]
        safe_put_k = filtered_df.loc[filtered_df["PUT OI"].idxmax(), "STRIKE"]

        def style_chain(row):
            styles = [""] * len(row)
            for i, col in enumerate(row.index):
                if col == "STRIKE":
                    if row["STRIKE"] == atm_strike:
                        styles[i] = "background-color: #ffd700; color: black; font-weight: bold; text-align: center;"
                    elif row["STRIKE"] == safe_call_k:
                        styles[i] = "background-color: #ff5252; color: white; font-weight: bold; text-align: center;"
                    elif row["STRIKE"] == safe_put_k:
                        styles[i] = "background-color: #00c853; color: white; font-weight: bold; text-align: center;"
                    else:
                        styles[i] = "background-color: #2b303a; color: #ffffff; font-weight: bold; text-align: center;"
                elif "CHG OI" in col and oi_valid:
                    val = row[col]
                    styles[i] = "color: #00e676; font-weight: bold;" if val > 0 else ("color: #ff5252; font-weight: bold;" if val < 0 else "")
            return styles

        fmt_dict = {
            "CALL VOL": "{:,}", "CALL OI": "{:,}", "CALL LTP": "₹{:.1f}",
            "PUT LTP": "₹{:.1f}", "PUT OI": "{:,}", "PUT VOL": "{:,}",
            "CALL CHG OI": ("{:+,}" if oi_valid else lambda _: "N/A"),
            "PUT CHG OI": ("{:+,}" if oi_valid else lambda _: "N/A"),
        }
        st.dataframe(filtered_df.style.apply(style_chain, axis=1).format(fmt_dict), use_container_width=True, hide_index=True, height=430)

    render_seller_chain(idx, strike_range)

with tab3:
    st.subheader("📊 10:00 AM बैकटेस्ट (छुट्टियों के अनुसार सही 0 DTE / 1 DTE सहित)")
    st.info("ℹ️ नोट: यह बैकटेस्ट स्पॉट कैंडल व Black-Scholes मॉडल पर चलता है (नकली OI के बजाय सुबह के प्राइस मोमेंटम पर)। इसमें मंगलवार (Nifty) और गुरुवार (Sensex) की एक्सपायरी + छुट्टी के दिन एक दिन पहले शिफ्ट होने का सही कैलेंडर लगा है।")

    b1, b2, b3, b4 = st.columns(4)
    with b1:
        bt_idx = st.selectbox("इंडेक्स चुनें", ["Nifty 50", "Bank Nifty", "Sensex"], key="bt_idx")
    with b2:
        bt_days = st.selectbox("कितने समय का बैकटेस्ट?", ["30 दिन (1 महीना)", "90 दिन (3 महीने)", "180 दिन (6 महीने)", "365 दिन (1 साल)"], index=1)
    with b3:
        lots = st.number_input("लॉट साइज (Lots)", min_value=1, max_value=50, value=1, key="bt_lots")
    with b4:
        deduct_costs = st.checkbox("ब्रोकरेज + टैक्स + स्लिपेज घटाएं", value=True)

    dte_options = ["0 DTE (Expiry Day)", "1 DTE", "2 DTE", "3 DTE", "4 DTE"]
    selected_dtes = st.multiselect("📅 DTE चुनें:", options=dte_options, default=dte_options, key="bt_dte_select")
    allowed_dte_nums = [int(x.split(" ")[0]) for x in selected_dtes]

    if st.button("🚀 बैकटेस्ट चलाएं", use_container_width=True):
        jwt, akey = ensure_angel_session()
        if not jwt:
            st.error("Angel One लॉगिन आवश्यक है।")
        elif not allowed_dte_nums:
            st.warning("कम से कम एक DTE चुनें।")
        else:
            meta = get_index_meta(bt_idx)
            step, lot_qty, hedge_points = meta["step"], meta["lot"], meta["hedge"]
            total_qty = lot_qty * lots
            total_sl_rs = 1700.0 * lots
            slip_mult = 1.0 if deduct_costs else 0.0
            days_num = int(bt_days.split(" ")[0])

            headers = {
                "Authorization": f"Bearer {jwt}", "Content-Type": "application/json", "Accept": "application/json",
                "X-UserType": "USER", "X-SourceID": "WEB",
                "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
                "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": akey
            }
            ist = pytz.timezone("Asia/Kolkata")
            end_dt = datetime.now(ist)
            rem = days_num
            all_c = []
            with st.spinner("1-मिनट डेटा लोड हो रहा है..."):
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
                        all_c.extend(r["data"])
                    time.sleep(0.15)
                    end_dt = st_dt - timedelta(minutes=1)
                    rem -= ch

            if not all_c:
                st.error("डेटा नहीं मिला।")
            else:
                df = pd.DataFrame(all_c, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
                df["Timestamp"] = pd.to_datetime(df["Timestamp"])
                if df["Timestamp"].dt.tz is None:
                    df["Timestamp"] = df["Timestamp"].dt.tz_localize("Asia/Kolkata")
                else:
                    df["Timestamp"] = df["Timestamp"].dt.tz_convert("Asia/Kolkata")
                df = df.drop_duplicates(subset=["Timestamp"]).sort_values("Timestamp").set_index("Timestamp")
                df["Date"] = df.index.date

                dte_calendar = build_backtest_dte_map(sorted(df["Date"].unique()), bt_idx)
                trade_logs = []

                def _bs(S, K, T, iv, side):
                    if T <= 1e-6:
                        return max(0.05, S - K) if side == "CE" else max(0.05, K - S)
                    r_rate = 0.065
                    d1 = (math.log(S / K) + (r_rate + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
                    d2 = d1 - iv * math.sqrt(T)
                    nd1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
                    nd2 = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
                    return max(0.5, (S * nd1 - K * math.exp(-r_rate * T) * nd2) if side == "CE" else (K * math.exp(-r_rate * T) * (1.0 - nd2) - S * (1.0 - nd1)))

                for dt, day_df in df.groupby("Date"):
                    days_to_exp = dte_calendar.get(dt, 1)
                    if days_to_exp not in allowed_dte_nums:
                        continue
                    morn = day_df.between_time("09:15", "09:59")
                    sess = day_df.between_time("10:00", "15:15")
                    if len(morn) < 5 or len(sess) < 15:
                        continue

                    open_915 = float(morn["Open"].iloc[0])
                    spot_1000 = float(sess["Open"].iloc[0])
                    atm_1000 = round(spot_1000 / step) * step
                    move_pct = ((spot_1000 - open_915) / open_915) * 100.0
                    m_dir = "UP" if move_pct >= 0.25 else ("DOWN" if move_pct <= -0.25 else "SIDEWAYS")

                    rng_pct = max(0.0025, (float(morn["High"].max()) - float(morn["Low"].min())) / spot_1000)
                    daily_iv = min(0.30, max(0.12, rng_pct * 16.0))
                    t_entry = (days_to_exp + (315.0 / 375.0)) / 365.0

                    if m_dir == "UP":
                        s_name = "Bull Put Spread"
                        legs_bt = [
                            {"action": "BUY", "strike": atm_1000 - hedge_points, "side": "PE"},
                            {"action": "SELL", "strike": atm_1000, "side": "PE"},
                        ]
                    elif m_dir == "DOWN":
                        s_name = "Bear Call Spread"
                        legs_bt = [
                            {"action": "BUY", "strike": atm_1000 + hedge_points, "side": "CE"},
                            {"action": "SELL", "strike": atm_1000, "side": "CE"},
                        ]
                    else:
                        s_name = "Straddle + Hedge"
                        legs_bt = [
                            {"action": "BUY", "strike": atm_1000 + hedge_points, "side": "CE"},
                            {"action": "BUY", "strike": atm_1000 - hedge_points, "side": "PE"},
                            {"action": "SELL", "strike": atm_1000, "side": "CE"},
                            {"action": "SELL", "strike": atm_1000, "side": "PE"},
                        ]

                    for lg in legs_bt:
                        raw_p = _bs(spot_1000, lg["strike"], t_entry, daily_iv, lg["side"])
                        lg["ent"] = raw_p - (max(0.4, raw_p * 0.004) * slip_mult) if lg["action"] == "SELL" else raw_p + (max(0.4, raw_p * 0.012) * slip_mult)
                        lg["ext"] = lg["ent"]

                    exit_status = "3:15 Exit ✅"
                    n_bars = len(sess)
                    for idx_b, (ts, row) in enumerate(sess.iterrows()):
                        t_curr = (days_to_exp + max(15.0, 315.0 * (1.0 - idx_b / max(1, n_bars - 1))) / 375.0) / 365.0
                        worst_mtm = 0.0
                        worst_px = {}
                        for csp in [float(row["Low"]), float(row["High"])]:
                            pts = 0.0
                            tmp = {}
                            for il, lg in enumerate(legs_bt):
                                cp = _bs(csp, lg["strike"], t_curr, daily_iv, lg["side"])
                                tmp[il] = cp
                                pts += (lg["ent"] - cp) if lg["action"] == "SELL" else (cp - lg["ent"])
                            if pts * total_qty < worst_mtm:
                                worst_mtm = pts * total_qty
                                worst_px = tmp
                        if worst_mtm <= -total_sl_rs:
                            exit_status = f"SL Hit ❌ ({ts.strftime('%I:%M %p')})"
                            for il, lg in enumerate(legs_bt):
                                wp = worst_px.get(il, lg["ent"])
                                lg["ext"] = wp + (max(0.4, wp * 0.008) * slip_mult) if lg["action"] == "SELL" else max(0.05, wp - (max(0.4, wp * 0.012) * slip_mult))
                            break

                    if exit_status == "3:15 Exit ✅":
                        csp = float(sess["Close"].iloc[-1])
                        t_cls = (days_to_exp + (15.0 / 375.0)) / 365.0
                        for lg in legs_bt:
                            cp = _bs(csp, lg["strike"], t_cls, daily_iv, lg["side"])
                            lg["ext"] = cp + (max(0.4, cp * 0.004) * slip_mult) if lg["action"] == "SELL" else max(0.05, cp - (max(0.4, cp * 0.012) * slip_mult))

                    gross_rs = sum(((lg["ent"] - lg["ext"]) if lg["action"] == "SELL" else (lg["ext"] - lg["ent"])) * total_qty for lg in legs_bt)
                    costs_rs = (20.0 * len(legs_bt) * 2 + abs(gross_rs) * 0.01) if deduct_costs else 0.0
                    trade_logs.append({
                        "तारीख": str(dt), "दिन": dt.strftime("%a"), "DTE": f"{days_to_exp} DTE",
                        "रणनीति": s_name, "नतीजा": exit_status, "लाभ / हानि (₹)": round(gross_rs - costs_rs, 0)
                    })

                res_df = pd.DataFrame(trade_logs)
                if not res_df.empty:
                    res_df["कुल पूंजी (₹)"] = res_df["लाभ / हानि (₹)"].cumsum()
                    m1, m2, m3 = st.columns(3)
                    m1.metric("कुल लाभ / हानि", f"₹{res_df['लाभ / हानि (₹)'].sum():,.0f}", f"{len(res_df)} दिन")
                    m2.metric("Win Rate", f"{round((len(res_df[res_df['लाभ / हानि (₹)'] > 0]) / len(res_df)) * 100, 1)}%")
                    m3.metric("Lot Size", f"{lot_qty} Qty")
                    st.line_chart(res_df.set_index("तारीख")["कुल पूंजी (₹)"])
                    st.dataframe(res_df, use_container_width=True, hide_index=True)
