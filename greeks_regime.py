import math
from scipy.stats import norm

def calculate_bs_greeks(spot, strike, t_years, iv_pct, option_type="CE", r=0.07):
    """
    Option ke Greeks (Delta, Gamma, Theta) nikalne ka formula
    """
    if t_years <= 0 or iv_pct <= 0 or spot <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0}

    sigma = iv_pct / 100.0
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t_years) / (sigma * math.sqrt(t_years))
    d2 = d1 - sigma * math.sqrt(t_years)

    pdf_d1 = norm.pdf(d1)
    gamma = pdf_d1 / (spot * sigma * math.sqrt(t_years))

    if option_type.upper() == "CE":
        delta = norm.cdf(d1)
        theta_annual = -(spot * pdf_d1 * sigma) / (2 * math.sqrt(t_years)) - r * strike * math.exp(-r * t_years) * norm.cdf(d2)
    else:
        delta = norm.cdf(d1) - 1.0
        theta_annual = -(spot * pdf_d1 * sigma) / (2 * math.sqrt(t_years)) + r * strike * math.exp(-r * t_years) * norm.cdf(-d2)

    theta_daily = theta_annual / 365.0

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta_daily, 2)
    }


def predict_market_direction(
    spot,
    atm_strike,
    ce_iv,
    pe_iv,
    minutes_to_expiry,
    ce_oi_change_3strikes,
    pe_oi_change_3strikes,
    prev_avg_iv=None
):
    """
    Yeh function screen par saaf likh kar dikhayega ki aaj market:
    1. Upar ja sakta hai (Bullish)
    2. Neeche ja sakta hai (Bearish)
    3. Sideways rahega (Sideways)
    """
    t_years = max(minutes_to_expiry, 15) / 525600.0

    ce_greeks = calculate_bs_greeks(spot, atm_strike, t_years, ce_iv, "CE")
    pe_greeks = calculate_bs_greeks(spot, atm_strike, t_years, pe_iv, "PE")

    # 1. Theta aur Delta ki takat (TDE)
    total_theta = abs(ce_greeks["theta"]) + abs(pe_greeks["theta"])
    net_delta = abs(ce_greeks["delta"] + pe_greeks["delta"])
    tde_score = round(total_theta / (net_delta + 0.05), 2)

    # 2. Gamma aur IV ka khatra (GIP)
    total_gamma = ce_greeks["gamma"] + pe_greeks["gamma"]
    avg_iv = (ce_iv + pe_iv) / 2.0
    gip_score = round(total_gamma * avg_iv * (spot / 100.0), 2)

    # 3. IV mein achanak tezi ki jaanch
    iv_spike = False
    if prev_avg_iv and prev_avg_iv > 0:
        iv_change_pct = ((avg_iv - prev_avg_iv) / prev_avg_iv) * 100
        if iv_change_pct > 4.0:
            iv_spike = True

    # 4. OI ka anupat (Put OI badlav banam Call OI badlav)
    oi_ratio = 1.0
    if ce_oi_change_3strikes > 0 and pe_oi_change_3strikes > 0:
        oi_ratio = pe_oi_change_3strikes / ce_oi_change_3strikes
    elif pe_oi_change_3strikes > 0 and ce_oi_change_3strikes <= 0:
        oi_ratio = 2.0
    elif ce_oi_change_3strikes > 0 and pe_oi_change_3strikes <= 0:
        oi_ratio = 0.4

    # 5. Antim Faisla (App screen par dikhne wala Hindi sandesh)
    if iv_spike or gip_score > 25:
        headline = "⚠️ आज मार्केट में बहुत तेज़ हलचल है — ट्रेड से बचें"
        direction = "HIGH VOLATILITY"
        advice = "अभी ऑप्शन बेचने में स्टॉप-लॉस कटने का खतरा ज़्यादा है। थोड़ा रुकें।"
        box_color = "error"

    elif oi_ratio >= 1.35 and pe_iv >= ce_iv:
        headline = "📈 आज मार्केट ऊपर जा सकता है (Bullish)"
        direction = "UP / BULLISH"
        advice = "नीचे मज़बूत सपोर्ट (Put Writing) बन रहा है। Straddle की जगह सिर्फ़ ATM Put बेचें (+ हेज खरीदें)।"
        box_color = "info"

    elif oi_ratio <= 0.70 and ce_iv >= pe_iv:
        headline = "📉 आज मार्केट नीचे जा सकता है (Bearish)"
        direction = "DOWN / BEARISH"
        advice = "ऊपर भारी रुकावट (Call Writing) बन रही है। Straddle की जगह सिर्फ़ ATM Call बेचें (+ हेज खरीदें)।"
        box_color = "warning"

    elif tde_score >= 100 and (0.70 < oi_ratio < 1.35):
        headline = "↔️ आज मार्केट साइडवेज रहेगा (Sideways)"
        direction = "SIDEWAYS"
        advice = "दोनों तरफ़ बराबर दबाव है और थीटा तेज़ी से गल रहा है। अपनी 10:00 AM वाली दोनों तरफ़ (Call + Put) बेचने की स्ट्रैटेजी चलाएं।"
        box_color = "success"

    else:
        headline = "⚖️ मार्केट अभी दिशा तय कर रहा है (थोड़ा इंतज़ार करें)"
        direction = "UNCLEAR"
        advice = "अभी डेटा पूरी तरह साफ़ नहीं है। 10:15 AM तक रुक कर दोबारा देखें।"
        box_color = "warning"

    return {
        "headline": headline,
        "direction": direction,
        "advice": advice,
        "box_color": box_color,
        "tde_score": tde_score,
        "gip_score": gip_score,
        "oi_ratio": round(oi_ratio, 2)
    }
