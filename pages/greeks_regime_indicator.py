import math
from datetime import datetime
import pytz
import streamlit as st
import pandas as pd
import requests

from oi_base import (
    ensure_angel_session,
    get_scrip_master_df,
    get_index_meta,
    get_live_expiry_info,
    get_exact_t_years,
    compute_accurate_chg_oi,
)


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _implied_volatility(option_price, spot, strike, t_years, opt_type="CE"):
    """असली लाइव LTP और बचे हुए सटीक समय से IV निकालना"""
    if option_price <= 0.5 or t_years <= 1e-6 or spot <= 0 or strike <= 0:
        return 0.14

    intrinsic = max(0.0, spot - strike) if opt_type == "CE" else max(0.0, strike - spot)
    if option_price <= intrinsic + 0.2:
        return 0.08

    low, high = 0.02, 2.00
    r = 0.065
    for _ in range(40):
        mid = 0.5 * (low + high)
        d1 = (math.log(spot / strike) + (r + 0.5 * mid * mid) * t_years) / (mid * math.sqrt(t_years))
        d2 = d1 - mid * math.sqrt(t_years)
        if opt_type == "CE":
            px = spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2)
        else:
            px = strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)

        if px > option_price:
            high = mid
        else:
            low = mid
    return round(0.5 * (low + high), 4)


def _calc_bs_greeks(spot, strike, t_years, iv, opt_type="CE"):
    t_safe = max(t_years, 1.0 / (365.0 * 24.0))
    iv_safe = max(iv, 0.05)
    r = 0.065

    d1 = (math.log(spot / strike) + (r + 0.5 * iv_safe * iv_safe) * t_safe) / (iv_safe * math.sqrt(t_safe))
    d2 = d1 - iv_safe * math.sqrt(t_safe)

    pdf_d1 = _norm_pdf(d1)
    gamma = pdf_d1 / (spot * iv_safe * math.sqrt(t_safe))

    if opt_type == "CE":
        delta = _norm_cdf(d1)
        annual_theta = (-(spot * pdf_d1 * iv_safe) / (2.0 * math.sqrt(t_safe))) - (
            r * strike * math.exp(-r * t_safe) * _norm_cdf(d2)
        )
    else:
        delta = _norm_cdf(d1) - 1.0
        annual_theta = (-(spot * pdf_d1 * iv_safe) / (2.0 * math.sqrt(t_safe))) + (
            r * strike * math.exp(-r * t_safe) * _norm_cdf(-d2)
        )

    daily_theta = annual_theta / 365.0
    return {
        "delta": round(delta, 3),
        "gamma": round(gamma, 6),
        "theta": round(daily_theta, 2),
        "iv_pct": round(iv_safe * 100.0, 2),
    }


@st.cache_data(ttl=8)
def _fetch_real_angel_option_chain_raw(jwt_token, api_key, selected_idx):
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
            return None, None, "Unknown", 1
        spot_price = float(spot_res["data"]["fetched"][0]["ltp"])
    except Exception:
        return None, None, "Unknown", 1

    step = meta["step"]
    atm_strike = round(spot_price / step) * step
    exp_str, trading_dte, _ = get_live_expiry_info(selected_idx)
    if exp_str == "Unknown":
        return None, spot_price, "Unknown", 1

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

        min_s = atm_strike - (6 * step)
        max_s = atm_strike + (6 * step)
        target_df = exp_df[(exp_df["clean_strike"] >= min_s) & (exp_df["clean_strike"] <= max_s)].copy()
        tokens = target_df["token"].astype(str).tolist()
        if not tokens:
            return None, spot_price, exp_str, trading_dte

        contract_map = {
            (int(r["clean_strike"]), r["opt_type"]): str(r["token"])
            for _, r in target_df.iterrows()
        }

        fetched_map = {}
        q_res = requests.post(
            quote_url,
            json={"mode": "FULL", "exchangeTokens": {meta["opt_exch"]: tokens[:50]}},
            headers=headers,
            timeout=6,
        ).json()
        if q_res.get("status") and q_res.get("data", {}).get("fetched"):
            for item in q_res["data"]["fetched"]:
                fetched_map[str(item["symbolToken"])] = item

        if not fetched_map:
            return None, spot_price, exp_str, trading_dte

        rows = []
        for st_val in sorted(target_df["clean_strike"].unique()):
            ce_tok = contract_map.get((st_val, "CE"), "")
            pe_tok = contract_map.get((st_val, "PE"), "")
            ce_q = fetched_map.get(ce_tok, {})
            pe_q = fetched_map.get(pe_tok, {})
            rows.append({
                "CE_TOKEN": ce_tok,
                "PE_TOKEN": pe_tok,
                "STRIKE": st_val,
                "CALL LTP": float(ce_q.get("ltp", 0.0) or 0.0),
                "PUT LTP": float(pe_q.get("ltp", 0.0) or 0.0),
                "CALL OI": int(float(ce_q.get("opnInterest", 0) or 0)),
                "PUT OI": int(float(pe_q.get("opnInterest", 0) or 0)),
                "API_CALL_CHG": int(float(ce_q.get("netChangeOpenInterest", 0) or 0)),
                "API_PUT_CHG": int(float(pe_q.get("netChangeOpenInterest", 0) or 0)),
            })
        return pd.DataFrame(rows), spot_price, exp_str, trading_dte
    except Exception:
        return None, spot_price, exp_str, trading_dte


