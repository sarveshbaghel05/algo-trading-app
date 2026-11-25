import math
from datetime import datetime
import pytz
import pandas as pd
import streamlit as st

from oi_base import (
    ensure_angel_session,
    get_index_meta,
    get_live_expiry_info,
    get_exact_t_years,
    compute_accurate_chg_oi,
)


def _norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _bs_price(S, K, T, r, sigma, option_type="CE"):
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if option_type == "CE" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "CE":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def calculate_otm_iv(ltp, spot, strike, expiry_str, option_type="CE", r=0.065):
    """असली एक्सपायरी समय से सटीक OTM IV (%) निकालता है"""
    if ltp <= 0.5 or spot <= 0 or strike <= 0:
        return 0.0
    if not expiry_str or expiry_str in ["Unknown", "Simulation", "DEMO", "Backtest"]:
        return 0.0

    T = get_exact_t_years(expiry_str)
    low, high = 0.02, 2.50
    for _ in range(40):
        mid = (low + high) / 2.0
        price = _bs_price(spot, strike, T, r, mid, option_type)
        if price < ltp:
            low = mid
        else:
            high = mid
    return round(((low + high) / 2.0) * 100.0, 2)


def evaluate_otm_iv_from_df(selected_idx, spot_price, full_df, expiry_label, oi_valid=True):
    meta = get_index_meta(selected_idx)
    step = meta["step"]
    hedge_dist = meta["hedge"]
    atm_strike = round(spot_price / step) * step

    if full_df is None or full_df.empty or expiry_label in ["Unknown", "Simulation", "DEMO"]:
        return {
            "signal": "WAIT",
            "spread_name": "असली डेटा की प्रतीक्षा (WAIT)",
            "sell_leg": "-",
            "buy_hedge": "-",
            "avg_otm_ce_iv": 0.0,
            "avg_otm_pe_iv": 0.0,
            "ce_iv_change": 0.0,
            "pe_iv_change": 0.0,
            "top3_ce_chg_oi": 0,
            "bottom3_pe_chg_oi": 0,
            "message": "बिना लाइव Angel One डेटा के IV सिग्नल नहीं बनेगा।",
        }

    otm_ce_strikes = [atm_strike + step, atm_strike + 2 * step, atm_strike + 3 * step]
    otm_pe_strikes = [atm_strike - step, atm_strike - 2 * step, atm_strike - 3 * step]

    up_3_df = full_df[full_df["STRIKE"].isin(otm_ce_strikes)]
    down_3_df = full_df[full_df["STRIKE"].isin(otm_pe_strikes)]

    top3_ce_chg_oi = int(up_3_df["CALL CHG OI"].sum()) if (oi_valid and not up_3_df.empty) else 0
    bottom3_pe_chg_oi = int(down_3_df["PUT CHG OI"].sum()) if (oi_valid and not down_3_df.empty) else 0

    top3_ce_ivs = [
        calculate_otm_iv(float(r["CALL LTP"]), spot_price, int(r["STRIKE"]), expiry_label, "CE")
        for _, r in up_3_df.iterrows() if float(r["CALL LTP"]) > 0.5
    ]
    top3_ce_ivs = [v for v in top3_ce_ivs if v > 0]

    bottom3_pe_ivs = [
        calculate_otm_iv(float(r["PUT LTP"]), spot_price, int(r["STRIKE"]), expiry_label, "PE")
        for _, r in down_3_df.iterrows() if float(r["PUT LTP"]) > 0.5
    ]
    bottom3_pe_ivs = [v for v in bottom3_pe_ivs if v > 0]

    avg_otm_ce_iv = round(sum(top3_ce_ivs) / len(top3_ce_ivs), 2) if top3_ce_ivs else 0.0
    avg_otm_pe_iv = round(sum(bottom3_pe_ivs) / len(bottom3_pe_ivs), 2) if bottom3_pe_ivs else 0.0

    # कॉल बनाम पुट IV अंतर (Skew) + सेशन बदलाव दोनों देखें
    iv_skew = round(avg_otm_pe_iv - avg_otm_ce_iv, 2)

    ist = pytz.timezone("Asia/Kolkata")
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    now_min = datetime.now(ist).strftime("%H:%M")
    state_key = f"otm_iv_hist_{selected_idx}_{today_str}"

    if state_key not in st.session_state:
        st.session_state[state_key] = []

    hist = st.session_state[state_key]
    if avg_otm_ce_iv > 0 and avg_otm_pe_iv > 0:
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
        "spread_name": "स्पष्ट सेटअप की प्रतीक्षा",
        "sell_leg": "-",
        "buy_hedge": "-",
        "avg_otm_ce_iv": avg_otm_ce_iv,
        "avg_otm_pe_iv": avg_otm_pe_iv,
        "ce_iv_change": ce_iv_change,
        "pe_iv_change": pe_iv_change,
        "top3_ce_chg_oi": top3_ce_chg_oi,
        "bottom3_pe_chg_oi": bottom3_pe_chg_oi,
        "message": "",
    }

    if not oi_valid:
        decision["message"] = "कल का क्लोज़िंग OI उपलब्ध नहीं है — गलत Change in OI से बचने के लिए सिग्नल रुका हुआ है।"
        return decision

    # 1. Bull Put कंडीशन: नीचे Put राइटिंग ज़्यादा + या तो Put IV गिर रहा हो या Put IV महँगा हो (Skew > 0.8) और अचानक स्पाइक न हो
    if bottom3_pe_chg_oi > 0 and bottom3_pe_chg_oi >= top3_ce_chg_oi * 1.20:
        if pe_iv_change > 0.45:
            decision.update({
                "signal": "TRAP",
                "spread_name": "ट्रेड न लें (Put Writers Trap)",
                "message": f"Put Chg OI ज़्यादा है, पर OTM Put IV तेज़ी से +{pe_iv_change}% बढ़ रहा है — नीचे गिरने का खतरा!",
            })
        elif pe_iv_change <= -0.10 or iv_skew >= 0.80:
            decision.update({
                "signal": "BUY_SIGNAL",
                "spread_name": "BULL PUT SPREAD (मजबूत अपट्रेंड)",
                "sell_leg": f"{atm_strike} PE",
                "buy_hedge": f"{atm_strike - hedge_dist} PE",
                "message": f"नीचे Put Chg OI मज़बूत है ({bottom3_pe_chg_oi:+,}) और Put IV अनुकूल है (बदलाव: {pe_iv_change:+}%, Skew: {iv_skew:+}%)।",
            })
        else:
            decision.update({
                "signal": "WAIT",
                "spread_name": "Bull Put Spread (IV कन्फर्मेशन बाकी)",
                "message": f"Put Chg OI ज़्यादा है, पर Put IV अभी स्थिर है ({pe_iv_change:+}%) — थोड़ा इंतज़ार करें।",
            })

    # 2. Bear Call कंडीशन
    elif top3_ce_chg_oi > 0 and top3_ce_chg_oi >= bottom3_pe_chg_oi * 1.20:
        if ce_iv_change > 0.45:
            decision.update({
                "signal": "TRAP",
                "spread_name": "ट्रेड न लें (Call Writers Trap)",
                "message": f"Call Chg OI ज़्यादा है, पर OTM Call IV तेज़ी से +{ce_iv_change}% बढ़ रहा है — ऊपर भागने का खतरा!",
            })
        elif ce_iv_change <= -0.10 or iv_skew <= -0.80:
            decision.update({
                "signal": "BUY_SIGNAL",
                "spread_name": "BEAR CALL SPREAD (मजबूत डाउनट्रेंड)",
                "sell_leg": f"{atm_strike} CE",
                "buy_hedge": f"{atm_strike + hedge_dist} CE",
                "message": f"ऊपर Call Chg OI भारी है ({top3_ce_chg_oi:+,}) और Call IV अनुकूल है (बदलाव: {ce_iv_change:+}%, Skew: {iv_skew:+}%)।",
            })
        else:
            decision.update({
                "signal": "WAIT",
                "spread_name": "Bear Call Spread (IV कन्फर्मेशन बाकी)",
                "message": f"Call Chg OI ज़्यादा है, पर Call IV अभी स्थिर है ({ce_iv_change:+}%) — थोड़ा इंतज़ार करें।",
            })
    else:
        decision["message"] = "ऊपर की Call और नीचे की Put राइटिंग लगभग बराबर है — स्पष्ट ट्रेंड नहीं।"

    return decision


