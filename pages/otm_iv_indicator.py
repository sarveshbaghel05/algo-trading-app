import math
from datetime import datetime
import pytz
import numpy as np
import pandas as pd
import requests
import streamlit as st

try:
    from oi_base import (
        ensure_angel_session,
        get_scrip_master_df,
        get_index_meta,
        get_live_expiry_info,
        get_exact_t_years,
        compute_accurate_chg_oi,
    )
except Exception:
    ensure_angel_session = None
    get_scrip_master_df = None
    get_index_meta = None
    get_live_expiry_info = None
    get_exact_t_years = None
    compute_accurate_chg_oi = None


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x):
    return (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * x * x)


def bs_option_price(spot, strike, t_years, rate, iv_dec, opt_type="CE"):
    if spot <= 0 or strike <= 0:
        return 0.0
    if t_years <= 1e-6 or iv_dec <= 1e-4:
        return max(0.0, spot - strike) if opt_type == "CE" else max(0.0, strike - spot)
    try:
        vol_t = iv_dec * math.sqrt(t_years)
        d1 = (math.log(spot / strike) + (rate + 0.5 * iv_dec * iv_dec) * t_years) / vol_t
        d2 = d1 - vol_t
        if opt_type == "CE":
            return max(0.0, spot * _norm_cdf(d1) - strike * math.exp(-rate * t_years) * _norm_cdf(d2))
        return max(0.0, strike * math.exp(-rate * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1))
    except Exception:
        return 0.0


def implied_volatility_bs(price, spot, strike, t_years, rate=0.065, opt_type="CE"):
    intrinsic = max(0.0, spot - strike) if opt_type == "CE" else max(0.0, strike - spot)
    if price is None or price <= intrinsic + 0.05 or spot <= 0 or strike <= 0 or t_years <= 1e-6:
        return 0.0
    low, high = 0.01, 2.50
    mid = 0.16
    for _ in range(55):
        mid = 0.5 * (low + high)
        p_mid = bs_option_price(spot, strike, t_years, rate, mid, opt_type)
        if abs(p_mid - price) < 0.02:
            break
        if p_mid > price:
            high = mid
        else:
            low = mid
    return round(mid * 100.0, 2)


