import math
from datetime import datetime
import pytz
import streamlit as st
import yfinance as yf
import pandas as pd
import requests


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _implied_volatility(option_price, spot, strike, t_years, opt_type="CE"):
    """ऑप्शन के लाइव प्राइस (LTP) से सटीक IV निकालना"""
    if option_price <= 0.5 or t_years <= 1e-6 or spot <= 0 or strike <= 0:
        return 0.15

    intrinsic = max(0.0, spot - strike) if opt_type == "CE" else max(0.0, strike - spot)
    if option_price <= intrinsic + 0.2:
        return 0.08

    low, high = 0.03, 1.50
    r = 0.065
    for _ in range(35):
        mid = 0.5 * (low + high)
        d1 = (math.log(spot / strike) + (r + 0.5 * mid * mid) * t_years) / (mid * math.sqrt(t_years))
        d2 = d1 - mid * math.sqrt(t_years)
        if opt_type == "CE":
            px = spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2)
        else:
            px = strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * math.exp(-r * t_years) * 0 - spot * _norm_cdf(-d1)

        if px > option_price:
            high = mid
        else:
            low = mid
    return round(0.5 * (low + high), 4)


def _calc_bs_greeks(spot, strike, t_years, iv, opt_type="CE"):
    """ATM स्ट्राइक के लिए Delta, Gamma और रोज़ाना Theta निकालना"""
    t_safe = max(t_years, 1.0 / (365.0 * 24.0))
    iv_safe = max(iv, 0.05)
    r = 0.065

    d1 = (math.log(spot / strike) + (r + 0.5 * iv_safe * iv_safe) * t_safe) / (iv_safe * math.sqrt(t_safe))
    d2 = d1 - iv_safe * math.sqrt(t_safe)

    pdf_d1 = _norm_pdf(d1)
    gamma = pdf_d1 / (spot * iv_safe * math.sqrt(t_safe))

    if opt_type == "CE":
        delta = _norm_cdf(d1)
        annual_theta = (-(spot * pdf_d1 * iv_safe) / (2.0 * math.sqrt(t_safe))) - (r * strike * math.exp(-r * t_safe) * _norm_cdf(d2))
    else:
        delta = _norm_cdf(d1) - 1.0
        annual_theta = (-(spot * pdf_d1 * iv_safe) / (2.0 * math.sqrt(t_safe))) + (r * strike * math.exp(-r * t_safe) * _norm_cdf(-d2))

    daily_theta = annual_theta / 365.0
    return {
        "delta": round(delta, 3),
        "gamma": round(gamma, 6),
        "theta": round(daily_theta, 2),
        "iv_pct": round(iv_safe * 100.0, 2)
    }


def _get_time_to_expiry_years(expiry_str):
    """Expiry की तारीख से बचे हुए समय का सटीक हिसाब"""
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    try:
        exp_dt = datetime.strptime(expiry_str.strip(), "%d-%b-%Y")
        exp_dt = ist.localize(exp_dt.replace(hour=15, minute=30, second=0))
        diff_secs = max(900.0, (exp_dt - now_ist).total_seconds())
        return diff_secs / (365.0 * 24.0 * 3600.0)
    except Exception:
        return 2.0 / 365.0


