import os
import json
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import StringIO
from datetime import datetime
import pytz
import pandas as pd
import requests

from oi_base import (
    ensure_angel_session,
    get_scrip_master_df,
    get_index_meta,
    get_live_expiry_info,
    compute_accurate_chg_oi,
    load_algo_settings,
    is_strategy_traded_today,
    mark_strategy_traded_today,
    get_saved_tg_offset,
    save_tg_offset,
    gh_read_text,
    gh_write_text,
    _get_secret,
)

# ==========================================
# 1. Tiny Web Server (Render Free Plan के लिए)
# ==========================================
WORKER_STATUS = {
    "last_tick": "Starting...",
    "active_trades": 0,
    "logs_saved": 0,
    "last_error": "",
}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, **WORKER_STATUS}, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        return


def start_web_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


# ==========================================
# 2. Telegram & Angel Quote Helpers
# ==========================================
def tg_send(msg):
    try:
        tok = _get_secret("TELEGRAM_BOT_TOKEN", "")
        cid = _get_secret("TELEGRAM_CHAT_ID", "")
        if tok and cid:
            requests.post(
                f"https://api.telegram.org/bot{tok}/sendMessage",
                json={"chat_id": cid, "text": msg, "parse_mode": "HTML"},
                timeout=6,
            )
    except Exception as e:
        WORKER_STATUS["last_error"] = f"tg_send: {e}"


def check_tg_cmd():
    try:
        tok = _get_secret("TELEGRAM_BOT_TOKEN", "")
        cid = _get_secret("TELEGRAM_CHAT_ID", "")
        if not tok or not cid:
            return None
        last_off = get_saved_tg_offset()
        res = requests.get(
            f"https://api.telegram.org/bot{tok}/getUpdates",
            params={"offset": last_off + 1, "timeout": 1},
            timeout=4,
        ).json()
        cmd = None
        max_u = last_off
        now_ts = time.time()
        for u in res.get("result", []):
            uid = int(u.get("update_id", 0))
            if uid > max_u:
                max_u = uid
            m = u.get("message", {})
            if now_ts - float(m.get("date", now_ts)) > 120:
                continue
            if str(m.get("chat", {}).get("id", "")) == str(cid):
                txt = m.get("text", "").strip().upper()
                if txt in ["/EXIT", "EXIT", "STOP"]:
                    cmd = "EXIT"
                elif txt in ["/STATUS", "STATUS", "MTM"]:
                    cmd = "STATUS"
        if max_u > last_off:
            save_tg_offset(max_u)
        return cmd
    except Exception as e:
        WORKER_STATUS["last_error"] = f"check_tg_cmd: {e}"
        return None