def evaluate_greeks_regime(selected_idx, spot_price, full_df, expiry_str="Unknown", oi_valid=True):
    """सही Delta, Theta, Gamma-IV सुरक्षा फ़िल्टर और सटीक OI से बाज़ार का निर्णय"""
    meta = get_index_meta(selected_idx)
    step = meta["step"]
    hedge_pts = meta["hedge"]
    atm_strike = round(spot_price / step) * step

    if full_df is None or full_df.empty or expiry_str in ["Unknown", "Simulation", "DEMO"]:
        return {
            "direction": "WAIT",
            "headline": "⏳ असली लाइव डेटा की प्रतीक्षा (WAIT)",
            "sub_headline": "बिना लाइव Angel One डेटा के कोई ट्रेड सिग्नल नहीं बनेगा",
            "bg_color": "linear-gradient(90deg, #334155, #1e293b)",
            "border_color": "#64748b",
            "efficiency_score": 0.0,
            "gamma_iv_risk": 0.0,
            "gamma_iv_score": 0.0,
            "atm_theta": 0.0,
            "net_delta": 0.0,
            "atm_iv": 0.0,
            "up_call_chg": 0,
            "dn_put_chg": 0,
            "trade_plan": "अभी ट्रेड न लें — लाइव डेटा कनेक्ट होने दें।",
            "delta_detail": "डेटा उपलब्ध नहीं",
            "theta_detail": "डेटा उपलब्ध नहीं",
            "gamma_detail": "डेटा उपलब्ध नहीं",
            "iv_detail": "डेटा उपलब्ध नहीं",
        }

    atm_row = full_df[full_df["STRIKE"] == atm_strike]
    if atm_row.empty:
        return {
            "direction": "WAIT",
            "headline": "⏳ ATM स्ट्राइक डेटा लोड हो रहा है...",
            "sub_headline": "कृपया कुछ सेकंड प्रतीक्षा करें",
            "bg_color": "linear-gradient(90deg, #334155, #1e293b)",
            "border_color": "#64748b",
            "efficiency_score": 0.0,
            "gamma_iv_risk": 0.0,
            "gamma_iv_score": 0.0,
            "atm_theta": 0.0,
            "net_delta": 0.0,
            "atm_iv": 0.0,
            "up_call_chg": 0,
            "dn_put_chg": 0,
            "trade_plan": "अभी ट्रेड न लें।",
            "delta_detail": "प्रतीक्षा...",
            "theta_detail": "प्रतीक्षा...",
            "gamma_detail": "प्रतीक्षा...",
            "iv_detail": "प्रतीक्षा...",
        }

    ce_ltp = float(atm_row["CALL LTP"].iloc[0])
    pe_ltp = float(atm_row["PUT LTP"].iloc[0])
    t_years = get_exact_t_years(expiry_str)

    ce_iv = _implied_volatility(ce_ltp, spot_price, atm_strike, t_years, "CE")
    pe_iv = _implied_volatility(pe_ltp, spot_price, atm_strike, t_years, "PE")

    ce_g = _calc_bs_greeks(spot_price, atm_strike, t_years, ce_iv, "CE")
    pe_g = _calc_bs_greeks(spot_price, atm_strike, t_years, pe_iv, "PE")

    # 1. Theta
    ce_theta_abs = round(abs(ce_g["theta"]), 2)
    pe_theta_abs = round(abs(pe_g["theta"]), 2)
    total_daily_theta = round(ce_theta_abs + pe_theta_abs, 2)
    theta_detail = (
        f"🟢 तेज़ प्रीमियम गलन (कुल ₹{total_daily_theta}/दिन | Call: ₹{ce_theta_abs}, Put: ₹{pe_theta_abs})"
        if total_daily_theta >= 18.0
        else f"🟡 सामान्य प्रीमियम गलन (कुल ₹{total_daily_theta}/दिन | Call: ₹{ce_theta_abs}, Put: ₹{pe_theta_abs})"
    )

    # 2. Delta
    net_delta = round(ce_g["delta"] + pe_g["delta"], 3)
    abs_net_delta = abs(net_delta)
    if net_delta >= 0.06:
        delta_detail = f"🟢 ऊपर का दबाव (Net Delta: {net_delta:+} | Call: {ce_g['delta']}, Put: {pe_g['delta']})"
    elif net_delta <= -0.06:
        delta_detail = f"🔴 नीचे का दबाव (Net Delta: {net_delta:+} | Call: {ce_g['delta']}, Put: {pe_g['delta']})"
    else:
        delta_detail = f"🟡 संतुलित बाज़ार (Net Delta: {net_delta:+} | Call: {ce_g['delta']}, Put: {pe_g['delta']})"

    # 3. Gamma-IV (सही नियम: हाई स्कोर = सेलर के लिए खतरा / No-Trade)
    one_pct_move = spot_price * 0.01
    delta_point_risk = max(1.0, abs_net_delta * one_pct_move)
    theta_delta_eff = round((total_daily_theta / delta_point_risk) * 10.0, 2)

    avg_gamma = round((ce_g["gamma"] + pe_g["gamma"]) / 2.0, 6)
    avg_iv_pct = round((ce_g["iv_pct"] + pe_g["iv_pct"]) / 2.0, 2)
    gamma_iv_risk = round(min(100.0, (avg_gamma * spot_price * avg_iv_pct) * 2.2), 1)

    if gamma_iv_risk >= 62.0:
        gamma_detail = f"🔴 अत्यधिक Gamma-IV झटका ({gamma_iv_risk}/100 | Gamma: {avg_gamma}) — अचानक तेज़ मूव का खतरा, नई सेलिंग से बचें!"
    elif gamma_iv_risk >= 48.0:
        gamma_detail = f"🟡 मध्यम हलचल ({gamma_iv_risk}/100 | Gamma: {avg_gamma}) — केवल साफ़ दिशा होने पर ही स्प्रेड बनाएं।"
    else:
        gamma_detail = f"🟢 शांत और स्थिर बाज़ार ({gamma_iv_risk}/100 | Gamma: {avg_gamma}) — ऑप्शन सेलिंग के लिए सुरक्षित।"

    # 4. OTM 3-Strike IV Skew (महँगी साइड बेचने का सिद्धांत)
    up_3_df = full_df[(full_df["STRIKE"] > atm_strike) & (full_df["STRIKE"] <= atm_strike + 3 * step)]
    dn_3_df = full_df[(full_df["STRIKE"] < atm_strike) & (full_df["STRIKE"] >= atm_strike - 3 * step)]

    otm_ce_ivs = [
        _implied_volatility(float(r["CALL LTP"]), spot_price, int(r["STRIKE"]), t_years, "CE") * 100.0
        for _, r in up_3_df.iterrows() if float(r["CALL LTP"]) > 0.5
    ]
    otm_pe_ivs = [
        _implied_volatility(float(r["PUT LTP"]), spot_price, int(r["STRIKE"]), t_years, "PE") * 100.0
        for _, r in dn_3_df.iterrows() if float(r["PUT LTP"]) > 0.5
    ]
    avg_otm_ce_iv = round(sum(otm_ce_ivs) / len(otm_ce_ivs), 2) if otm_ce_ivs else ce_g["iv_pct"]
    avg_otm_pe_iv = round(sum(otm_pe_ivs) / len(otm_pe_ivs), 2) if otm_pe_ivs else pe_g["iv_pct"]
    iv_diff = round(avg_otm_pe_iv - avg_otm_ce_iv, 2)

    if iv_diff > 1.2:
        iv_detail = f"🟢 Put IV ज़्यादा महँगी है (OTM Put IV: {avg_otm_pe_iv}% vs Call IV: {avg_otm_ce_iv}%) — नीचे मज़बूत सपोर्ट और Bull Put में ज़्यादा प्रीमियम।"
    elif iv_diff < -1.2:
        iv_detail = f"🔴 Call IV ज़्यादा महँगी है (OTM Call IV: {avg_otm_ce_iv}% vs Put IV: {avg_otm_pe_iv}%) — ऊपर से दबाव और Bear Call में ज़्यादा प्रीमियम।"
    else:
        iv_detail = f"🟡 Call व Put IV संतुलित हैं (OTM Call IV: {avg_otm_ce_iv}% | OTM Put IV: {avg_otm_pe_iv}%) — कोई घबराहट नहीं।"

    # 5. असली Change in OI दिशा
    up_call_chg = int(up_3_df["CALL CHG OI"].sum()) if (oi_valid and not up_3_df.empty) else 0
    dn_put_chg = int(dn_3_df["PUT CHG OI"].sum()) if (oi_valid and not dn_3_df.empty) else 0

    if not oi_valid:
        return {
            "direction": "WAIT",
            "headline": "⏳ कल का क्लोज़िंग OI उपलब्ध नहीं (WAIT)",
            "sub_headline": "गलत Change in OI से बचने के लिए अभी सिग्नल होल्ड पर है",
            "bg_color": "linear-gradient(90deg, #334155, #1e293b)",
            "border_color": "#f59e0b",
            "efficiency_score": theta_delta_eff,
            "gamma_iv_risk": gamma_iv_risk,
            "gamma_iv_score": gamma_iv_risk,
            "atm_theta": total_daily_theta,
            "net_delta": net_delta,
            "atm_iv": avg_iv_pct,
            "up_call_chg": 0,
            "dn_put_chg": 0,
            "trade_plan": "कल का क्लोज़िंग OI सेव होने दें — आज गलत आंकड़ों पर ट्रेड न लें।",
            "delta_detail": delta_detail,
            "theta_detail": theta_detail,
            "gamma_detail": gamma_detail,
            "iv_detail": iv_detail,
        }

    # हाई Gamma-IV होने पर सेलर को सुरक्षित रखें
    if gamma_iv_risk >= 65.0:
        return {
            "direction": "WAIT",
            "headline": "🚨 अत्यधिक Gamma-IV खतरा — ट्रेड से बचें (NO-TRADE)",
            "sub_headline": f"Gamma-IV स्कोर {gamma_iv_risk}/100 है — अचानक बड़े झटके का जोखिम",
            "bg_color": "linear-gradient(90deg, #4c0519, #1e1b4b)",
            "border_color": "#f43f5e",
            "efficiency_score": theta_delta_eff,
            "gamma_iv_risk": gamma_iv_risk,
            "gamma_iv_score": gamma_iv_risk,
            "atm_theta": total_daily_theta,
            "net_delta": net_delta,
            "atm_iv": avg_iv_pct,
            "up_call_chg": up_call_chg,
            "dn_put_chg": dn_put_chg,
            "trade_plan": "अभी बाज़ार बहुत अस्थिर है — कोई नई पोजीशन न बनाएं।",
            "delta_detail": delta_detail,
            "theta_detail": theta_detail,
            "gamma_detail": gamma_detail,
            "iv_detail": iv_detail,
        }

    bullish_points = 0
    bearish_points = 0

    if dn_put_chg > 0 and dn_put_chg >= up_call_chg * 1.25:
        bullish_points += 2
    elif up_call_chg > 0 and up_call_chg >= dn_put_chg * 1.25:
        bearish_points += 2

    if net_delta >= 0.06:
        bullish_points += 1
    elif net_delta <= -0.06:
        bearish_points += 1

    if iv_diff > 1.2:
        bullish_points += 1
    elif iv_diff < -1.2:
        bearish_points += 1

    if bullish_points >= 2 and bullish_points > bearish_points:
        direction = "UP"
        headline = "🟢 आज मार्केट ऊपर जा सकता है (BULLISH)"
        sub_headline = f"नीचे Put राइटिंग मज़बूत है ({dn_put_chg:+,} vs {up_call_chg:+,}) | Theta/Delta क्षमता: {theta_delta_eff}"
        bg_color = "linear-gradient(90deg, #064e3b, #022c22)"
        border_color = "#10b981"
        trade_plan = f"क्या करें: {atm_strike} PE बेचें और {atm_strike - hedge_pts} PE हेज खरीदें (Bull Put Spread)।"
    elif bearish_points >= 2 and bearish_points > bullish_points:
        direction = "DOWN"
        headline = "🔴 आज मार्केट नीचे जा सकता है (BEARISH)"
        sub_headline = f"ऊपर Call राइटिंग भारी है ({up_call_chg:+,} vs {dn_put_chg:+,}) | Theta/Delta क्षमता: {theta_delta_eff}"
        bg_color = "linear-gradient(90deg, #7f1d1d, #450a0a)"
        border_color = "#ef4444"
        trade_plan = f"क्या करें: {atm_strike} CE बेचें और {atm_strike + hedge_pts} CE हेज खरीदें (Bear Call Spread)।"
    elif (up_call_chg > 0 or dn_put_chg > 0) and gamma_iv_risk < 50.0 and abs_net_delta < 0.08:
        direction = "SIDEWAYS"
        headline = "🟡 आज मार्केट साइडवेज रहेगा (RANGE-BOUND)"
        sub_headline = f"Theta गलन ₹{total_daily_theta}/दिन | Gamma-IV शांत ({gamma_iv_risk}/100) — रेंज में रहने के आसार"
        bg_color = "linear-gradient(90deg, #78350f, #451a03)"
        border_color = "#f59e0b"
        trade_plan = f"क्या करें: {atm_strike} CE+PE Straddle बेचें + {atm_strike + hedge_pts} CE और {atm_strike - hedge_pts} PE हेज खरीदें।"
    else:
        direction = "WAIT"
        headline = "⏳ स्पष्ट सिग्नल की प्रतीक्षा (WAIT)"
        sub_headline = "OI और Greeks में अभी एकतरफ़ा साफ़ सहमति नहीं बनी है"
        bg_color = "linear-gradient(90deg, #334155, #1e293b)"
        border_color = "#94a3b8"
        trade_plan = "जबरदस्ती एंट्री न लें — साफ़ OI और Greeks सहमति का इंतज़ार करें।"

    return {
        "direction": direction,
        "headline": headline,
        "sub_headline": sub_headline,
        "bg_color": bg_color,
        "border_color": border_color,
        "efficiency_score": theta_delta_eff,
        "gamma_iv_risk": gamma_iv_risk,
        "gamma_iv_score": gamma_iv_risk,
        "atm_theta": total_daily_theta,
        "net_delta": net_delta,
        "atm_iv": avg_iv_pct,
        "up_call_chg": up_call_chg,
        "dn_put_chg": dn_put_chg,
        "trade_plan": trade_plan,
        "delta_detail": delta_detail,
        "theta_detail": theta_detail,
        "gamma_detail": gamma_detail,
        "iv_detail": iv_detail,
    }