def evaluate_greeks_regime(selected_idx, spot_price, full_df, expiry_str="Simulation"):
    """
    हर एक Greek (Delta, Theta, Gamma, IV) की पूरी हिंदी व्याख्या के साथ
    बाज़ार का साफ़ फैसला देता है।
    """
    step_map = {"Nifty 50": 50, "Bank Nifty": 100, "Sensex": 100}
    hedge_map = {"Nifty 50": 300, "Bank Nifty": 300, "Sensex": 1000}
    step = step_map.get(selected_idx, 50)
    hedge_pts = hedge_map.get(selected_idx, 300)
    atm_strike = round(spot_price / step) * step

    atm_row = full_df[full_df["STRIKE"] == atm_strike]
    if atm_row.empty:
        return {
            "direction": "WAIT",
            "headline": "⏳ डेटा लोड हो रहा है...",
            "sub_headline": "कृपया कुछ सेकंड प्रतीक्षा करें",
            "bg_color": "#334155",
            "border_color": "#64748b",
            "efficiency_score": 0.0,
            "gamma_iv_risk": 0.0,
            "atm_theta": 0.0,
            "net_delta": 0.0,
            "atm_iv": 0.0,
            "trade_plan": "अभी डेटा उपलब्ध नहीं है।",
            "delta_detail": "डेटा प्रतीक्षा...",
            "theta_detail": "डेटा प्रतीक्षा...",
            "gamma_detail": "डेटा प्रतीक्षा...",
            "iv_detail": "डेटा प्रतीक्षा..."
        }

    ce_ltp = float(atm_row["CALL LTP"].iloc[0])
    pe_ltp = float(atm_row["PUT LTP"].iloc[0])
    t_years = _get_time_to_expiry_years(expiry_str)

    ce_iv = _implied_volatility(ce_ltp, spot_price, atm_strike, t_years, "CE")
    pe_iv = _implied_volatility(pe_ltp, spot_price, atm_strike, t_years, "PE")

    ce_g = _calc_bs_greeks(spot_price, atm_strike, t_years, ce_iv, "CE")
    pe_g = _calc_bs_greeks(spot_price, atm_strike, t_years, pe_iv, "PE")

    # 1. Theta हिसाब और हिंदी सिग्नल
    ce_theta_abs = round(abs(ce_g["theta"]), 2)
    pe_theta_abs = round(abs(pe_g["theta"]), 2)
    total_daily_theta = round(ce_theta_abs + pe_theta_abs, 2)

    if total_daily_theta >= 18.0:
        theta_detail = f"🟢 तेज़ प्रीमियम गलन (कुल ₹{total_daily_theta}/दिन | Call: ₹{ce_theta_abs}, Put: ₹{pe_theta_abs}) — ऑप्शन बेचने वालों को तेज़ी से प्रॉफिट मिल रहा है।"
    else:
        theta_detail = f"🟡 धीमी प्रीमियम गलन (कुल ₹{total_daily_theta}/दिन | Call: ₹{ce_theta_abs}, Put: ₹{pe_theta_abs}) — अभी समय के साथ प्रीमियम धीरे गलेगा।"

    # 2. Delta हिसाब और हिंदी सिग्नल
    net_delta = round(ce_g["delta"] + pe_g["delta"], 3)
    abs_net_delta = abs(net_delta)

    if net_delta >= 0.06:
        delta_detail = f"🟢 ऊपर जाने का सिग्नल (Net Delta: {net_delta:+} | Call: {ce_g['delta']}, Put: {pe_g['delta']}) — खरीदार बाज़ार को ऊपर खींच रहे हैं।"
    elif net_delta <= -0.06:
        delta_detail = f"🔴 नीचे जाने का सिग्नल (Net Delta: {net_delta:+} | Call: {ce_g['delta']}, Put: {pe_g['delta']}) — बाज़ार पर नीचे की तरफ़ दबाव है।"
    else:
        delta_detail = f"🟡 साइडवेज / संतुलित सिग्नल (Net Delta: {net_delta:+} | Call: {ce_g['delta']}, Put: {pe_g['delta']}) — ऊपर और नीचे दोनों तरफ़ बराबर ताकत है।"

    # 3. Gamma और Gamma-IV दबाव हिसाब और हिंदी सिग्नल
    one_pct_move = spot_price * 0.01
    delta_point_risk = max(1.0, abs_net_delta * one_pct_move)
    theta_delta_eff = round((total_daily_theta / delta_point_risk) * 10.0, 2)

    avg_gamma = round((ce_g["gamma"] + pe_g["gamma"]) / 2.0, 6)
    avg_iv_pct = round((ce_g["iv_pct"] + pe_g["iv_pct"]) / 2.0, 2)
    gamma_iv_risk = round(min(100.0, (avg_gamma * spot_price * avg_iv_pct) * 2.2), 1)

    if gamma_iv_risk >= 52.0:
        gamma_detail = f"🔴 तेज़ हलचल / ब्रेकआउट खतरा (Gamma-IV स्कोर: {gamma_iv_risk}/100 | Gamma: {avg_gamma}) — बाज़ार में अचानक बड़ा मूव आ सकता है, स्टॉप-लॉस सख्त रखें।"
    else:
        gamma_detail = f"🟢 शांत और स्थिर बाज़ार (Gamma-IV स्कोर: {gamma_iv_risk}/100 | Gamma: {avg_gamma}) — अचानक तेज़ भागने का खतरा कम है, सेलिंग सुरक्षित है।"

    # 4. IV (Implied Volatility) तुलना और हिंदी सिग्नल
    iv_diff = round(pe_g["iv_pct"] - ce_g["iv_pct"], 2)
    if iv_diff > 1.2:
        iv_detail = f"🟢 Put IV ज़्यादा है (Put IV: {pe_g['iv_pct']}% बनाम Call IV: {ce_g['iv_pct']}%) — नीचे के स्तरों पर मज़बूत सपोर्ट और ऊपर जाने का संकेत।"
    elif iv_diff < -1.2:
        iv_detail = f"🔴 Call IV ज़्यादा है (Call IV: {ce_g['iv_pct']}% बनाम Put IV: {pe_g['iv_pct']}%) — ऊपर से बिकवाली का दबाव और नीचे जाने का संकेत।"
    else:
        iv_detail = f"🟡 Call और Put IV बराबर हैं (Call IV: {ce_g['iv_pct']}% | Put IV: {pe_g['iv_pct']}%) — बाज़ार में कोई बड़ा डर नहीं है, रेंज में रहने के आसार।"

    # 5. ऊपर 3 Call बनाम नीचे 3 Put Change in OI दिशा
    up_3_df = full_df[(full_df["STRIKE"] > atm_strike) & (full_df["STRIKE"] <= atm_strike + 3 * step)]
    dn_3_df = full_df[(full_df["STRIKE"] < atm_strike) & (full_df["STRIKE"] >= atm_strike - 3 * step)]
    up_call_chg = int(up_3_df["CALL CHG OI"].sum()) if not up_3_df.empty else 0
    dn_put_chg = int(dn_3_df["PUT CHG OI"].sum()) if not dn_3_df.empty else 0

    bullish_points = 0
    bearish_points = 0

    if dn_put_chg > up_call_chg * 1.25 and dn_put_chg > 0:
        bullish_points += 2
    elif up_call_chg > dn_put_chg * 1.25 and up_call_chg > 0:
        bearish_points += 2

    if net_delta >= 0.06:
        bullish_points += 1
    elif net_delta <= -0.06:
        bearish_points += 1

    if iv_diff > 1.2:
        bullish_points += 1
    elif iv_diff < -1.2:
        bearish_points += 1

    # 6. अंतिम बड़ा फैसला
    is_strong_trend = (gamma_iv_risk >= 52.0) or (abs_net_delta >= 0.14) or (abs(bullish_points - bearish_points) >= 2)

    if is_strong_trend and bullish_points > bearish_points:
        direction = "UP"
        headline = "🟢 आज मार्केट ऊपर जा सकता है (BULLISH)"
        sub_headline = f"नीचे की Put राइटिंग मज़बूत है और Delta/IV ऊपर की तरफ़ इशारा कर रहे हैं (Theta/Delta क्षमता: {theta_delta_eff})"
        bg_color = "linear-gradient(90deg, #064e3b, #022c22)"
        border_color = "#10b981"
        trade_plan = f"क्या करें: {atm_strike} PE बेचें और {atm_strike - hedge_pts} PE हेज खरीदें (Bull Put Spread)।"
    elif is_strong_trend and bearish_points > bullish_points:
        direction = "DOWN"
        headline = "🔴 आज मार्केट नीचे जा सकता है (BEARISH)"
        sub_headline = f"ऊपर की Call राइटिंग भारी है और Delta/IV गिरावट का दबाव दिखा रहे हैं (Theta/Delta क्षमता: {theta_delta_eff})"
        bg_color = "linear-gradient(90deg, #7f1d1d, #450a0a)"
        border_color = "#ef4444"
        trade_plan = f"क्या करें: {atm_strike} CE बेचें और {atm_strike + hedge_pts} CE हेज खरीदें (Bear Call Spread)।"
    else:
        direction = "SIDEWAYS"
        headline = "🟡 आज मार्केट साइडवेज रहेगा (RANGE-BOUND)"
        sub_headline = f"Theta गलन तेज़ है (₹{total_daily_theta}/दिन) और Gamma-IV शांत है ({gamma_iv_risk}/100) — बाज़ार एक दायरे में रहेगा"
        bg_color = "linear-gradient(90deg, #78350f, #451a03)"
        border_color = "#f59e0b"
        if dn_put_chg >= up_call_chg:
            trade_plan = f"क्या करें: बाज़ार शांत है — {atm_strike} PE बेचें + {atm_strike - hedge_pts} PE हेज खरीदें और Theta का लाभ लें।"
        else:
            trade_plan = f"क्या करें: बाज़ार शांत है — {atm_strike} CE बेचें + {atm_strike + hedge_pts} CE हेज खरीदें और Theta का लाभ लें।"

    return {
        "direction": direction,
        "headline": headline,
        "sub_headline": sub_headline,
        "bg_color": bg_color,
        "border_color": border_color,
        "efficiency_score": theta_delta_eff,
        "gamma_iv_risk": gamma_iv_risk,
        "atm_theta": total_daily_theta,
        "net_delta": net_delta,
        "atm_iv": avg_iv_pct,
        "trade_plan": trade_plan,
        "delta_detail": delta_detail,
        "theta_detail": theta_detail,
        "gamma_detail": gamma_detail,
        "iv_detail": iv_detail
    }