def fetch_chain_worker(jwt, akey, selected_idx):
    meta = get_index_meta(selected_idx)
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00",
        "X-PrivateKey": akey,
    }
    q_url = "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/"
    try:
        sp = requests.post(
            q_url,
            json={"mode": "LTP", "exchangeTokens": {meta["exch"]: [meta["token"]]}},
            headers=headers,
            timeout=6,
        ).json()
        if not sp.get("status"):
            return None
        spot = float(sp["data"]["fetched"][0]["ltp"])
        if spot <= 0:
            return None
    except Exception:
        return None

    step = meta["step"]
    atm = round(spot / step) * step
    exp_str, dte, _ = get_live_expiry_info(selected_idx)
    if exp_str == "Unknown":
        return None

    mdf = get_scrip_master_df()
    opt = mdf[
        (mdf["name"] == meta["name"])
        & (mdf["exch_seg"] == meta["opt_exch"])
        & (mdf["instrumenttype"] == "OPTIDX")
    ].copy()
    opt["expiry_dt"] = pd.to_datetime(opt["expiry"], format="%d%b%Y", errors="coerce")
    opt = opt.dropna(subset=["expiry_dt"])
    exp_df = opt[opt["expiry_dt"] == pd.to_datetime(exp_str, format="%d-%b-%Y")].copy()
    exp_df["clean_strike"] = (exp_df["strike"].astype(float) / 100.0).astype(int)
    exp_df["opt_type"] = exp_df["symbol"].str[-2:]

    span = meta["hedge"] + (5 * step)
    sub = exp_df[(exp_df["clean_strike"] >= atm - span) & (exp_df["clean_strike"] <= atm + span)].copy()
    tokens = sub["token"].astype(str).tolist()
    cmap = {
        (int(r["clean_strike"]), r["opt_type"]): {
            "symbol": r["symbol"],
            "token": str(r["token"]),
            "exch": meta["opt_exch"],
        }
        for _, r in sub.iterrows()
    }

    fetched = {}
    for i in range(0, len(tokens), 50):
        batch = tokens[i : i + 50]
        try:
            qr = requests.post(
                q_url,
                json={"mode": "FULL", "exchangeTokens": {meta["opt_exch"]: batch}},
                headers=headers,
                timeout=6,
            ).json()
            if qr.get("status") and qr.get("data", {}).get("fetched"):
                for item in qr["data"]["fetched"]:
                    fetched[str(item["symbolToken"])] = item
        except Exception:
            pass

    if not fetched:
        return None

    rows = []
    for k in sorted(sub["clean_strike"].unique()):
        ct = cmap.get((k, "CE"), {}).get("token", "")
        pt = cmap.get((k, "PE"), {}).get("token", "")
        cq = fetched.get(ct, {})
        pq = fetched.get(pt, {})
        rows.append(
            {
                "CE_TOKEN": ct,
                "PE_TOKEN": pt,
                "STRIKE": k,
                "CALL VOL": int(float(cq.get("tradeVolume", 0) or 0)),
                "CALL OI": int(float(cq.get("opnInterest", 0) or 0)),
                "API_CALL_CHG": int(float(cq.get("netChangeOpenInterest", 0) or 0)),
                "CALL LTP": float(cq.get("ltp", 0.0) or 0.0),
                "PUT LTP": float(pq.get("ltp", 0.0) or 0.0),
                "PUT OI": int(float(pq.get("opnInterest", 0) or 0)),
                "API_PUT_CHG": int(float(pq.get("netChangeOpenInterest", 0) or 0)),
                "PUT VOL": int(float(pq.get("tradeVolume", 0) or 0)),
                "DTE": max(0.2, float(dte)),
            }
        )

    df_acc, oi_valid, oi_src = compute_accurate_chg_oi(selected_idx, pd.DataFrame(rows))
    return {
        "df": df_acc,
        "spot": spot,
        "atm": atm,
        "exp_str": exp_str,
        "dte": dte,
        "cmap": cmap,
        "oi_valid": oi_valid,
        "info": meta,
    }


# ==========================================
# 3. Permanent Option Chain Logger (तीनों इंडेक्स)
# ==========================================
_LAST_LOG_MIN = {}
_LAST_GH_SYNC = 0.0


def save_oc_permanent(idx_nm, cd, now_ist):
    global _LAST_GH_SYNC
    if not cd or not cd.get("oi_valid"):
        return
    today_str = now_ist.strftime("%Y-%m-%d")
    min_key = f"{idx_nm}_{today_str}_{now_ist.strftime('%H:%M')}"
    if _LAST_LOG_MIN.get(idx_nm) == min_key:
        return

    day_file = f"oc_history_{today_str}.csv"
    cols = ["STRIKE", "CALL VOL", "CALL CHG OI", "CALL OI", "CALL LTP", "PUT LTP", "PUT OI", "PUT CHG OI", "PUT VOL"]
    sdf = cd["df"][cols].copy()
    sdf.insert(0, "Time", now_ist.strftime("%H:%M"))
    sdf.insert(1, "Index", idx_nm)
    sdf.insert(2, "Spot", round(cd["spot"], 2))
    sdf.insert(3, "Expiry", cd["exp_str"])

    write_hdr = not os.path.exists(day_file)
    sdf.to_csv(day_file, mode="a", header=write_hdr, index=False)
    _LAST_LOG_MIN[idx_nm] = min_key
    WORKER_STATUS["logs_saved"] += 1

    curr_mins = now_ist.hour * 60 + now_ist.minute
    if (time.time() - _LAST_GH_SYNC >= 600) or curr_mins >= (15 * 60 + 25):
        with open(day_file, "r", encoding="utf-8") as f:
            gh_write_text(f"oc_logs/{day_file}", f.read(), f"Worker OC sync {today_str}")
        _LAST_GH_SYNC = time.time()


# ==========================================
# 4. Signal & Paper Trade Execution Engine
# ==========================================
def _signal_dir(cd):
    step = cd["info"]["step"]
    atm = cd["atm"]
    df = cd["df"]
    up3 = int(df[(df["STRIKE"] > atm) & (df["STRIKE"] <= atm + 3 * step)]["CALL CHG OI"].sum())
    dn3 = int(df[(df["STRIKE"] < atm) & (df["STRIKE"] >= atm - 3 * step)]["PUT CHG OI"].sum())
    if dn3 > 0 and dn3 >= up3 * 1.25:
        return "UP"
    if up3 > 0 and up3 >= dn3 * 1.25:
        return "DOWN"
    if up3 > 0 or dn3 > 0:
        return "SIDEWAYS"
    return "WAIT"


