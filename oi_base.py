import os
import json
import base64
import time
from io import StringIO
from datetime import datetime, timedelta
import pytz
import pandas as pd
import requests
import pyotp

try:
    import streamlit as st
except Exception:
    st = None

DEFAULT_GH_REPO = "Sarveshbaghel05/algo-trading-app"
SETTINGS_FILE = "algo_settings.json"
EOD_OI_FILE = "daily_eod_oi.json"
OC_RECORD_FILE = "recorded_oc_history.csv"

_MEM_CACHE = {}


def _get_secret(key_name, default_val=""):
    # पहले Streamlit secrets देखें, न मिले तो Render/OS Environment Variables देखें
    try:
        if st is not None and key_name in st.secrets:
            return str(st.secrets[key_name]).strip()
    except Exception:
        pass
    return str(os.environ.get(key_name, default_val)).strip()


# ==========================================
# 1. GitHub Storage Helpers (1MB+ Safe)
# ==========================================
def get_gh_config():
    token = _get_secret("GITHUB_TOKEN", "")
    repo = _get_secret("GITHUB_REPO", DEFAULT_GH_REPO) or DEFAULT_GH_REPO
    return token, repo


def gh_read_text(filepath):
    token, repo = get_gh_config()
    if not token:
        return None, None, "NO_TOKEN"
    url = f"https://api.github.com/repos/{repo}/contents/{filepath}"
    headers_meta = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"}
    try:
        res = requests.get(url, headers=headers_meta, timeout=8)
        if res.status_code == 404:
            return None, None, "NOT_FOUND"
        if res.status_code != 200:
            return None, None, f"ERR_{res.status_code}"
        meta = res.json()
        sha = meta.get("sha")
        content_b64 = meta.get("content", "")
        if content_b64:
            raw_txt = base64.b64decode(content_b64).decode("utf-8")
            return raw_txt, sha, "OK"
        headers_raw = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw"}
        raw_res = requests.get(url, headers=headers_raw, timeout=12)
        if raw_res.status_code == 200:
            return raw_res.text, sha, "OK"
        return None, sha, f"RAW_ERR_{raw_res.status_code}"
    except Exception as e:
        return None, None, str(e)


def gh_write_text(filepath, text_content, commit_msg):
    token, repo = get_gh_config()
    if not token:
        return False, "GITHUB_TOKEN Missing"
    _, sha, _ = gh_read_text(filepath)
    url = f"https://api.github.com/repos/{repo}/contents/{filepath}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"}
    payload = {
        "message": commit_msg,
        "content": base64.b64encode(text_content.encode("utf-8")).decode("utf-8"),
    }
    if sha:
        payload["sha"] = sha
    try:
        res = requests.put(url, json=payload, headers=headers, timeout=12)
        if res.status_code in [200, 201]:
            return True, "Saved"
        return False, f"Err {res.status_code}"
    except Exception as e:
        return False, str(e)


def gh_read_json(filepath, default_val=None):
    raw, _, status = gh_read_text(filepath)
    if status == "OK" and raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {} if default_val is None else default_val


def gh_write_json(filepath, data_obj, commit_msg):
    return gh_write_text(filepath, json.dumps(data_obj, ensure_ascii=False, indent=2), commit_msg)