def render_greeks_detail_box(selected_idx, spot_price, full_df, expiry_str="Unknown", oi_valid=True, oi_source=""):
    g_info = evaluate_greeks_regime(selected_idx, spot_price, full_df, expiry_str, oi_valid=oi_valid)

    st.markdown(f"""
    <div style="background: {g_info['bg_color']}; padding: 18px 20px; border-radius: 12px; border-left: 6px solid {g_info['border_color']}; color: white; margin-top: 10px; margin-bottom: 12px;">
        <div style="font-size: 13px; color: #cbd5e1; margin-bottom: 4px;">🧭 Greeks + OI बाज़ार का साफ़ फैसला ({selected_idx} | Expiry: {expiry_str})</div>
        <div style="font-size: 24px; font-weight: bold; margin-bottom: 6px;">{g_info['headline']}</div>
        <div style="font-size: 14px; color: #e2e8f0; margin-bottom: 8px;">{g_info['sub_headline']}</div>
        <div style="font-size: 15px; font-weight: bold; color: #fde047;">👉 {g_info['trade_plan']}</div>
    </div>
    """, unsafe_allow_html=True)

    chg_txt = (
        f"ऊपर 3 Call Chg OI: `{g_info['up_call_chg']:+,}` &nbsp;|&nbsp; नीचे 3 Put Chg OI: `{g_info['dn_put_chg']:+,}`"
        if oi_valid else "`N/A (कल का क्लोज़ OI उपलब्ध नहीं)`"
    )
    st.markdown(f"📊 **Change in OI ({oi_source}):** {chg_txt}")
    st.markdown("#### 🔍 चारों Greeks की अलग-अलग डिटेल रिपोर्ट:")
    c1, c2 = st.columns(2)
    with c1:
        st.info(f"**1️⃣ Delta (दिशा की ताकत):**\n\n{g_info['delta_detail']}")
        st.warning(f"**3️⃣ Gamma (अचानक झटके का खतरा):**\n\n{g_info['gamma_detail']}")
    with c2:
        st.success(f"**2️⃣ Theta (प्रीमियम गलन / सेलर प्रॉफिट):**\n\n{g_info['theta_detail']}")
        st.info(f"**4️⃣ IV (ऑप्शन की मँहगाई और डर):**\n\n{g_info['iv_detail']}")

    return g_info