def _fetch_live_chain_for_otm(selected_idx):
    if ensure_angel_session is None or get_index_meta is None:
        return None, 0.0, "Session helper unavailable"
    jwt_tok, api_key = ensure_angel_session()
    if not jwt_tok or not api_key:
        return None, 0.0, "Angel session not active"

    meta = get_index_meta(selected_idx)
    exp_str, _, _ = get_live_expiry_info(selected_idx)
    t_years = get_exact_t_years(exp_str)

    try:
        mdf = get_scrip_master_df()
        sub = mdf[
            (mdf["name"] == meta["name"])
            & (mdf["exch_seg"] == meta["opt_exch"])
            & (mdf["instrumenttype"] == "OPTIDX")
        ].copy()
        sub["expiry_dt"] = pd.to_datetime(sub["expiry"], format="%d%b%Y", errors="coerce")
        sub = sub.dropna(subset=["expiry_dt"])
        if sub.empty:
            return None, 0.0, "No option contracts found"
        nearest_exp = sorted(sub["expiry_dt"].dt.date.unique())[0]
        exp_df = sub[sub["expiry_dt"].dt.date == nearest_exp].copy()
        exp_df["strike_num"] = pd.to_numeric(exp_df["strike"], errors="coerce") / 100.0
        exp_df = exp_df.dropna(subset=["strike_num"])

        headers = {
            "Authorization": f"Bearer {jwt_tok}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": api_key,
        }

        spot = 0.0
        ltp_res = requests.post(
            "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/",
            json={"mode": "LTP", "exchangeTokens": {meta["exch"]: [meta["token"]]}},
            headers=headers,
            timeout=10,
        ).json()
        if ltp_res.get("status"):
            fetched = ltp_res.get("data", {}).get("fetched", [])
            if fetched:
                spot = float(fetched[0].get("ltp", 0) or 0)

        if spot <= 0:
            return None, 0.0, "Spot LTP unavailable"

        step = meta["step"]
        atm = int(round(spot / step) * step)
        strikes = [atm + i * step for i in range(-10, 11)]

        ce_map = {}
        pe_map = {}
        for _, r in exp_df[exp_df["strike_num"].isin(strikes)].iterrows():
            k = int(round(float(r["strike_num"])))
            sym = str(r.get("symbol", ""))
            tok = str(r.get("token", ""))
            if sym.endswith("CE"):
                ce_map[k] = tok
            elif sym.endswith("PE"):
                pe_map[k] = tok

        opt_tokens = [t for t in list(ce_map.values()) + list(pe_map.values()) if t]
        q_map = {}
        if opt_tokens:
            q_res = requests.post(
                "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/",
                json={"mode": "FULL", "exchangeTokens": {meta["opt_exch"]: opt_tokens[:50]}},
                headers=headers,
                timeout=12,
            ).json()
            if q_res.get("status"):
                for item in q_res.get("data", {}).get("fetched", []):
                    q_map[str(item.get("symbolToken", ""))] = item

        rows = []
        for k in strikes:
            c_tok = ce_map.get(k, "")
            p_tok = pe_map.get(k, "")
            c_q = q_map.get(c_tok, {})
            p_q = q_map.get(p_tok, {})
            c_ltp = float(c_q.get("ltp", 0) or 0)
            p_ltp = float(p_q.get("ltp", 0) or 0)
            c_oi = int(float(c_q.get("opnInterest", 0) or 0))
            p_oi = int(float(p_q.get("opnInterest", 0) or 0))
            c_iv = implied_volatility_bs(c_ltp, spot, k, t_years, 0.065, "CE") if k >= atm else 0.0
            p_iv = implied_volatility_bs(p_ltp, spot, k, t_years, 0.065, "PE") if k <= atm else 0.0
            rows.append({
                "STRIKE": k,
                "CE_TOKEN": c_tok,
                "PE_TOKEN": p_tok,
                "CALL LTP": c_ltp,
                "PUT LTP": p_ltp,
                "CALL OI": c_oi,
                "PUT OI": p_oi,
                "API_CALL_CHG": 0,
                "API_PUT_CHG": 0,
                "CE_OTM_IV": c_iv,
                "PE_OTM_IV": p_iv,
            })

        df = pd.DataFrame(rows)
        if compute_accurate_chg_oi is not None and not df.empty:
            df, _, _ = compute_accurate_chg_oi(selected_idx, df)
        return df, spot, "OK"
    except Exception as e:
        return None, 0.0, str(e)


def compute_otm_iv_metrics(chain_df, spot, selected_idx="Nifty 50"):
    if chain_df is None or chain_df.empty or spot <= 0:
        return {
            "ce_otm_iv": 0.0,
            "pe_otm_iv": 0.0,
            "avg_otm_iv": 0.0,
            "iv_skew_ce_minus_pe": 0.0,
            "iv_filter_pass": True,
            "iv_regime": "Neutral / Unknown",
            "signal_text": "Neutral / Insufficient Data",
        }

    step = 50
    if get_index_meta is not None:
        try:
            step = int(get_index_meta(selected_idx).get("step", 50))
        except Exception:
            step = 50

    atm = round(spot / step) * step
    ce_iv_vals = []
    pe_iv_vals = []

    for _, r in chain_df.iterrows():
        k = float(r.get("STRIKE", 0) or 0)
        c_iv = float(r.get("CE_OTM_IV", 0) or 0)
        p_iv = float(r.get("PE_OTM_IV", 0) or 0)
        if k >= atm + step and k <= atm + 8 * step and c_iv > 0:
            ce_iv_vals.append(c_iv)
        if k <= atm - step and k >= atm - 8 * step and p_iv > 0:
            pe_iv_vals.append(p_iv)

    ce_avg = round(float(np.mean(ce_iv_vals)), 2) if ce_iv_vals else 0.0
    pe_avg = round(float(np.mean(pe_iv_vals)), 2) if pe_iv_vals else 0.0
    valid_ivs = [v for v in [ce_avg, pe_avg] if v > 0]
    avg_iv = round(float(np.mean(valid_ivs)), 2) if valid_ivs else 0.0
    skew = round(ce_avg - pe_avg, 2) if (ce_avg > 0 and pe_avg > 0) else 0.0

    iv_filter_pass = (avg_iv == 0.0) or (avg_iv <= 22.0)

    if skew > 1.5:
        iv_regime = "Call IV Elevated (Bearish / Upside Fear)"
        signal_text = "Bearish Skew"
    elif skew < -1.5:
        iv_regime = "Put IV Elevated (Downside Protection)"
        signal_text = "Bullish / Cautious Skew"
    else:
        iv_regime = "Balanced OTM IV (Range / Sideways)"
        signal_text = "Sideways / Balanced"

    return {
        "ce_otm_iv": ce_avg,
        "pe_otm_iv": pe_avg,
        "avg_otm_iv": avg_iv,
        "iv_skew_ce_minus_pe": skew,
        "iv_filter_pass": iv_filter_pass,
        "iv_regime": iv_regime,
        "signal_text": signal_text,
    }