def _build_legs(direction, cd):
    atm, hedge = cd["atm"], cd["info"]["hedge"]
    df = cd["df"]

    def px(k, s):
        r = df[df["STRIKE"] == k]
        col = "CALL LTP" if s == "CE" else "PUT LTP"
        return float(r[col].iloc[0]) if not r.empty else 0.0

    if direction == "UP":
        legs = [
            {"action": "BUY", "strike": atm - hedge, "side": "PE", "entry_ltp": px(atm - hedge, "PE")},
            {"action": "SELL", "strike": atm, "side": "PE", "entry_ltp": px(atm, "PE")},
        ]
        return "Bull Put Spread", f"Sell {atm} PE / Buy {atm-hedge} PE", legs
    elif direction == "DOWN":
        legs = [
            {"action": "BUY", "strike": atm + hedge, "side": "CE", "entry_ltp": px(atm + hedge, "CE")},
            {"action": "SELL", "strike": atm, "side": "CE", "entry_ltp": px(atm, "CE")},
        ]
        return "Bear Call Spread", f"Sell {atm} CE / Buy {atm+hedge} CE", legs
    else:
        legs = [
            {"action": "BUY", "strike": atm + hedge, "side": "CE", "entry_ltp": px(atm + hedge, "CE")},
            {"action": "BUY", "strike": atm - hedge, "side": "PE", "entry_ltp": px(atm - hedge, "PE")},
            {"action": "SELL", "strike": atm, "side": "CE", "entry_ltp": px(atm, "CE")},
            {"action": "SELL", "strike": atm, "side": "PE", "entry_ltp": px(atm, "PE")},
        ]
        return "Straddle + Hedge", f"Straddle {atm} CE+PE / Hedge {hedge}pts", legs


def _append_csv_gh(filename, row_dict):
    raw, _, status = gh_read_text(filename)
    if status == "OK" and raw:
        df = pd.read_csv(StringIO(raw))
        df = pd.concat([pd.DataFrame([row_dict]), df], ignore_index=True)
    else:
        df = pd.DataFrame([row_dict])
    gh_write_text(filename, df.to_csv(index=False), f"Worker update {filename}")


def _calc_trade_mtm(tr, cd):
    if not cd or cd.get("df") is None or cd["df"].empty:
        return round(float(tr.get("last_mtm", 0.0)), 2)
    df_now = cd["df"]
    pts = 0.0
    for lg in tr.get("legs", []):
        r = df_now[df_now["STRIKE"] == lg["strike"]]
        col = "CALL LTP" if lg["side"] == "CE" else "PUT LTP"
        px = (
            float(r[col].iloc[0])
            if (not r.empty and float(r[col].iloc[0]) > 0)
            else float(lg.get("last_valid_ltp", lg["entry_ltp"]))
        )
        lg["last_valid_ltp"] = px
        pts += (lg["entry_ltp"] - px) if lg["action"] == "SELL" else (px - lg["entry_ltp"])
    mtm = round(pts * float(tr.get("qty", 0)), 2)
    tr["last_mtm"] = mtm
    return mtm


def _send_all_trips_status(m_map, chains, now_str):
    if not m_map:
        tg_send(f"📊 <b>WORKER STATUS ({now_str})</b>\nकोई खुला ट्रिप/ट्रेड नहीं चल रहा है।")
        return

    lines = [f"📊 <b>ALL ACTIVE TRIPS MTM ({now_str})</b>"]
    total_mtm = 0.0
    for key, tr in m_map.items():
        idx_nm = tr.get("index", key)
        strat_nm = tr.get("strategy", tr.get("summary", "Strategy"))
        mtm = _calc_trade_mtm(tr, chains.get(idx_nm))
        total_mtm += mtm
        lines.append(f"• <b>{idx_nm}</b> | {strat_nm}: <b>₹{mtm:+,.0f}</b>")

    lines.append(f"━━━━━━━━━━━━━━━\n<b>कुल (Total) MTM: ₹{total_mtm:+,.0f}</b>")
    tg_send("\n".join(lines))