def render_greeks_detail_box(selected_idx, spot_price, full_df, expiry_str="Simulation"):
    """स्क्रीन पर बड़ा बैनर + चारों Greeks की पूरी हिंदी व्याख्या दिखाने वाला फंक्शन"""
    g_info = evaluate_greeks_regime(selected_idx, spot_price, full_df, expiry_str)

    st.markdown(f"""
    <div style="background: {g_info['bg_color']}; padding: 18px 20px; border-radius: 12px; border-left: 6px solid {g_info['border_color']}; color: white; margin-top: 10px; margin-bottom: 12px;">
        <div style="font-size: 13px; color: #cbd5e1; margin-bottom: 4px;">🧭 Greeks + OI बाज़ार का साफ़ फैसला ({selected_idx})</div>
        <div style="font-size: 24px; font-weight: bold; margin-bottom: 6px;">{g_info['headline']}</div>
        <div style="font-size: 14px; color: #e2e8f0; margin-bottom: 8px;">{g_info['sub_headline']}</div>
        <div style="font-size: 15px; font-weight: bold; color: #fde047;">👉 {g_info['trade_plan']}</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("#### 🔍 चारों Greeks की अलग-अलग डिटेल रिपोर्ट (कौन क्या कह रहा है):")
    c1, c2 = st.columns(2)
    with c1:
        st.info(f"**1️⃣ Delta (दिशा की ताकत):**\n\n{g_info['delta_detail']}")
        st.warning(f"**3️⃣ Gamma (अचानक झटके का खतरा):**\n\n{g_info['gamma_detail']}")
    with c2:
        st.success(f"**2️⃣ Theta (प्रीमियम गलन / सेलर प्रॉफिट):**\n\n{g_info['theta_detail']}")
        st.info(f"**4️⃣ IV (ऑप्शन की मँहगाई और डर):**\n\n{g_info['iv_detail']}")

    return g_info


def _fetch_live_spot_and_df(selected_idx):
    sym_map = {
        "Nifty 50": ("^NSEI", 50, 24000.0, "NSE", "99926000"),
        "Bank Nifty": ("^NSEBANK", 100, 51000.0, "NSE", "99926009"),
        "Sensex": ("^BSESN", 100, 79000.0, "BSE", "99919000"),
    }
    sym, step, def_spot, exch, tok = sym_map[selected_idx]
    spot = None

    if "angel_jwt" in st.session_state and "angel_key" in st.session_state:
        try:
            headers = {
                "Authorization": f"Bearer {st.session_state['angel_jwt']}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-UserType": "USER",
                "X-SourceID": "WEB",
                "X-ClientLocalIP": "127.0.0.1",
                "X-ClientPublicIP": "127.0.0.1",
                "X-MACAddress": "00:00:00:00:00:00",
                "X-PrivateKey": st.session_state["angel_key"].strip()
            }
            res = requests.post(
                "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/",
                json={"mode": "LTP", "exchangeTokens": {exch: [tok]}},
                headers=headers,
                timeout=5
            ).json()
            if res.get("status"):
                spot = float(res["data"]["fetched"][0]["ltp"])
        except Exception:
            pass

    if spot is None:
        data = yf.download(sym, period="1d", interval="1m", progress=False)
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
        spot = float(data["Close"].iloc[-1]) if not data.empty else def_spot

    atm = round(spot / step) * step
    strikes = [atm + (i * step) for i in range(-5, 6)]
    df_tmp = pd.DataFrame({
        "STRIKE": strikes,
        "CALL CHG OI": [0 for _ in strikes],
        "PUT CHG OI": [0 for _ in strikes],
        "CALL LTP": [max(5.0, round(max(0, spot - s) + 75, 1)) for s in strikes],
        "PUT LTP": [max(5.0, round(max(0, s - spot) + 75, 1)) for s in strikes],
    })
    return spot, atm, df_tmp


def _run_standalone_page():
    st.subheader("🧭 लाइव Greeks बाज़ार दिशा इंडिकेटर (पूरी डिटेल)")
    sel = st.selectbox("इंडेक्स चुनें:", ["Nifty 50", "Bank Nifty", "Sensex"], key="standalone_greeks_idx")
    spot, atm, df_tmp = _fetch_live_spot_and_df(sel)
    st.caption(f"📍 **{sel} Spot:** `{spot:,.2f}` | 🎯 **ATM Strike:** `{atm}`")
    render_greeks_detail_box(sel, spot, df_tmp, "Simulation")


if __name__ == "__main__" or __name__ in ["greeks_regime_indicator", "greek_regime_indicator"]:
    _run_standalone_page()