# ==========================================
# 2. Persistent Settings (Multi-Index Nifty + Sensex)
# ==========================================
def load_algo_settings(force_refresh=False):
    """तीनों रणनीतियों के टॉगल, इंडेक्स लिस्ट और दैनिक लॉक लोड करना"""
    defaults = {
        "main_auto_10am": False,
        "main_indices": ["Nifty 50", "Sensex"],
        "main_lots": 1,
        "ema_auto_arm": False,
        "ema_indices": ["Nifty 50", "Sensex"],
        "ema_tf": 5,
        "ema_lots": 1,
        "pro_auto": False,
        "pro_indices": ["Nifty 50", "Sensex"],
        "pro_lots": 1,
        "traded_dates": {},
        "last_tg_update_id": 0,
    }

    now_ts = time.time()
    cache_age = now_ts - _MEM_CACHE.get("settings_ts", 0)
    if not force_refresh and "settings_obj" in _MEM_CACHE and cache_age < 20:
        cfg = _MEM_CACHE["settings_obj"]
        for k, v in defaults.items():
            if k not in cfg:
                cfg[k] = v
        return cfg

    loaded = gh_read_json(SETTINGS_FILE, defaults)
    for k, v in defaults.items():
        if k not in loaded:
            loaded[k] = v
    if not isinstance(loaded.get("traded_dates"), dict):
        loaded["traded_dates"] = {}

    _MEM_CACHE["settings_obj"] = loaded
    _MEM_CACHE["settings_ts"] = now_ts
    return loaded


def save_algo_settings(cfg):
    _MEM_CACHE["settings_obj"] = cfg
    _MEM_CACHE["settings_ts"] = time.time()
    gh_write_json(SETTINGS_FILE, cfg, "Update persistent algo settings")


def update_algo_settings_batch(updates_dict):
    """केवल तभी GitHub पर सेव करें जब सचमुच कोई मान बदला हो"""
    cfg = load_algo_settings()
    changed = False
    for k, v in updates_dict.items():
        if cfg.get(k) != v:
            cfg[k] = v
            changed = True
    if changed:
        save_algo_settings(cfg)


def set_toggle_setting(key_name, bool_val):
    update_algo_settings_batch({key_name: bool(bool_val)})


def is_strategy_traded_today(strategy_key, today_str, idx_name="ALL"):
    cfg = load_algo_settings()
    td = cfg.get("traded_dates", {})
    slot_key = f"{strategy_key}_{idx_name}"
    return td.get(slot_key, "") == today_str or td.get(strategy_key, "") == today_str


def mark_strategy_traded_today(strategy_key, today_str, idx_name="ALL"):
    cfg = load_algo_settings()
    if not isinstance(cfg.get("traded_dates"), dict):
        cfg["traded_dates"] = {}
    slot_key = f"{strategy_key}_{idx_name}"
    if cfg["traded_dates"].get(slot_key) != today_str:
        cfg["traded_dates"][slot_key] = today_str
        save_algo_settings(cfg)


def get_saved_tg_offset():
    cfg = load_algo_settings()
    return int(cfg.get("last_tg_update_id", 0) or 0)


def save_tg_offset(new_offset):
    cfg = load_algo_settings()
    cur = int(cfg.get("last_tg_update_id", 0) or 0)
    if int(new_offset) > cur:
        cfg["last_tg_update_id"] = int(new_offset)
        save_algo_settings(cfg)


# ==========================================
# 3. Angel One Auto-Login & Scrip Master
# ==========================================
def ensure_angel_session(force_relogin=False):
    now_ts = time.time()
    if not force_relogin and "angel_jwt" in _MEM_CACHE and (now_ts - _MEM_CACHE.get("angel_login_ts", 0)) < 14400:
        return _MEM_CACHE["angel_jwt"], _MEM_CACHE["angel_key"]

    api_key = _get_secret("ANGEL_API_KEY", "")
    client_id = _get_secret("ANGEL_CLIENT_ID", "")
    mpin = _get_secret("ANGEL_MPIN", "")
    totp_sec = _get_secret("ANGEL_TOTP", "").replace(" ", "")

    if not (api_key and client_id and mpin and totp_sec):
        return None, None

    try:
        totp = pyotp.TOTP(totp_sec).now()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": api_key,
        }
        res = requests.post(
            "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword",
            json={"clientcode": client_id, "password": mpin, "totp": totp},
            headers=headers,
            timeout=10,
        ).json()
        if res.get("status") and res.get("data", {}).get("jwtToken"):
            jwt = res["data"]["jwtToken"]
            _MEM_CACHE["angel_jwt"] = jwt
            _MEM_CACHE["angel_key"] = api_key
            _MEM_CACHE["angel_login_ts"] = now_ts
            return jwt, api_key
    except Exception:
        pass
    return None, None


