import math
from datetime import datetime
import pytz


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
            px = strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)

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
    Greeks (Theta/Delta Efficiency + Gamma-IV) और 3-Strike OI/IV Skew के आधार पर
    साफ़ हिंदी में बताता है:
    1) आज मार्केट ऊपर जा सकता है
    2) आज मार्केट नीचे जा सकता है
    3) आज मार्केट साइडवेज रहेगा
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
            "trade_plan": "अभी डेटा उपलब्ध नहीं है।"
        }

    ce_ltp = float(atm_row["CALL LTP"].iloc[0])
    pe_ltp = float(atm_row["PUT LTP"].iloc[0])
    t_years = _get_time_to_expiry_years(expiry_str)

    ce_iv = _implied_volatility(ce_ltp, spot_price, atm_strike, t_years, "CE")
    pe_iv = _implied_volatility(pe_ltp, spot_price, atm_strike, t_years, "PE")

    ce_g = _calc_bs_greeks(spot_price, atm_strike, t_years, ce_iv, "CE")
    pe_g = _calc_bs_greeks(spot_price, atm_strike, t_years, pe_iv, "PE")

    # 1. Theta और Delta हिसाब
    total_daily_theta = abs(ce_g["theta"]) + abs(pe_g["theta"])
    net_delta = round(ce_g["delta"] + pe_g["delta"], 3)
    abs_net_delta = abs(net_delta)

    one_pct_move = spot_price * 0.01
    delta_point_risk = max(1.0, abs_net_delta * one_pct_move)
    theta_delta_eff = round((total_daily_theta / delta_point_risk) * 10.0, 2)

    # 2. Gamma-IV दबाव (0 से 100)
    avg_gamma = (ce_g["gamma"] + pe_g["gamma"]) / 2.0
    avg_iv_pct = round((ce_g["iv_pct"] + pe_g["iv_pct"]) / 2.0, 2)
    gamma_iv_risk = round(min(100.0, (avg_gamma * spot_price * avg_iv_pct) * 2.2), 1)

    # 3. ऊपर 3 Call बनाम नीचे 3 Put Change in OI दिशा
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

    if pe_g["iv_pct"] > ce_g["iv_pct"] + 1.2:
        bullish_points += 1
    elif ce_g["iv_pct"] > pe_g["iv_pct"] + 1.2:
        bearish_points += 1

    # 4. साफ़-साफ़ अंतिम फैसला (ऊपर / नीचे / साइडवेज)
    is_strong_trend = (gamma_iv_risk >= 52.0) or (abs_net_delta >= 0.14) or (abs(bullish_points - bearish_points) >= 2)

    if is_strong_trend and bullish_points > bearish_points:
        direction = "UP"
        headline = "🟢 आज मार्केट ऊपर जा सकता है (BULLISH)"
        sub_headline = f"नीचे की Put राइटिंग मज़बूत है और Delta ऊपर की तरफ़ इशारा कर रहा है (Gamma-IV स्कोर: {gamma_iv_risk}/100)"
        bg_color = "linear-gradient(90deg, #064e3b, #022c22)"
        border_color = "#10b981"
        trade_plan = f"क्या करें: {atm_strike} PE बेचें और {atm_strike - hedge_pts} PE हेज खरीदें (Bull Put Spread)।"
    elif is_strong_trend and bearish_points > bullish_points:
        direction = "DOWN"
        headline = "🔴 आज मार्केट नीचे जा सकता है (BEARISH)"
        sub_headline = f"ऊपर की Call राइटिंग भारी है और गिरावट का दबाव दिख रहा है (Gamma-IV स्कोर: {gamma_iv_risk}/100)"
        bg_color = "linear-gradient(90deg, #7f1d1d, #450a0a)"
        border_color = "#ef4444"
        trade_plan = f"क्या करें: {atm_strike} CE बेचें और {atm_strike + hedge_pts} CE हेज खरीदें (Bear Call Spread)।"
    else:
        direction = "SIDEWAYS"
        headline = "🟡 आज मार्केट साइडवेज रहेगा (RANGE-BOUND)"
        sub_headline = f"Theta गलन तेज़ है (₹{round(total_daily_theta, 1)}/दिन) और Gamma-IV शांत है ({gamma_iv_risk}/100) — बाज़ार एक दायरे में रहेगा"
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
        "atm_theta": round(total_daily_theta, 2),
        "net_delta": net_delta,
        "atm_iv": avg_iv_pct,
        "trade_plan": trade_plan
    }