def worker_loop():
    ist = pytz.timezone("Asia/Kolkata")
    while True:
        try:
            now_ist = datetime.now(ist)
            WORKER_STATUS["last_tick"] = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")

            if now_ist.weekday() < 5:
                curr_mins = now_ist.hour * 60 + now_ist.minute
                if (9 * 60 + 15) <= curr_mins <= (15 * 60 + 30):
                    now_str = now_ist.strftime("%I:%M:%S %p")
                    tg_cmd = check_tg_cmd()

                    raw_m, _, st_m = gh_read_text("active_trades_multi.json")
                    m_map = json.loads(raw_m) if (st_m == "OK" and raw_m) else {}
                    m_changed = False

                    # यदि /STATUS आया है तो chains fetch करने से पहले ही तुरंत मौजूदा MTM भेजें
                    if tg_cmd == "STATUS":
                        _send_all_trips_status(m_map, {}, now_str)

                    jwt, akey = ensure_angel_session()
                    chains = {}
                    if jwt and akey:
                        cfg = load_algo_settings(force_refresh=True)
                        today_str = now_ist.strftime("%Y-%m-%d")

                        for idx_nm in ["Nifty 50", "Bank Nifty", "Sensex"]:
                            cd = fetch_chain_worker(jwt, akey, idx_nm)
                            if cd:
                                chains[idx_nm] = cd
                                save_oc_permanent(idx_nm, cd, now_ist)

                        for idx_nm in list(m_map.keys()):
                            tr = m_map[idx_nm]
                            cd = chains.get(idx_nm)
                            mtm = _calc_trade_mtm(tr, cd)
                            if tg_cmd == "EXIT" or mtm <= -float(tr.get("sl_rs", 1700)) or curr_mins >= (15 * 60 + 15):
                                rsn = (
                                    "Telegram /exit"
                                    if tg_cmd == "EXIT"
                                    else ("Auto SL Hit" if mtm <= -float(tr.get("sl_rs", 1700)) else "3:15 PM Square-Off")
                                )
                                _append_csv_gh(
                                    "paper_trades_history.csv",
                                    {
                                        "तारीख": today_str,
                                        "मोड": "Paper",
                                        "इंडेक्स": idx_nm,
                                        "स्प्रेड": f"{tr.get('strategy', '')} - {tr.get('summary', '')}".strip(" -"),
                                        "लॉट": tr.get("lots", 1),
                                        "एंट्री समय": tr.get("entry_time", ""),
                                        "एग्जिट समय": now_str,
                                        "कारण": rsn,
                                        "लाभ / हानि (₹)": mtm,
                                    },
                                )
                                del m_map[idx_nm]
                                m_changed = True
                                tg_send(f"🏁 <b>CLOUD PAPER CLOSED ({idx_nm})</b>\n{rsn} | P&L: <b>₹{mtm:+,.0f}</b>")

                        if cfg.get("main_auto_10am") and now_ist.hour == 10 and now_ist.minute <= 5:
                            lots = int(cfg.get("main_lots", 1))
                            for idx_nm in cfg.get("main_indices", ["Nifty 50", "Sensex"]):
                                if idx_nm in m_map or is_strategy_traded_today("main", today_str, idx_nm):
                                    continue
                                cd = chains.get(idx_nm)
                                if cd and cd["oi_valid"]:
                                    d = _signal_dir(cd)
                                    if d in ["UP", "DOWN", "SIDEWAYS"]:
                                        sn, ss, legs = _build_legs(d, cd)
                                        if not any(float(x["entry_ltp"]) <= 0 for x in legs):
                                            meta = cd["info"]
                                            for lg in legs:
                                                ci = cd["cmap"].get(
                                                    (lg["strike"], lg["side"]),
                                                    {"symbol": "", "token": "0", "exch": meta["opt_exch"]},
                                                )
                                                lg.update(
                                                    {
                                                        "symbol": ci["symbol"],
                                                        "token": ci["token"],
                                                        "exch": ci["exch"],
                                                        "last_valid_ltp": lg["entry_ltp"],
                                                    }
                                                )
                                            m_map[idx_nm] = {
                                                "mode": "🟡 PAPER TRADING (वर्चुअल प्रैक्टिस)",
                                                "index": idx_nm,
                                                "strategy": sn,
                                                "summary": ss,
                                                "lots": lots,
                                                "qty": meta["lot"] * lots,
                                                "exch": meta["opt_exch"],
                                                "legs": legs,
                                                "entry_time": now_str,
                                                "sl_rs": 1700.0 * lots,
                                                "last_mtm": 0.0,
                                            }
                                            m_changed = True
                                            mark_strategy_traded_today("main", today_str, idx_nm)
                                            tg_send(f"🚀 <b>CLOUD 10AM AUTO-ENTRY ({idx_nm})</b>\n{sn}: {ss}\nTime: {now_str}")

                        if m_changed:
                            gh_write_text(
                                "active_trades_multi.json",
                                json.dumps(m_map, ensure_ascii=False, indent=2),
                                "Worker update active trades",
                            )
                        WORKER_STATUS["active_trades"] = len(m_map)
        except Exception as e:
            WORKER_STATUS["last_error"] = f"worker_loop: {e}"
        time.sleep(15)


if __name__ == "__main__":
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
    start_web_server()