def get_scrip_master_df():
    now_ts = time.time()
    if "scrip_df" in _MEM_CACHE and (now_ts - _MEM_CACHE.get("scrip_ts", 0)) < 7200:
        return _MEM_CACHE["scrip_df"]
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    res = requests.get(url, timeout=18)
    df = pd.DataFrame(res.json())
    _MEM_CACHE["scrip_df"] = df
    _MEM_CACHE["scrip_ts"] = now_ts
    return df


def get_index_meta(selected_idx):
    base_cfg = {
        "Nifty 50": {"exch": "NSE", "token": "99926000", "name": "NIFTY", "opt_exch": "NFO", "step": 50, "hedge": 500, "default_lot": 65, "yf": "^NSEI"},
        "Bank Nifty": {"exch": "NSE", "token": "99926009", "name": "BANKNIFTY", "opt_exch": "NFO", "step": 100, "hedge": 1000, "default_lot": 30, "yf": "^NSEBANK"},
        "Sensex": {"exch": "BSE", "token": "99919000", "name": "SENSEX", "opt_exch": "BFO", "step": 100, "hedge": 1500, "default_lot": 20, "yf": "^BSESN"},
    }[selected_idx]

    lot_qty = base_cfg["default_lot"]
    try:
        mdf = get_scrip_master_df()
        sub = mdf[
            (mdf["name"] == base_cfg["name"])
            & (mdf["exch_seg"] == base_cfg["opt_exch"])
            & (mdf["instrumenttype"] == "OPTIDX")
        ]
        if not sub.empty and "lotsize" in sub.columns:
            val = int(float(sub["lotsize"].dropna().iloc[0]))
            if val > 0:
                lot_qty = val
    except Exception:
        pass

    out = dict(base_cfg)
    out["lot"] = lot_qty
    return out


# ==========================================
# 4. सही Expiry और DTE (छुट्टियों सहित)
# ==========================================
def get_live_expiry_info(selected_idx):
    meta = get_index_meta(selected_idx)
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    today_date = now_ist.date()

    try:
        mdf = get_scrip_master_df()
        opt_df = mdf[
            (mdf["name"] == meta["name"])
            & (mdf["exch_seg"] == meta["opt_exch"])
            & (mdf["instrumenttype"] == "OPTIDX")
        ].copy()
        opt_df["expiry_dt"] = pd.to_datetime(opt_df["expiry"], format="%d%b%Y", errors="coerce")
        opt_df = opt_df.dropna(subset=["expiry_dt"])
        future_exps = sorted(opt_df[opt_df["expiry_dt"].dt.date >= today_date]["expiry_dt"].dt.date.unique())
        if future_exps:
            nearest_exp = future_exps[0]
            exp_str = nearest_exp.strftime("%d-%b-%Y")
            cal_days = (nearest_exp - today_date).days
            trading_dte = 0
            cur = today_date
            while cur < nearest_exp:
                cur += timedelta(days=1)
                if cur.weekday() < 5:
                    trading_dte += 1
            return exp_str, trading_dte, cal_days
    except Exception:
        pass

    return "Unknown", 1, 1


def get_exact_t_years(expiry_str):
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    if not expiry_str or expiry_str in ["Unknown", "Simulation", "DEMO", "Backtest"]:
        return 1.0 / 365.0
    try:
        exp_dt = datetime.strptime(expiry_str.strip(), "%d-%b-%Y")
        exp_dt = ist.localize(exp_dt.replace(hour=15, minute=30, second=0))
        diff_secs = max(60.0, (exp_dt - now_ist).total_seconds())
        return diff_secs / (365.0 * 24.0 * 3600.0)
    except Exception:
        return 1.0 / 365.0