def _run_standalone_page():
    st.subheader("🧭 लाइव Greeks बाज़ार दिशा इंडिकेटर (100% Real Angel One Data)")
    col_a, col_b = st.columns([3, 1])
    with col_a:
        sel = st.selectbox("इंडेक्स चुनें:", ["Nifty 50", "Bank Nifty", "Sensex"], key="standalone_greeks_idx")
    with col_b:
        st.write("")
        st.write("")
        if st.button("🔄 डेटा रिफ्रेश करें", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    jwt, akey = ensure_angel_session()
    if not jwt or not akey:
        st.error("🚨 Angel One SmartAPI कनेक्ट नहीं है। कृपया पहले मुख्य पेज पर लॉगिन जाँचें।")
        return

    raw_df, spot, exp_str, trading_dte = _fetch_real_angel_option_chain_raw(jwt, akey, sel)
    if raw_df is None or raw_df.empty or not spot:
        st.warning("⚠️ अभी Angel One से लाइव ऑप्शन चेन डेटा नहीं मिल रहा है।")
        return

    df_live, oi_valid, oi_source = compute_accurate_chg_oi(sel, raw_df)
    meta = get_index_meta(sel)
    atm = round(spot / meta["step"]) * meta["step"]
    ist_now = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%I:%M:%S %p")
    st.caption(
        f"🟢 **ANGEL ONE LIVE** | 📅 **Expiry:** `{exp_str}` (**`{trading_dte} DTE`**) | ⏱️ **समय:** `{ist_now}` | 📍 **{sel} Spot:** `{spot:,.2f}` | 🎯 **ATM:** `{atm}`"
    )
    render_greeks_detail_box(sel, spot, df_live, exp_str, oi_valid=oi_valid, oi_source=oi_source)


if __name__ == "__main__" or __name__ in ["greeks_regime_indicator", "greek_regime_indicator"]:
    _run_standalone_page()
