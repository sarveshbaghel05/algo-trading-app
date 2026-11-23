import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from datetime import datetime, timedelta

st.set_page_config(page_title="Options Simulator", layout="wide")

INDEX_CONFIG = {
    "NIFTY": {"lot": 65, "step": 50, "base_spot": 24250},
    "BANKNIFTY": {"lot": 30, "step": 100, "base_spot": 51800},
    "SENSEX": {"lot": 20, "step": 100, "base_spot": 79800},
}

EXPIRIES = {
    "20 Nov 2026 (Weekly)": ["16 Nov 2026", "17 Nov 2026", "18 Nov 2026", "19 Nov 2026", "20 Nov 2026 (Expiry)"],
    "27 Nov 2026 (Monthly)": ["23 Nov 2026", "24 Nov 2026", "25 Nov 2026", "26 Nov 2026", "27 Nov 2026 (Expiry)"],
}

@st.cache_data
def get_day_data(index_name, expiry_name, selected_date):
    seed_val = abs(hash(f"{index_name}_{expiry_name}_{selected_date}")) % (2**31)
    np.random.seed(seed_val)

    cfg = INDEX_CONFIG[index_name]
    step = cfg["step"]
    start_spot = cfg["base_spot"] + np.random.randint(-250, 250)

    start_time = datetime(2026, 11, 19, 9, 15)
    minutes = 376
    timestamps = [start_time + timedelta(minutes=i) for i in range(minutes)]

    spot_moves = np.random.normal(0, step * 0.13, size=minutes)
    spots = np.round(start_spot + np.cumsum(spot_moves), 2)

    center_strike = int(round(start_spot / step) * step)
    # ATM के ऊपर-नीचे 10+ स्ट्राइक्स कवर करने के लिए 15 स्ट्राइक्स की रेंज जनरेट करें
    strikes = list(range(center_strike - (15 * step), center_strike + (16 * step), step))

    is_expiry = "Expiry" in selected_date
    base_iv_factor = 0.45 if is_expiry else 1.0

    base_ce_oi = {k: np.random.randint(15, 55) * 100000 for k in strikes}
    base_pe_oi = {k: np.random.randint(15, 55) * 100000 for k in strikes}
    curr_ce_oi = base_ce_oi.copy()
    curr_pe_oi = base_pe_oi.copy()

    records = []
    for idx, ts in enumerate(timestamps):
        spot = spots[idx]
        decay = (minutes - idx) / minutes

        for strike in strikes:
            ce_intr = max(0.0, spot - strike)
            pe_intr = max(0.0, strike - spot)
            dist = abs(spot - strike)

            time_val = max(1.5, (step * 2.3 * base_iv_factor * np.exp(-dist / (step * 4.5))) * (0.35 + 0.65 * decay))
            ce_ltp = round(max(0.5, ce_intr + time_val + np.random.uniform(-0.8, 0.8)), 2)
            pe_ltp = round(max(0.5, pe_intr + time_val + np.random.uniform(-0.8, 0.8)), 2)

            if idx > 0:
                curr_ce_oi[strike] = max(50000, curr_ce_oi[strike] + np.random.randint(-25000, 45000))
                curr_pe_oi[strike] = max(50000, curr_pe_oi[strike] + np.random.randint(-25000, 45000))

            records.append({
                "time_str": ts.strftime("%H:%M"),
                "spot": spot,
                "strike": strike,
                "ce_ltp": ce_ltp,
                "ce_oi": curr_ce_oi[strike],
                "ce_coi": curr_ce_oi[strike] - base_ce_oi[strike],
                "pe_ltp": pe_ltp,
                "pe_oi": curr_pe_oi[strike],
                "pe_coi": curr_pe_oi[strike] - base_pe_oi[strike],
            })

    return pd.DataFrame(records), [ts.strftime("%H:%M") for ts in timestamps]

# Session State
if "sim_idx" not in st.session_state:
    st.session_state.sim_idx = 45
if "sim_positions" not in st.session_state:
    st.session_state.sim_positions = []
if "sim_booked_pnl" not in st.session_state:
    st.session_state.sim_booked_pnl = 0.0
if "last_context" not in st.session_state:
    st.session_state.last_context = ""

# --- 1. टॉप फिल्टर बार (Index, Expiry, Date, Lots) ---
c1, c2, c3, c4 = st.columns(4)
sel_index = c1.selectbox("Index", list(INDEX_CONFIG.keys()))
sel_expiry = c2.selectbox("Expiry", list(EXPIRIES.keys()))
sel_date = c3.selectbox("Date", EXPIRIES[sel_expiry])
default_lots = c4.number_input("Lots", min_value=1, max_value=50, value=1)