def build_backtest_dte_map(sorted_trading_dates, selected_idx):
    if not sorted_trading_dates:
        return {}
    dates_list = sorted(list(set(sorted_trading_dates)))
    date_set = set(dates_list)
    expiry_dates = set()

    if selected_idx == "Nifty 50":
        for d in dates_list:
            scheduled_exp = (d - timedelta(days=d.weekday())) + timedelta(days=1)
            actual_exp = scheduled_exp
            for _ in range(5):
                if actual_exp in date_set:
                    break
                actual_exp -= timedelta(days=1)
            if actual_exp in date_set:
                expiry_dates.add(actual_exp)
    elif selected_idx == "Sensex":
        for d in dates_list:
            scheduled_exp = (d - timedelta(days=d.weekday())) + timedelta(days=3)
            actual_exp = scheduled_exp
            for _ in range(5):
                if actual_exp in date_set:
                    break
                actual_exp -= timedelta(days=1)
            if actual_exp in date_set:
                expiry_dates.add(actual_exp)
    else:
        by_ym = {}
        for d in dates_list:
            by_ym.setdefault((d.year, d.month), []).append(d)
        for _, m_dates in by_ym.items():
            tuesdays = [x for x in m_dates if x.weekday() == 1]
            expiry_dates.add(max(tuesdays) if tuesdays else max(m_dates))

    sorted_exps = sorted(list(expiry_dates))
    dte_map = {}
    for d in dates_list:
        future_e = [e for e in sorted_exps if e >= d]
        if not future_e:
            dte_map[d] = 0
            continue
        nxt_exp = future_e[0]
        dte_map[d] = sum(1 for x in dates_list if d < x <= nxt_exp)
    return dte_map


# ==========================================
# 5. सटीक Change in OI इंजन (तुरंत सेविंग सहित)
# ==========================================
def _load_eod_oi_store():
    if "eod_oi_store" in _MEM_CACHE:
        return _MEM_CACHE["eod_oi_store"]
    data = gh_read_json(EOD_OI_FILE, {"days": {}})
    if not isinstance(data.get("days"), dict):
        data = {"days": {}}
    _MEM_CACHE["eod_oi_store"] = data
    return data


def _save_eod_oi_store(data):
    _MEM_CACHE["eod_oi_store"] = data
    gh_write_json(EOD_OI_FILE, data, "Update EOD OI baseline")


def _get_prev_day_recorded_oi(selected_idx, today_str):
    try:
        cache_k = f"rec_prev_{selected_idx}_{today_str}"
        if cache_k in _MEM_CACHE:
            return _MEM_CACHE[cache_k]
        raw_txt, _, status = gh_read_text(OC_RECORD_FILE)
        if status == "OK" and raw_txt:
            df = pd.read_csv(StringIO(raw_txt))
            if not df.empty and "Date" in df.columns and "Index" in df.columns:
                sub = df[(df["Index"] == selected_idx) & (df["Date"] < today_str)].copy()
                if not sub.empty:
                    last_dt = sub["Date"].max()
                    last_day = sub[sub["Date"] == last_dt].sort_values("Time")
                    last_time = last_day["Time"].iloc[-1]
                    snap = last_day[last_day["Time"] == last_time]
                    by_strike = {}
                    for _, r in snap.iterrows():
                        by_strike[int(r["STRIKE"])] = {
                            "CE": int(float(r.get("CALL OI", 0) or 0)),
                            "PE": int(float(r.get("PUT OI", 0) or 0)),
                        }
                    res_obj = {"by_strike": by_strike, "label": f"Recorded Close ({last_dt} {last_time})"}
                    _MEM_CACHE[cache_k] = res_obj
                    return res_obj
    except Exception:
        pass
    return None


