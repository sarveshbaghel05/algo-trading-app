import math
from datetime import datetime
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

def calculate_otm_iv(ltp, spot, strike, expiry_dt, option_type="CE", r=0.07):
    if ltp <= 0.5 or spot <= 0:
        return 0.0
    now = datetime.now()
    seconds_left = max((expiry_dt - now).total_seconds(), 3600)
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

def evaluate_otm_iv_indicator(symbol, spot_price, option_chain_data, expiry_dt):
    if symbol == "NIFTY":
        step, hedge_dist = 50, 300
    elif symbol == "BANKNIFTY":
        step, hedge_dist = 100, 300
    else:
        step, hedge_dist = 100, 1000

    atm_strike = round(spot_price / step) * step

    # ऊपर की 3 OTM Call स्ट्राइक्स और नीचे की 3 OTM Put स्ट्राइक्स
    otm_ce_strikes = [atm_strike + step, atm_strike + 2 * step, atm_strike + 3 * step]
    otm_pe_strikes = [atm_strike - step, atm_strike - 2 * step, atm_strike - 3 * step]

    chain_map = {row["strike"]: row for row in option_chain_data}

    top3_ce_chg_oi = 0
    top3_ce_ivs = []
    for s in otm_ce_strikes:
        if s in chain_map:
            top3_ce_chg_oi += chain_map[s].get("ce_chg_oi", 0)
            iv = calculate_otm_iv(chain_map[s]["ce_ltp"], spot_price, s, expiry_dt, "CE")
            if iv > 0:
                top3_ce_ivs.append(iv)

    bottom3_pe_chg_oi = 0
    bottom3_pe_ivs = []
    for s in otm_pe_strikes:
        if s in chain_map:
            bottom3_pe_chg_oi += chain_map[s].get("pe_chg_oi", 0)
            iv = calculate_otm_iv(chain_map[s]["pe_ltp"], spot_price, s, expiry_dt, "PE")
            if iv > 0:
                bottom3_pe_ivs.append(iv)

    avg_otm_ce_iv = round(sum(top3_ce_ivs) / len(top3_ce_ivs), 2) if top3_ce_ivs else 0.0
    avg_otm_pe_iv = round(sum(bottom3_pe_ivs) / len(bottom3_pe_ivs), 2) if bottom3_pe_ivs else 0.0

    # पिछले 15 मिनट का IV रिकॉर्ड रखने के लिए
    state_key = f"otm_iv_hist_{symbol}"
    if state_key not in st.session_state:
        st.session_state[state_key] = []

    now_min = datetime.now().strftime("%H:%M")
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

    # Bull Put Spread लॉजिक (जब नीचे पुट राइटिंग ज़्यादा हो)
    if bottom3_pe_chg_oi > top3_ce_chg_oi * 1.15:
        if pe_iv_change <= -0.15:
            decision.update({
                "signal": "BUY_SIGNAL",
                "spread_name": "BULL PUT SPREAD (तेज़ी का ट्रेंड)",
                "sell_leg": f"{atm_strike} PE",
                "buy_hedge": f"{atm_strike - hedge_dist} PE",
                "message": f"Put Chg OI ज़्यादा है और OTM Put IV {abs(pe_iv_change)}% गिरा है (असली अपट्रेंड कन्फर्म)।"
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
                "message": f"Put Chg OI ज़्यादा है, पर OTM Put IV अभी गिरना शुरू नहीं हुआ ({pe_iv_change}%)।"
            })

    # Bear Call Spread लॉजिक (जब ऊपर कॉल राइटिंग ज़्यादा हो)
    elif top3_ce_chg_oi > bottom3_pe_chg_oi * 1.15:
        if ce_iv_change <= -0.15:
            decision.update({
                "signal": "BUY_SIGNAL",
                "spread_name": "BEAR CALL SPREAD (मंदी का ट्रेंड)",
                "sell_leg": f"{atm_strike} CE",
                "buy_hedge": f"{atm_strike + hedge_dist} CE",
                "message": f"Call Chg OI ज़्यादा है और OTM Call IV {abs(ce_iv_change)}% गिरा है (असली डाउनट्रेंड कन्फर्म)।"
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
                "message": f"Call Chg OI ज़्यादा है, पर OTM Call IV अभी गिरना शुरू नहीं हुआ ({ce_iv_change}%)।"
            })
    else:
        decision["message"] = "ऊपर और नीचे की 3 स्ट्राइक्स का Change in OI लगभग बराबर है।"

    return decision