current_context = f"{sel_index}_{sel_expiry}_{sel_date}"
if st.session_state.last_context != current_context:
    st.session_state.last_context = current_context
    st.session_state.sim_idx = 45
    st.session_state.sim_positions = []
    st.session_state.sim_booked_pnl = 0.0

df_day, all_times = get_day_data(sel_index, sel_expiry, sel_date)
lot_size = INDEX_CONFIG[sel_index]["lot"]

def move_time(mins):
    st.session_state.sim_idx = max(0, min(len(all_times) - 1, st.session_state.sim_idx + mins))

curr_time = all_times[st.session_state.sim_idx]
curr_df = df_day[df_day["time_str"] == curr_time].reset_index(drop=True)
spot_now = curr_df["spot"].iloc[0]
atm_strike = min(curr_df["strike"], key=lambda x: abs(x - spot_now))

# ATM के ऊपर 10 और नीचे 10 स्ट्राइक्स (कुल 21 स्ट्राइक्स) निकालें
atm_idx = curr_df[curr_df["strike"] == atm_strike].index[0]
view_df = curr_df.iloc[max(0, atm_idx - 10): min(len(curr_df), atm_idx + 11)].copy().reset_index(drop=True)

# --- 2. Spot & Time Machine Controller ---
st.info(f"⏰ **Time:** {curr_time}  |  📈 **{sel_index} Spot:** {spot_now:,.2f}  |  🎯 **ATM:** {atm_strike}")

jump_t = st.select_slider("समय आगे-पीछे खिसकाएं (Time Slider):", options=all_times, value=curr_time)
if jump_t != curr_time:
    st.session_state.sim_idx = all_times.index(jump_t)
    st.rerun()

t_cols = st.columns(7)
if t_cols[0].button("-15m"): move_time(-15); st.rerun()
if t_cols[1].button("-5m"): move_time(-5); st.rerun()
if t_cols[2].button("-1m"): move_time(-1); st.rerun()
if t_cols[3].button("+1m", type="primary"): move_time(1); st.rerun()
if t_cols[4].button("+5m", type="primary"): move_time(5); st.rerun()
if t_cols[5].button("+15m", type="primary"): move_time(15); st.rerun()
if t_cols[6].button("Reset"):
    st.session_state.sim_idx = 45
    st.session_state.sim_positions = []
    st.session_state.sim_booked_pnl = 0.0
    st.rerun()

# --- 3. ऊपर का सेक्शन: MTM, Payoff Graph और Active Positions ---
st.divider()
open_pos = [p for p in st.session_state.sim_positions if p["status"] == "OPEN"]
running_pnl = 0.0

if open_pos:
    st.subheader("💼 Active Positions & Live MTM")
    for p in open_pos:
        s_row = curr_df[curr_df["strike"] == p["strike"]].iloc[0]
        ltp = s_row["ce_ltp"] if p["type"] == "CE" else s_row["pe_ltp"]
        pnl = (p["entry"] - ltp) * p["qty"] if p["side"] == "SELL" else (ltp - p["entry"]) * p["qty"]
        running_pnl += pnl

        pc1, pc2, pc3, pc4 = st.columns([2.2, 1.4, 1.4, 1])
        badge = "🔴 SELL" if p["side"] == "SELL" else "🟢 BUY"
        pc1.write(f"{badge} **{p['strike']} {p['type']}** ({p['lots']}L)")
        pc2.write(f"In: ₹{p['entry']} | LTP: ₹{ltp}")
        color = "green" if pnl >= 0 else "red"
        pc3.markdown(f":{color}[**₹{pnl:,.0f}**]")
        if pc4.button("Exit", key=f"ex_{p['id']}"):
            p["status"] = "CLOSED"
            st.session_state.sim_booked_pnl += pnl
            st.rerun()

    net_mtm = st.session_state.sim_booked_pnl + running_pnl
    km1, km2, km3 = st.columns(3)
    km1.metric("Live MTM", f"₹{running_pnl:,.0f}")
    km2.metric("Booked P&L", f"₹{st.session_state.sim_booked_pnl:,.0f}")
    km3.metric("Net Total P&L", f"₹{net_mtm:,.0f}", delta=f"{net_mtm:,.0f}")

    # Payoff Graph
    spot_range = np.linspace(spot_now * 0.96, spot_now * 1.04, 150)
    payoff = np.zeros_like(spot_range) + st.session_state.sim_booked_pnl
    for p in open_pos:
        intr = np.maximum(0, spot_range - p["strike"]) if p["type"] == "CE" else np.maximum(0, p["strike"] - spot_range)
        leg_pnl = (p["entry"] - intr) * p["qty"] if p["side"] == "SELL" else (intr - p["entry"]) * p["qty"]
        payoff += leg_pnl

    mp1, mp2 = st.columns(2)
    mp1.success(f"Max Profit (Expiry): **₹{np.max(payoff):,.0f}**")
    mp2.error(f"Max Loss (Expiry): **₹{np.min(payoff):,.0f}**")

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=spot_range, y=payoff, mode="lines", fill="tozeroy", name="Payoff", line=dict(color="#26a69a", width=2.5)))
    fig.add_hline(y=0, line_dash="dash", line_color="gray")
    fig.add_vline(x=spot_now, line_dash="dot", line_color="#f59e0b", annotation_text="Spot")
    fig.update_layout(title="📈 Expiry Payoff Graph", height=260, margin=dict(l=10, r=10, t=35, b=10), xaxis_title=sel_index, yaxis_title="P&L (₹)")
    st.plotly_chart(fig, use_container_width=True)
