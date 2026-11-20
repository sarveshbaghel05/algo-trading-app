import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from datetime import datetime, timedelta

st.set_page_config(page_title="StockMock Pro Simulator", layout="wide")

# कस्टम CSS: असली StockMock / Sensibull Option Chain लुक के लिए
st.markdown("""
<style>
    .oc-header {
        background-color: #1e222d;
        color: #ffffff;
        padding: 8px 4px;
        font-weight: 700;
        font-size: 13px;
        text-align: center;
        border-bottom: 2px solid #363a45;
        border-radius: 4px 4px 0 0;
    }
    .oc-row {
        border-bottom: 1px solid #2a2e39;
        padding: 4px 0px;
        align-items: center;
    }
    .itm-ce { background-color: rgba(255, 193, 7, 0.08); }
    .itm-pe { background-color: rgba(255, 193, 7, 0.08); }
    .atm-badge {
        background-color: #f59e0b;
        color: #000000;
        font-weight: 800;
        padding: 4px 10px;
        border-radius: 6px;
        text-align: center;
        display: inline-block;
        width: 100%;
        font-size: 14px;
    }
    .strike-normal {
        background-color: #2a2e39;
        color: #e0e3eb;
        font-weight: 700;
        padding: 4px 10px;
        border-radius: 6px;
        text-align: center;
        display: inline-block;
        width: 100%;
        font-size: 13px;
    }
    .oi-bar-bg {
        background-color: #2a2e39;
        border-radius: 3px;
        height: 5px;
        width: 100%;
        margin-top: 3px;
    }
    .oi-bar-ce { background-color: #ef5350; height: 5px; border-radius: 3px; }
    .oi-bar-pe { background-color: #26a69a; height: 5px; border-radius: 3px; }
    /* छोटे और क्रिस्प Buy/Sell बटन */
    div.stButton > button {
        padding: 2px 6px !important;
        font-size: 12px !important;
        font-weight: 700 !important;
        min-height: 28px !important;
        border-radius: 4px !important;
    }
</style>
""", unsafe_allow_html=True)

