import math
from datetime import datetime
import pytz
import pandas as pd
import streamlit as st

def _norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def _bs_price(S, K, T, r, sigma, option_type="CE"):
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if option_type == "CE" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "CE":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

def calculate_otm_iv(ltp, spot, strike, expiry_str, option_type="CE", r=0.065):
    """केवल OTM ऑप्शन के LTP और एक्सपायरी तारीख से सटीक IV (%) निकालता है"""
    if ltp <= 0.5 or spot <= 0:
        return 0.0

    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)

    try:
        exp_dt = datetime.strptime(expiry_str, "%d-%b-%Y").replace(hour=15, minute=30)
        exp_dt = ist.localize(exp_dt)
        seconds_left = max((exp_dt - now_ist).total_seconds(), 3600)
    except Exception:
        seconds_left = 2 * 24 * 3600

    T = seconds_left / (365.0 * 24 * 3600)

    low, high = 0.01, 3.0
    for _ in range(40):
        mid = (low + high) / 2.0
        price = _bs_price(spot, strike, T, r, mid, option_type)
        if price < ltp:
            low = mid
        else:
            high = mid
    return round(((low + high) / 2.0) * 100, 2)

def evaluate_otm_iv_from_df(selected_idx, spot_price, full_df, expiry_label):
    if selected_idx == "Nifty 50":
        step, hedge_dist = 50, 300
    elif selected_idx == "Bank Nifty":
        step, hedge_dist = 100, 300
    else:
        step, hedge_dist = 100, 1000

    atm_strike = round(spot_price / step) * step

    otm_ce_strikes = [atm_strike + step, atm_strike + 2 * step, atm_strike + 3 * step]
    otm_pe_strikes = [atm_strike - step, atm_strike - 2 * step, atm_strike - 3 * step]

    up_3_df = full_df[full_df["STRIKE"].isin(otm_ce_strikes)]
    down_3_df = full_df[full_df["STRIKE"].isin(otm_pe_strikes)]

    top3_ce_chg_oi = int(up_3_df["CALL CHG OI"].sum()) if not up_3_df.empty else 0
    bottom3_pe_chg_oi = int(down_3_df["PUT CHG OI"].sum()) if not down_3_df.empty else 0

    top3_ce_ivs = []
    for _, row in up_3_df.iterrows():
        iv = calculate_otm_iv(float(row["CALL LTP"]), spot_price, int(row["STRIKE"]), expiry_label, "CE")
        if iv > 0:
            top3_ce_ivs.append(iv)

    bottom3_pe_ivs = []
    for _, row in down_3_df.iterrows():
        iv = calculate_otm_iv(float(row["PUT LTP"]), spot_price, int(row["STRIKE"]), expiry_label, "PE")
        if iv > 0:
            bottom3_pe_ivs.append(iv)

    avg_otm_ce_iv = round(sum(top3_ce_ivs) / len(top3_ce_ivs), 2) if top3_ce_ivs else 0.0
    avg_otm_pe_iv = round(sum(bottom3_pe_ivs) / len(bottom3_pe_ivs), 2) if bottom3_pe_ivs else 0.0

    ist = pytz.timezone('Asia/Kolkata')
    now_min = datetime.now(ist).strftime("%H:%M")
    state_key = f"otm_iv_hist_{selected_idx}"

    if state_key not in st.session_state:
        st.session_state[state_key] = []

    hist = st.session_state[state_key]
    if not hist or hist[-1]["time"] != now_min:
        hist.append({"time": now_min, "ce_iv": avg_otm_ce_iv, "pe_iv": avg_otm_pe_iv})
        if len(hist) > 30:
            hist.pop(0)

    ref_idx = -15 if len(hist) >= 15 else 0
    prev_ce_iv = hist[ref_idx]["ce_iv"] if hist else avg_otm_ce_iv
    prev_pe_iv = hist[ref_idx]["pe_iv"] if hist else avg_otm_pe_iv

    ce_iv_change = round(avg_otm_ce_iv - prev_ce_iv, 2)
    pe_iv_change = round(avg_otm_pe_iv - prev_pe_iv, 2)

    decision = {
        "signal": "WAIT",
        "spread_name": "कोई स्पष्ट सेटअप नहीं",
        "sell_leg": "-",
        "buy_hedge": "-",
        "avg_otm_ce_iv": avg_otm_ce_iv,
        "avg_otm_pe_iv": avg_otm_pe_iv,
        "ce_iv_change": ce_iv_change,
        "pe_iv_change": pe_iv_change,
        "top3_ce_chg_oi": top3_ce_chg_oi,
        "bottom3_pe_chg_oi": bottom3_pe_chg_oi,
        "message": ""
    }

    if bottom3_pe_chg_oi > top3_ce_chg_oi * 1.15:
        if pe_iv_change <= -0.15:
            decision.update({
                "signal": "BUY_SIGNAL",
                "spread_name": "BULL PUT SPREAD (मजबूत अपट्रेंड)",
                "sell_leg": f"{atm_strike} PE",
                "buy_hedge": f"{atm_strike - hedge_dist} PE",
                "message": f"नीचे की 3 स्ट्राइक्स में Put Chg OI ज़्यादा है और OTM Put IV {abs(pe_iv_change)}% गिरा है (असली अपट्रेंड कन्फर्म)।"
            })
        elif pe_iv_change > 0.25:
            decision.update({
                "signal": "TRAP",
                "spread_name": "ट्रेड न लें (Put Writers Trap)",
                "message": f"Put Chg OI ज़्यादा है, पर OTM Put IV +{pe_iv_change}% बढ़ रहा है — नीचे पलटने का खतरा!"
            })
        else:
            decision.update({
                "signal": "WAIT",
                "spread_name": "Bull Put Spread (IV गिरने का इंतज़ार)",
                "message": f"Put Chg OI ज़्यादा है, पर OTM Put IV अभी गिरना शुरू नहीं हुआ ({pe_iv_change:+}%)।"
            })

    elif top3_ce_chg_oi > bottom3_pe_chg_oi * 1.15:
        if ce_iv_change <= -0.15:
            decision.update({
                "signal": "BUY_SIGNAL",
                "spread_name": "BEAR CALL SPREAD (मजबूत डाउनट्रेंड)",
                "sell_leg": f"{atm_strike} CE",
                "buy_hedge": f"{atm_strike + hedge_dist} CE",
                "message": f"ऊपर की 3 स्ट्राइक्स में Call Chg OI ज़्यादा है और OTM Call IV {abs(ce_iv_change)}% गिरा है (असली डाउनट्रेंड कन्फर्म)।"
            })
        elif ce_iv_change > 0.25:
            decision.update({
                "signal": "TRAP",
                "spread_name": "ट्रेड न लें (Call Writers Trap)",
                "message": f"Call Chg OI ज़्यादा है, पर OTM Call IV +{ce_iv_change}% बढ़ रहा है — ऊपर भागने का खतरा!"
            })
        else:
            decision.update({
                "signal": "WAIT",
                "spread_name": "Bear Call Spread (IV गिरने का इंतज़ार)",
                "message": f"Call Chg OI ज़्यादा है, पर OTM Call IV अभी गिरना शुरू नहीं हुआ ({ce_iv_change:+}%)।"
            })
    else:
        decision["message"] = "ऊपर और नीचे की 3 स्ट्राइक्स का Change in OI लगभग बराबर है — स्पष्ट ट्रेंड नहीं।"

    return decision