else:
    st.write(f"💰 **Booked P&L:** ₹{st.session_state.sim_booked_pnl:,.0f} | नीचे Quick Trade Bar से स्ट्राइक चुनकर **Buy** या **Sell** करें।")

# --- 4. नीचे का सेक्शन: Live-Style Option Chain (ATM ± 10 Strikes) ---
st.divider()
st.subheader(f"📋 Option Chain @ {curr_time} (ATM: {atm_strike} | ±10 Strikes)")

top_ce = view_df.nlargest(3, "ce_oi")
top_pe = view_df.nlargest(3, "pe_oi")
s1, s2 = st.columns(2)
s1.error(f"🔴 **Top 3 Call OI:** {', '.join(top_ce['strike'].astype(str))} | Total Chg OI: **{top_ce['ce_coi'].sum():+,}**")
s2.success(f"🟢 **Top 3 Put OI:** {', '.join(top_pe['strike'].astype(str))} | Total Chg OI: **{top_pe['pe_coi'].sum():+,}**")

def add_trade(strike, opt_type, side, price):
    st.session_state.sim_positions.append({
        "id": len(st.session_state.sim_positions) + 1,
        "time": curr_time,
        "strike": strike,
        "type": opt_type,
        "side": side,
        "lots": default_lots,
        "qty": default_lots * lot_size,
        "entry": price,
        "status": "OPEN"
    })

strikes_list = view_df["strike"].tolist()
qt1, qt2, qt3, qt4, qt5 = st.columns([1.4, 1, 1, 1, 1])
sel_strike = qt1.selectbox("Strike चुनें (Trade के लिए):", strikes_list, index=strikes_list.index(atm_strike))
sel_row = view_df[view_df["strike"] == sel_strike].iloc[0]

if qt2.button(f"Sell CE ₹{sel_row['ce_ltp']}", type="primary", use_container_width=True):
    add_trade(sel_strike, "CE", "SELL", sel_row["ce_ltp"]); st.rerun()
if qt3.button(f"Buy CE ₹{sel_row['ce_ltp']}", use_container_width=True):
    add_trade(sel_strike, "CE", "BUY", sel_row["ce_ltp"]); st.rerun()
if qt4.button(f"Sell PE ₹{sel_row['pe_ltp']}", type="primary", use_container_width=True):
    add_trade(sel_strike, "PE", "SELL", sel_row["pe_ltp"]); st.rerun()
if qt5.button(f"Buy PE ₹{sel_row['pe_ltp']}", use_container_width=True):
    add_trade(sel_strike, "PE", "BUY", sel_row["pe_ltp"]); st.rerun()

oc_table = pd.DataFrame({
    "CE OI": view_df["ce_oi"].apply(lambda x: f"{x:,}"),
    "CE Chg OI": view_df["ce_coi"].apply(lambda x: f"{x:+,}"),
    "CE LTP": view_df["ce_ltp"].apply(lambda x: f"₹{x:.2f}"),
    "STRIKE": view_df["strike"].apply(lambda x: f"⭐ {x} (ATM)" if x == atm_strike else f"{x}"),
    "PE LTP": view_df["pe_ltp"].apply(lambda x: f"₹{x:.2f}"),
    "PE Chg OI": view_df["pe_coi"].apply(lambda x: f"{x:+,}"),
    "PE OI": view_df["pe_oi"].apply(lambda x: f"{x:,}"),
})

st.dataframe(oc_table, use_container_width=True, hide_index=True, height=770)
