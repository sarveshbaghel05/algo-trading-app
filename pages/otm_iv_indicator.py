import math
from datetime import datetime
import pytz
import pandas as pd
import requests
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
# STANDALONE PAGE UI (जब Sidebar से सीधे यह पेज खोला जाए)
# ==========================================
def render_standalone_iv_page():
    st.title("📊 OTM 3-Strike IV + Change in OI Indicator")
    st.caption("Angel One Live Option Chain से जुड़ा हुआ (Fake Breakout & IV Crush Filter)")

    c1, c2 = st.columns([2, 1])
    with c1:
        selected_idx = st.selectbox("इंडेक्स चुनें", ["Nifty 50", "Bank Nifty", "Sensex"], key="iv_page_idx")
    with c2:
        data_source_mode = st.radio("डेटा मोड:", ["🔴 Angel One Live", "🟡 Offline Test Mode"], horizontal=True)

    if selected_idx == "Nifty 50":
        step, default_spot = 50, 23500.0
    elif selected_idx == "Bank Nifty":
        step, default_spot = 100, 50500.0
    else:
        step, default_spot = 100, 77500.0

    spot = default_spot
    expiry_label = "DEMO"
    is_live_loaded = False
    df_oc = pd.DataFrame()

    # अगर Angel One Live चुना है और main app से फंक्शन उपलब्ध है तो लाइव डेटा लें
    if "Live" in data_source_mode:
        try:
            from app import fetch_angel_live_oc
            if "angel_jwt" in st.session_state and "angel_key" in st.session_state:
                live_df, live_spot, exp_str, _, err = fetch_angel_live_oc(
                    st.session_state["angel_jwt"], st.session_state["angel_key"], selected_idx, 5
                )
                if live_df is not None and not live_df.empty:
                    df_oc = live_df
                    spot = live_spot
                    expiry_label = exp_str
                    is_live_loaded = True
        except Exception:
            pass

    if not is_live_loaded:
        if "Live" in data_source_mode:
            st.info("ℹ️ अभी बाज़ार बंद है या मुख्य पेज से Angel One लॉगिन सक्रिय नहीं है — नीचे **Offline Test Mode** दिखाया जा रहा है।")
        demo_type = st.selectbox(
            "Test Scenario चुनें:",
            ["Bull Put Spread (Real Uptrend)", "Put Writers Trap (Fake Breakout)", "Bear Call Spread (Real Downtrend)", "Sideways / Wait"]
        )
        strikes = [int(default_spot + i * step) for i in range(-4, 5)]
        if "Bull Put" in demo_type:
            ce_oi, pe_oi, ce_ltp, pe_ltp, sim_ce, sim_pe = 12000, 46000, 110.0, 95.0, 0.05, -0.35
        elif "Put Writers Trap" in demo_type:
            ce_oi, pe_oi, ce_ltp, pe_ltp, sim_ce, sim_pe = 10000, 52000, 105.0, 130.0, 0.10, 0.45
        elif "Bear Call" in demo_type:
            ce_oi, pe_oi, ce_ltp, pe_ltp, sim_ce, sim_pe = 49000, 11000, 90.0, 115.0, -0.30, 0.05
        else:
            ce_oi, pe_oi, ce_ltp, pe_ltp, sim_ce, sim_pe = 20000, 21000, 110.0, 110.0, 0.0, 0.0

        df_oc = pd.DataFrame({
            "STRIKE": strikes,
            "CALL CHG OI": [ce_oi] * len(strikes),
            "PUT CHG OI": [pe_oi] * len(strikes),
            "CALL LTP": [max(10.0, ce_ltp - abs(s - default_spot) * 0.25) for s in strikes],
            "PUT LTP": [max(10.0, pe_ltp - abs(s - default_spot) * 0.25) for s in strikes],
        })
        res = evaluate_otm_iv_from_df(selected_idx, default_spot, df_oc, "DEMO")
        res["ce_iv_change"] = sim_ce
        res["pe_iv_change"] = sim_pe
        if res["bottom3_pe_chg_oi"] > res["top3_ce_chg_oi"] * 1.15:
            if sim_pe <= -0.15:
                res.update({"signal": "BUY_SIGNAL", "spread_name": "BULL PUT SPREAD (मजबूत अपट्रेंड)", "sell_leg": f"{int(default_spot)} PE", "buy_hedge": f"{int(default_spot - (1000 if selected_idx=='Sensex' else 300))} PE", "message": f"नीचे की 3 स्ट्राइक्स में Put Chg OI ज़्यादा है और OTM Put IV {abs(sim_pe)}% गिरा है (असली अपट्रेंड कन्फर्म)।"})
            elif sim_pe > 0.25:
                res.update({"signal": "TRAP", "spread_name": "ट्रेड न लें (Put Writers Trap)", "message": f"Put Chg OI ज़्यादा है, पर OTM Put IV +{sim_pe}% बढ़ रहा है — नीचे पलटने का खतरा!"})
        elif res["top3_ce_chg_oi"] > res["bottom3_pe_chg_oi"] * 1.15 and sim_ce <= -0.15:
            res.update({"signal": "BUY_SIGNAL", "spread_name": "BEAR CALL SPREAD (मजबूत डाउनट्रेंड)", "sell_leg": f"{int(default_spot)} CE", "buy_hedge": f"{int(default_spot + (1000 if selected_idx=='Sensex' else 300))} CE", "message": f"ऊपर की 3 स्ट्राइक्स में Call Chg OI ज़्यादा है और OTM Call IV {abs(sim_ce)}% गिरा है (असली डाउनट्रेंड कन्फर्म)।"})
    else:
        st.success(f"🟢 **Angel One Live Data Active** | Spot: `{spot:,.2f}` | Expiry: `{expiry_label}`")
        res = evaluate_otm_iv_from_df(selected_idx, spot, df_oc, expiry_label)

    st.divider()
    if res["signal"] == "BUY_SIGNAL":
        st.success(f"### ✅ {res['spread_name']}\n**Sell:** `{res['sell_leg']}` | **Buy Hedge:** `{res['buy_hedge']}`\n\n{res['message']}")
    elif res["signal"] == "TRAP":
        st.error(f"### ⚠️ {res['spread_name']}\n{res['message']}")
    else:
        st.warning(f"### ⏳ {res['spread_name']}\n{res['message']}")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Top-3 OTM Call IV", f"{res['avg_otm_ce_iv']}%", f"{res['ce_iv_change']:+}%", delta_color="inverse")
    m2.metric("Bottom-3 OTM Put IV", f"{res['avg_otm_pe_iv']}%", f"{res['pe_iv_change']:+}%", delta_color="inverse")
    m3.metric("Top-3 Call Chg OI", f"{res['top3_ce_chg_oi']:,}")
    m4.metric("Bottom-3 Put Chg OI", f"{res['bottom3_pe_chg_oi']:,}")

    with st.expander("देखें: Option Chain डेटा (OTM Strikes)"):
        st.dataframe(df_oc, use_container_width=True, hide_index=True)

if __name__ == "__main__":
    render_standalone_iv_page()