# ==========================================
# STREAMLIT UI (ताकि पेज स्क्रीन पर दिखे)
# ==========================================
def render_iv_page():
    st.set_page_config(page_title="OTM IV Indicator", layout="wide")
    st.title("📊 OTM IV + Change in OI Indicator")
    st.caption("10:00 AM Option Selling Confirmation Filter (Fake Breakout Detector)")

    col1, col2 = st.columns([2, 1])
    with col1:
        selected_idx = st.selectbox("Index चुनें", ["Nifty 50", "Bank Nifty", "Sensex"])
    with col2:
        demo_scenario = st.selectbox(
            "Test Mode (बाज़ार बंद होने पर टेस्ट करें)",
            ["Bull Put Spread (Real Uptrend)", "Put Writers Trap (Fake Breakout)", "Bear Call Spread (Real Downtrend)", "Sideways / Wait"]
        )

    # Index के हिसाब से Spot और Step सेट करें
    if selected_idx == "Nifty 50":
        spot, step = 23500, 50
    elif selected_idx == "Bank Nifty":
        spot, step = 50500, 100
    else:
        spot, step = 77500, 100

    # Demo Option Chain डेटा तैयार करें
    strikes = [spot + i * step for i in range(-4, 5)]
    if "Bull Put" in demo_scenario:
        ce_oi_chg = [12000] * len(strikes)
        pe_oi_chg = [45000] * len(strikes)
        ce_ltp_base, pe_ltp_base = 110.0, 95.0
        sim_pe_shift = -0.35
        sim_ce_shift = 0.05
    elif "Put Writers Trap" in demo_scenario:
        ce_oi_chg = [10000] * len(strikes)
        pe_oi_chg = [50000] * len(strikes)
        ce_ltp_base, pe_ltp_base = 105.0, 130.0
        sim_pe_shift = 0.45
        sim_ce_shift = 0.10
    elif "Bear Call" in demo_scenario:
        ce_oi_chg = [48000] * len(strikes)
        pe_oi_chg = [11000] * len(strikes)
        ce_ltp_base, pe_ltp_base = 90.0, 115.0
        sim_pe_shift = 0.05
        sim_ce_shift = -0.30
    else:
        ce_oi_chg = [20000] * len(strikes)
        pe_oi_chg = [21000] * len(strikes)
        ce_ltp_base, pe_ltp_base = 110.0, 110.0
        sim_pe_shift = 0.0
        sim_ce_shift = 0.0

    demo_df = pd.DataFrame({
        "STRIKE": strikes,
        "CALL CHG OI": ce_oi_chg,
        "PUT CHG OI": pe_oi_chg,
        "CALL LTP": [max(10.0, ce_ltp_base - abs(s - spot) * 0.25) for s in strikes],
        "PUT LTP": [max(10.0, pe_ltp_base - abs(s - spot) * 0.25) for s in strikes],
    })

    # Demo के लिए 15 मिनट पहले का IV History सेट करें
    res = evaluate_otm_iv_from_df(selected_idx, spot, demo_df, "DEMO")
    res["ce_iv_change"] = sim_ce_shift
    res["pe_iv_change"] = sim_pe_shift

    # दोबारा सिग्नल अपडेट करें ताकि Demo IV shift दिखे
    if res["bottom3_pe_chg_oi"] > res["top3_ce_chg_oi"] * 1.15:
        if sim_pe_shift <= -0.15:
            res["signal"] = "BUY_SIGNAL"
            res["spread_name"] = "BULL PUT SPREAD (मजबूत अपट्रेंड)"
            res["sell_leg"] = f"{spot} PE"
            res["buy_hedge"] = f"{spot - (1000 if selected_idx=='Sensex' else 300)} PE"
            res["message"] = f"नीचे की 3 स्ट्राइक्स में Put Chg OI ज़्यादा है और OTM Put IV {abs(sim_pe_shift)}% गिरा है (असली अपट्रेंड कन्फर्म)।"
        elif sim_pe_shift > 0.25:
            res["signal"] = "TRAP"
            res["spread_name"] = "ट्रेड न लें (Put Writers Trap)"
            res["message"] = f"Put Chg OI ज़्यादा है, पर OTM Put IV +{sim_pe_shift}% बढ़ रहा है — नीचे पलटने का खतरा!"
    elif res["top3_ce_chg_oi"] > res["bottom3_pe_chg_oi"] * 1.15 and sim_ce_shift <= -0.15:
        res["signal"] = "BUY_SIGNAL"
        res["spread_name"] = "BEAR CALL SPREAD (मजबूत डाउनट्रेंड)"
        res["sell_leg"] = f"{spot} CE"
        res["buy_hedge"] = f"{spot + (1000 if selected_idx=='Sensex' else 300)} CE"
        res["message"] = f"ऊपर की 3 स्ट्राइक्स में Call Chg OI ज़्यादा है और OTM Call IV {abs(sim_ce_shift)}% गिरा है (असली डाउनट्रेंड कन्फर्म)।"

    st.divider()

    # Signal Banner
    if res["signal"] == "BUY_SIGNAL":
        st.success(f"### ✅ {res['spread_name']}\n**Sell:** `{res['sell_leg']}` | **Buy Hedge:** `{res['buy_hedge']}`\n\n{res['message']}")
    elif res["signal"] == "TRAP":
        st.error(f"### ⚠️ {res['spread_name']}\n{res['message']}")
    else:
        st.warning(f"### ⏳ {res['spread_name']}\n{res['message']}")

    # Metrics Row
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Top 3 OTM Call IV", f"{res['avg_otm_ce_iv']}%", f"{res['ce_iv_change']:+}%")
    m2.metric("Bottom 3 OTM Put IV", f"{res['avg_otm_pe_iv']}%", f"{res['pe_iv_change']:+}%")
    m3.metric("Top 3 Call Chg OI", f"{res['top3_ce_chg_oi']:,}")
    m4.metric("Bottom 3 Put Chg OI", f"{res['bottom3_pe_chg_oi']:,}")

    with st.expander("देखें: OTM Option Chain डेटा जिस पर कैलकुलेशन हुई है"):
        st.dataframe(demo_df, use_container_width=True)

if __name__ == "__main__":
    render_iv_page()