def render_otm_iv_card(chain_df, spot, selected_idx="Nifty 50", iv_filter_enabled=True):
    m = compute_otm_iv_metrics(chain_df, spot, selected_idx)
    st.markdown("#### 🎯 OTM IV Indicator")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Avg OTM IV", f"{m['avg_otm_iv']:.2f}%")
    c2.metric("CE OTM IV", f"{m['ce_otm_iv']:.2f}%")
    c3.metric("PE OTM IV", f"{m['pe_otm_iv']:.2f}%")
    c4.metric("IV Skew (CE-PE)", f"{m['iv_skew_ce_minus_pe']:+.2f}%")

    filter_ok = True if not iv_filter_enabled else m["iv_filter_pass"]
    status_txt = "PASS (Safe for Selling)" if filter_ok else "HIGH IV CAUTION"
    st.caption(f"Regime: **{m['iv_regime']}** | OTM IV Filter: **{status_txt}**")
    return m


def render_standalone_iv_page():
    st.set_page_config(page_title="OTM IV Indicator", layout="wide")
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)

    st.title("🎯 OTM IV & Volatility Skew Indicator")
    st.caption(f"IST Time: {now_ist.strftime('%d-%b-%Y %H:%M:%S')}")

    col_a, col_b = st.columns([2, 1])
    with col_a:
        selected_idx = st.selectbox("Select Index", ["Nifty 50", "Sensex", "Bank Nifty"], index=0)
    with col_b:
        iv_filter_enabled = st.toggle("OTM IV Filter", value=True)

    df, spot, status_msg = _fetch_live_chain_for_otm(selected_idx)
    if df is not None and not df.empty and spot > 0:
        render_otm_iv_card(df, spot, selected_idx=selected_idx, iv_filter_enabled=iv_filter_enabled)
        st.dataframe(df[["STRIKE", "CALL LTP", "PUT LTP", "CE_OTM_IV", "PE_OTM_IV"]], use_container_width=True, hide_index=True)
    else:
        st.info(f"Live option chain status: {status_msg}")

    try:
        from pages.greek_regime_indicator import (
            compute_greek_regime_metrics,
            render_greek_regime_dashboard,
        )
    except Exception:
        try:
            from greek_regime_indicator import (
                compute_greek_regime_metrics,
                render_greek_regime_dashboard,
            )
        except Exception:
            compute_greek_regime_metrics = None
            render_greek_regime_dashboard = None

    if render_greek_regime_dashboard is not None and df is not None and spot > 0:
        st.divider()
        try:
            render_greek_regime_dashboard(df, spot, selected_idx=selected_idx)
        except Exception:
            pass


if __name__ == "__main__":
    render_standalone_iv_page()