def compute_accurate_chg_oi(selected_idx, raw_df):
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    today_str = now_ist.strftime("%Y-%m-%d")
    now_time_str = now_ist.strftime("%H:%M")

    store = _load_eod_oi_store()
    days_dict = store.get("days", {})

    prev_dates = sorted([d for d in days_dict.keys() if d < today_str])
    prev_eod_map = {}
    prev_eod_label = ""
    if prev_dates:
        last_d = prev_dates[-1]
        prev_day_obj = days_dict[last_d]
        prev_eod_map = prev_day_obj.get("tokens", {})
        prev_eod_label = f"Prev Close ({last_d} {prev_day_obj.get('time', '15:25')})"

    rec_prev = _get_prev_day_recorded_oi(selected_idx, today_str) if not prev_eod_map else None

    today_entry = days_dict.get(today_str, {"time": now_time_str, "tokens": {}})
    today_tokens = today_entry.get("tokens", {})
    need_save_eod = False

    ce_chgs, pe_chgs = [], []
    valid_count = 0
    source_used = "N/A (कल का क्लोज़ OI उपलब्ध नहीं)"

    for _, r in raw_df.iterrows():
        c_tok = str(r.get("CE_TOKEN", ""))
        p_tok = str(r.get("PE_TOKEN", ""))
        strike_k = int(r["STRIKE"])
        c_oi = int(r.get("CALL OI", 0) or 0)
        p_oi = int(r.get("PUT OI", 0) or 0)
        api_c_chg = int(r.get("API_CALL_CHG", 0) or 0)
        api_p_chg = int(r.get("API_PUT_CHG", 0) or 0)

        if c_tok and c_oi > 0 and today_tokens.get(c_tok) != c_oi:
            today_tokens[c_tok] = c_oi
            need_save_eod = True
        if p_tok and p_oi > 0 and today_tokens.get(p_tok) != p_oi:
            today_tokens[p_tok] = p_oi
            need_save_eod = True

        if api_c_chg != 0 or api_p_chg != 0:
            ce_chgs.append(api_c_chg)
            pe_chgs.append(api_p_chg)
            valid_count += 1
            source_used = "Angel Live Net Chg OI ✅"
        elif c_tok in prev_eod_map and p_tok in prev_eod_map and c_oi > 0 and p_oi > 0:
            ce_chgs.append(c_oi - int(prev_eod_map[c_tok]))
            pe_chgs.append(p_oi - int(prev_eod_map[p_tok]))
            valid_count += 1
            source_used = f"{prev_eod_label} ✅"
        elif rec_prev and strike_k in rec_prev["by_strike"] and c_oi > 0 and p_oi > 0:
            prev_ce = rec_prev["by_strike"][strike_k]["CE"]
            prev_pe = rec_prev["by_strike"][strike_k]["PE"]
            if prev_ce > 0 and prev_pe > 0:
                ce_chgs.append(c_oi - prev_ce)
                pe_chgs.append(p_oi - prev_pe)
                valid_count += 1
                source_used = f"{rec_prev['label']} ✅"
            else:
                ce_chgs.append(0)
                pe_chgs.append(0)
        else:
            ce_chgs.append(0)
            pe_chgs.append(0)

    # पहली बार तुरंत सेव हो (0 सेकंड देरी), उसके बाद हर 5 मिनट पर अपडेट हो
    last_eod_save_ts = _MEM_CACHE.get("last_eod_save_ts", 0)
    if need_save_eod and (last_eod_save_ts == 0 or (time.time() - last_eod_save_ts >= 300)):
        today_entry["time"] = now_time_str
        today_entry["tokens"] = today_tokens
        days_dict[today_str] = today_entry
        for old_d in sorted(days_dict.keys())[:-5]:
            del days_dict[old_d]
        store["days"] = days_dict
        _save_eod_oi_store(store)
        _MEM_CACHE["last_eod_save_ts"] = time.time()

    oi_valid = valid_count >= max(3, len(raw_df) // 3)
    df_out = raw_df.copy()
    df_out["CALL CHG OI"] = ce_chgs
    df_out["PUT CHG OI"] = pe_chgs
    return df_out, oi_valid, source_used