def render_standalone_iv_page():
    st.title("📊 OTM 3-Strike IV + Change in OI Indicator (100% Real Data)")
    st.caption("सही Expiry समय, अपडेटेड हेज दूरी (500 / 1000 / 1500) और कल के क्लोज़िंग OI पर आधारित")

    c1, c2 = st.columns([3, 1])
    with c1:
        selected_idx = st.selectbox("इंडेक्स चुनें", ["Nifty 50", "Bank Nifty", "Sensex"], key="iv_page_idx")
    with c2:
        st.write("")
        st.write("")
        if st.button("🔄 रिफ्रेश करें", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    jwt, akey = ensure_angel_session()
    if not jwt or not akey:
        st.error("🚨 Angel One SmartAPI कनेक्ट नहीं है। कृपया मुख्य टर्मिनल पर लॉगिन जाँचें।")
        return

    from pages.greek_regime_indicator import _fetch_real_angel_option_chain_raw
    raw_df, spot, exp_str, trading_dte = _fetch_real_angel_option_chain_raw(jwt, akey, selected_idx)
    if raw_df is None or raw_df.empty or not spot:
        st.warning("⚠️ अभी Angel One से लाइव ऑप्शन चेन डेटा नहीं मिल रहा है।")
        return

    df_oc, oi_valid, oi_source = compute_accurate_chg_oi(selected_idx, raw_df)
    st.success(f"🟢 **Angel One Live** | Spot: `{spot:,.2f}` | Expiry: `{exp_str}` (**`{trading_dte} DTE`**) | OI स्रोत: `{oi_source}`")

    res = evaluate_otm_iv_from_df(selected_idx, spot, df_oc, exp_str, oi_valid=oi_valid)
    st.divider()

    if res["signal"] == "BUY_SIGNAL":
        st.success(f"### ✅ {res['spread_name']}\n**Sell:** `{res['sell_leg']}` | **Buy Hedge:** `{res['buy_hedge']}`\n\n{res['message']}")
    elif res["signal"] == "TRAP":
        st.error(f"### 🚨 {res['spread_name']}\n{res['message']}")
    else:
        st.warning(f"### ⏳ {res['spread_name']}\n{res['message']}")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Top-3 OTM Call IV", f"{res['avg_otm_ce_iv']}%", f"{res['ce_iv_change']:+}%", delta_color="inverse")
    m2.metric("Bottom-3 OTM Put IV", f"{res['avg_otm_pe_iv']}%", f"{res['pe_iv_change']:+}%", delta_color="inverse")
    m3.metric("Top-3 Call Chg OI", f"{res['top3_ce_chg_oi']:,}" if oi_valid else "N/A")
    m4.metric("Bottom-3 Put Chg OI", f"{res['bottom3_pe_chg_oi']:,}" if oi_valid else "N/A")

    with st.expander("देखें: Live Option Chain डेटा"):
        st.dataframe(df_oc[["STRIKE", "CALL CHG OI", "CALL OI", "CALL LTP", "PUT LTP", "PUT OI", "PUT CHG OI"]], use_container_width=True, hide_index=True)


if __name__ == "__main__":
    render_standalone_iv_page()