INDEX_CONFIG = {
    "NIFTY": {"lot": 75, "step": 50, "base_spot": 24250},
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
    strikes = list(range(center_strike - (8 * step), center_strike + (9 * step), step))

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

# --- 1. StockMock Top Control Strip ---
st.markdown("### 📊 StockMock Options Simulator")

c1, c2, c3, c4 = st.columns([1.1, 1.4, 1.4, 0.8])
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

# --- 2. Spot & Time Machine Bar ---
st.markdown("---")
m1, m2, m3, m4 = st.columns([1.2, 1.2, 1.2, 1.2])
m1.metric("⏰ Time", curr_time)
m2.metric(f"📈 {sel_index} Spot", f"{spot_now:,.2f}")
m3.metric("🎯 ATM", f"{atm_strike}")
jump_t = m4.selectbox("Jump Time", all_times, index=st.session_state.sim_idx)
if jump_t != curr_time:
    st.session_state.sim_idx = all_times.index(jump_t)
    st.rerun()

t_cols = st.columns([1, 1, 1, 1, 1, 1, 1, 1.1])
if t_cols[0].button("◀ 30m", use_container_width=True): move_time(-30); st.rerun()
if t_cols[1].button("◀ 15m", use_container_width=True): move_time(-15); st.rerun()
if t_cols[2].button("◀ 5m", use_container_width=True): move_time(-5); st.rerun()
if t_cols[3].button("◀ 1m", use_container_width=True): move_time(-1); st.rerun()
if t_cols[4].button("▶ 1m", type="primary", use_container_width=True): move_time(1); st.rerun()
if t_cols[5].button("▶ 5m", type="primary", use_container_width=True): move_time(5); st.rerun()
if t_cols[6].button("▶ 15m", type="primary", use_container_width=True): move_time(15); st.rerun()
if t_cols[7].button("Reset", use_container_width=True):
    st.session_state.sim_idx = 45
    st.session_state.sim_positions = []
    st.session_state.sim_booked_pnl = 0.0
    st.rerun()

# --- 3. लाइव मार्केट जैसा Option Chain (OI Bars + B/S Buttons) ---
st.markdown("---")

# Top 3 OI Summary Strip
top_ce = curr_df.nlargest(3, "ce_oi")
top_pe = curr_df.nlargest(3, "pe_oi")
s1, s2 = st.columns(2)
s1.error(f"🔴 **Top 3 Call OI:** {', '.join(top_ce['strike'].astype(str))} | Chg OI: **{top_ce['ce_coi'].sum():+,}**")
s2.success(f"🟢 **Top 3 Put OI:** {', '.join(top_pe['strike'].astype(str))} | Chg OI: **{top_pe['pe_coi'].sum():+,}**")

# Option Chain Header
st.markdown("""
<div class="oc-header">
    <table style="width:100%; border-collapse:collapse; text-align:center;">
        <tr>
            <th style="width:16%; color:#ef5350;">CE OI (Chg)</th>
            <th style="width:12%; color:#ef5350;">CE LTP</th>
            <th style="width:14%;">CALL Action</th>
            <th style="width:16%; background:#2a2e39; color:#f59e0b;">STRIKE</th>
            <th style="width:14%;">PUT Action</th>
            <th style="width:12%; color:#26a69a;">PE LTP</th>
            <th style="width:16%; color:#26a69a;">PE OI (Chg)</th>
        </tr>
    </table>
</div>
""", unsafe_allow_html=True)

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

atm_idx = curr_df[curr_df["strike"] == atm_strike].index[0]
view_df = curr_df.iloc[max(0, atm_idx - 6): min(len(curr_df), atm_idx + 7)]

max_ce_oi = view_df["ce_oi"].max() or 1
max_pe_oi = view_df["pe_oi"].max() or 1

for _, r in view_df.iterrows():
    strike = int(r["strike"])
    is_atm = (strike == atm_strike)
    ce_itm = strike < spot_now
    pe_itm = strike > spot_now

    ce_pct = min(100, int((r["ce_oi"] / max_ce_oi) * 100))
    pe_pct = min(100, int((r["pe_oi"] / max_pe_oi) * 100))

    ce_coi_color = "#26a69a" if r["ce_coi"] >= 0 else "#ef5350"
    pe_coi_color = "#26a69a" if r["pe_coi"] >= 0 else "#ef5350"

    c_oi, c_ltp, c_act, c_str, p_act, p_ltp, p_oi = st.columns([1.4, 1.0, 1.2, 1.3, 1.2, 1.0, 1.4])

    # CE OI + Visual Bar
    c_oi.markdown(f"""
    <div style="text-align:right; font-size:12px; line-height:1.2;">
        <b>{r['ce_oi']/100000:.1f}L</b> <span style="color:{ce_coi_color}; font-size:11px;">({r['ce_coi']/1000:+.0f}K)</span>
        <div class="oi-bar-bg"><div class="oi-bar-ce" style="width:{ce_pct}%; margin-left:auto;"></div></div>
    </div>
    """, unsafe_allow_html=True)

    # CE LTP
    ltp_bg_ce = "background:rgba(245,158,11,0.12); border-radius:4px; padding:3px;" if ce_itm else "padding:3px;"
    c_ltp.markdown(f"<div style='text-align:center; font-weight:700; font-size:13px; {ltp_bg_ce}'>₹{r['ce_ltp']}</div>", unsafe_allow_html=True)

    # CE B / S Buttons
    cb1, cb2 = c_act.columns(2)
    if cb1.button("B", key=f"bce_{strike}", help="Buy Call"):
        add_trade(strike, "CE", "BUY", r["ce_ltp"]); st.rerun()
    if cb2.button("S", key=f"sce_{strike}", type="primary", help="Sell Call"):
        add_trade(strike, "CE", "SELL", r["ce_ltp"]); st.rerun()

    # Center Strike
    if is_atm:
        c_str.markdown(f"<div class='atm-badge'>{strike} (ATM)</div>", unsafe_allow_html=True)
    else:
        c_str.markdown(f"<div class='strike-normal'>{strike}</div>", unsafe_allow_html=True)

    # PE B / S Buttons
    pb1, pb2 = p_act.columns(2)
    if pb1.button("B", key=f"bpe_{strike}", help="Buy Put"):
        add_trade(strike, "PE", "BUY", r["pe_ltp"]); st.rerun()
    if pb2.button("S", key=f"spe_{strike}", type="primary", help="Sell Put"):
        add_trade(strike, "PE", "SELL", r["pe_ltp"]); st.rerun()

    # PE LTP
    ltp_bg_pe = "background:rgba(245,158,11,0.12); border-radius:4px; padding:3px;" if pe_itm else "padding:3px;"
    p_ltp.markdown(f"<div style='text-align:center; font-weight:700; font-size:13px; {ltp_bg_pe}'>₹{r['pe_ltp']}</div>", unsafe_allow_html=True)

    # PE OI + Visual Bar
    p_oi.markdown(f"""
    <div style="text-align:left; font-size:12px; line-height:1.2;">
        <b>{r['pe_oi']/100000:.1f}L</b> <span style="color:{pe_coi_color}; font-size:11px;">({r['pe_coi']/1000:+.0f}K)</span>
        <div class="oi-bar-bg"><div class="oi-bar-pe" style="width:{pe_pct}%;"></div></div>
    </div>
    """, unsafe_allow_html=True)

# --- 4. Positions & Payoff Chart ---
st.markdown("---")
st.subheader("💼 Active Positions & Payoff")

open_pos = [p for p in st.session_state.sim_positions if p["status"] == "OPEN"]
running_pnl = 0.0

if open_pos:
    for p in open_pos:
        s_row = curr_df[curr_df["strike"] == p["strike"]].iloc[0]
        ltp = s_row["ce_ltp"] if p["type"] == "CE" else s_row["pe_ltp"]
        pnl = (p["entry"] - ltp) * p["qty"] if p["side"] == "SELL" else (ltp - p["entry"]) * p["qty"]
        running_pnl += pnl

        pc1, pc2, pc3, pc4, pc5 = st.columns([2.3, 1.1, 1.1, 1.4, 0.9])
        badge = "🔴 SELL" if p["side"] == "SELL" else "🟢 BUY"
        pc1.markdown(f"{badge} **{p['strike']} {p['type']}** ({p['lots']}L @ {p['time']})")
        pc2.write(f"Entry: ₹{p['entry']}")
        pc3.write(f"LTP: ₹{ltp}")
        color = "green" if pnl >= 0 else "red"
        pc4.markdown(f"MTM: :{color}[**₹{pnl:,.2f}**]")
        if pc5.button("Exit", key=f"ex_{p['id']}"):
            p["status"] = "CLOSED"
            st.session_state.sim_booked_pnl += pnl
            st.rerun()

    # Payoff Calculation
    spot_range = np.linspace(spot_now * 0.96, spot_now * 1.04, 150)
    payoff = np.zeros_like(spot_range) + st.session_state.sim_booked_pnl
    for p in open_pos:
        intr = np.maximum(0, spot_range - p["strike"]) if p["type"] == "CE" else np.maximum(0, p["strike"] - spot_range)
        leg_pnl = (p["entry"] - intr) * p["qty"] if p["side"] == "SELL" else (intr - p["entry"]) * p["qty"]
        payoff += leg_pnl

    max_profit = np.max(payoff)
    max_loss = np.min(payoff)

    k1, k2, k3 = st.columns(3)
    k1.metric("Max Profit (Expiry)", f"₹{max_profit:,.0f}")
    k2.metric("Max Loss (Expiry)", f"₹{max_loss:,.0f}")
    k3.metric("Net Live MTM", f"₹{st.session_state.sim_booked_pnl + running_pnl:,.2f}")

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=spot_range, y=payoff, mode="lines", fill="tozeroy", name="Payoff", line=dict(color="#26a69a", width=2.5)))
    fig.add_hline(y=0, line_dash="dash", line_color="gray")
    fig.add_vline(x=spot_now, line_dash="dot", line_color="#f59e0b", annotation_text="Spot")
    fig.update_layout(height=280, margin=dict(l=10, r=10, t=20, b=10), xaxis_title=sel_index, yaxis_title="P&L (₹)")
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Option Chain में किसी भी स्ट्राइक के सामने **B** (Buy) या **S** (Sell) दबाएं।")
