import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from datetime import datetime, timedelta

st.set_page_config(page_title="StockMock Style Options Simulator", layout="wide")

# इंडेक्स के अनुसार लॉट साइज और स्ट्राइक गैप
INDEX_CONFIG = {
    "NIFTY": {"lot": 75, "step": 50, "base_spot": 24250},
    "BANKNIFTY": {"lot": 30, "step": 100, "base_spot": 51800},
    "SENSEX": {"lot": 20, "step": 100, "base_spot": 79800},
}

EXPIRIES = {
    "20 Nov 2026 (Weekly)": ["16 Nov 2026", "17 Nov 2026", "18 Nov 2026", "19 Nov 2026", "20 Nov 2026 (Expiry)"],
    "27 Nov 2026 (Monthly)": ["23 Nov 2026", "24 Nov 2026", "25 Nov 2026", "26 Nov 2026", "27 Nov 2026 (Expiry)"],
}

# --- 1. मल्टी-इंडेक्स और मल्टी-डेट सैंपल डेटा जनरेटर ---
@st.cache_data
def get_day_data(index_name, expiry_name, selected_date):
    seed_val = abs(hash(f"{index_name}_{expiry_name}_{selected_date}")) % (2**31)
    np.random.seed(seed_val)

    cfg = INDEX_CONFIG[index_name]
    step = cfg["step"]
    start_spot = cfg["base_spot"] + np.random.randint(-300, 300)

    start_time = datetime(2026, 11, 19, 9, 15)
    minutes = 376  # 09:15 से 15:30 तक
    timestamps = [start_time + timedelta(minutes=i) for i in range(minutes)]

    # दिन भर का स्पॉट मूवमेंट
    volatility = step * 0.14
    spot_moves = np.random.normal(0, volatility, size=minutes)
    spots = np.round(start_spot + np.cumsum(spot_moves), 2)

    center_strike = int(round(start_spot / step) * step)
    strikes = list(range(center_strike - (8 * step), center_strike + (9 * step), step))

    # एक्सपायरी के दिन थीटा कम होगा
    is_expiry = "Expiry" in selected_date
    base_iv_factor = 0.45 if is_expiry else 1.0

    base_ce_oi = {k: np.random.randint(12, 45) * 100000 for k in strikes}
    base_pe_oi = {k: np.random.randint(12, 45) * 100000 for k in strikes}
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

            time_val = max(1.5, (step * 2.4 * base_iv_factor * np.exp(-dist / (step * 4.5))) * (0.35 + 0.65 * decay))
            ce_ltp = round(max(0.5, ce_intr + time_val + np.random.uniform(-1.0, 1.0)), 2)
            pe_ltp = round(max(0.5, pe_intr + time_val + np.random.uniform(-1.0, 1.0)), 2)

            if idx > 0:
                curr_ce_oi[strike] = max(50000, curr_ce_oi[strike] + np.random.randint(-30000, 50000))
                curr_pe_oi[strike] = max(50000, curr_pe_oi[strike] + np.random.randint(-30000, 50000))

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

# --- 2. Session State ---
if "sim_idx" not in st.session_state:
    st.session_state.sim_idx = 45  # 10:00 AM
if "sim_positions" not in st.session_state:
    st.session_state.sim_positions = []
if "sim_booked_pnl" not in st.session_state:
    st.session_state.sim_booked_pnl = 0.0
if "last_context" not in st.session_state:
    st.session_state.last_context = ""

# --- 3. StockMock जैसा टॉप फिल्टर बार (Index, Expiry, Date, Lots) ---
st.markdown("### 📊 StockMock Style Options Simulator (Price + Historical OI)")

top1, top2, top3, top4 = st.columns([1.1, 1.4, 1.4, 0.9])
sel_index = top1.selectbox("1. Index चुनें", list(INDEX_CONFIG.keys()))
sel_expiry = top2.selectbox("2. Expiry चुनें", list(EXPIRIES.keys()))
sel_date = top3.selectbox("3. Backtest Date चुनें", EXPIRIES[sel_expiry])
default_lots = top4.number_input("Lots", min_value=1, max_value=50, value=1)

# अगर यूज़र ने इंडेक्स, एक्सपायरी या डेट बदली तो पोजीशन रीसेट करें
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

# --- 4. StockMock Time Controller पट्टी ---
st.markdown("---")
tc1, tc2, tc3, tc4 = st.columns([1.2, 1.2, 1.2, 1.4])
tc1.metric("📅 Date & Time", f"{sel_date.split(' ')[0]} | {curr_time}")
tc2.metric(f"📈 {sel_index} Spot", f"{spot_now:,.2f}")
tc3.metric("🎯 ATM Strike", f"{atm_strike}")
jump_t = tc4.selectbox("Jump to Time", all_times, index=st.session_state.sim_idx, label_visibility="collapsed")
if jump_t != curr_time:
    st.session_state.sim_idx = all_times.index(jump_t)
    st.rerun()

btn_cols = st.columns([1, 1, 1, 1, 1, 1, 1, 1.2])
if btn_cols[0].button("◀ -30m", use_container_width=True): move_time(-30); st.rerun()
if btn_cols[1].button("◀ -15m", use_container_width=True): move_time(-15); st.rerun()
if btn_cols[2].button("◀ -5m", use_container_width=True): move_time(-5); st.rerun()
if btn_cols[3].button("◀ -1m", use_container_width=True): move_time(-1); st.rerun()
if btn_cols[4].button("▶ +1m", type="primary", use_container_width=True): move_time(1); st.rerun()
if btn_cols[5].button("▶ +5m", type="primary", use_container_width=True): move_time(5); st.rerun()
if btn_cols[6].button("▶ +15m", type="primary", use_container_width=True): move_time(15); st.rerun()
if btn_cols[7].button("🔄 Reset", use_container_width=True):
    st.session_state.sim_idx = 45
    st.session_state.sim_positions = []
    st.session_state.sim_booked_pnl = 0.0
    st.rerun()

# --- 5. StockMock Style Option Chain (सीधे टेबल में Sell/Buy बटन) ---
st.markdown("---")
st.subheader(f"📋 Option Chain (हर स्ट्राइक के सामने S = Sell, B = Buy दबाएं)")

# टॉप 3 Call & Put OI हाइलाइट
top_ce = curr_df.nlargest(3, "ce_oi")
top_pe = curr_df.nlargest(3, "pe_oi")
oi_c1, oi_c2 = st.columns(2)
oi_c1.error(f"🔴 **Top 3 Call OI:** {', '.join(top_ce['strike'].astype(str))} | Chg OI: **{top_ce['ce_coi'].sum():+,}**")
oi_c2.success(f"🟢 **Top 3 Put OI:** {', '.join(top_pe['strike'].astype(str))} | Chg OI: **{top_pe['pe_coi'].sum():+,}**")

# हेडर लाइन
h = st.columns([1.2, 1.2, 1.0, 1.1, 1.1, 1.0, 1.2, 1.2])
h[0].markdown("**CE OI (Chg)**")
h[1].markdown("**CE LTP**")
h[2].markdown("**CE Trade**")
h[3].markdown("**STRIKE**")
h[4].markdown("**PE Trade**")
h[5].markdown("**PE LTP**")
h[6].markdown("**PE OI (Chg)**")

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

# ATM के आसपास 11 स्ट्राइक्स दिखाएं ताकि स्क्रीन तेज़ चले
atm_idx = curr_df[curr_df["strike"] == atm_strike].index[0]
view_df = curr_df.iloc[max(0, atm_idx - 5): min(len(curr_df), atm_idx + 6)]

for _, r in view_df.iterrows():
    strike = int(r["strike"])
    is_atm = (strike == atm_strike)
    cols = st.columns([1.2, 1.2, 1.0, 1.1, 1.1, 1.0, 1.2, 1.2])

    cols[0].caption(f"{r['ce_oi']//100000:.1f}L ({r['ce_coi']//1000:+}K)")
    cols[1].write(f"**₹{r['ce_ltp']}**")

    b_ce1, b_ce2 = cols[2].columns(2)
    if b_ce1.button("S", key=f"sce_{strike}", help="Sell Call"):
        add_trade(strike, "CE", "SELL", r["ce_ltp"]); st.rerun()
    if b_ce2.button("B", key=f"bce_{strike}", help="Buy Call"):
        add_trade(strike, "CE", "BUY", r["ce_ltp"]); st.rerun()

    if is_atm:
        cols[3].markdown(f"**:orange[⭐ {strike}]**")
    else:
        cols[3].markdown(f"**{strike}**")

    b_pe1, b_pe2 = cols[4].columns(2)
    if b_pe1.button("S", key=f"spe_{strike}", help="Sell Put"):
        add_trade(strike, "PE", "SELL", r["pe_ltp"]); st.rerun()
    if b_pe2.button("B", key=f"bpe_{strike}", help="Buy Put"):
        add_trade(strike, "PE", "BUY", r["pe_ltp"]); st.rerun()

    cols[5].write(f"**₹{r['pe_ltp']}**")
    cols[6].caption(f"{r['pe_oi']//100000:.1f}L ({r['pe_coi']//1000:+}K)")

# --- 6. Live Positions, MTM & StockMock Payoff Graph ---
st.markdown("---")
st.subheader("💼 Positions & Payoff Chart")

open_pos = [p for p in st.session_state.sim_positions if p["status"] == "OPEN"]
running_pnl = 0.0

if open_pos:
    for p in open_pos:
        s_row = curr_df[curr_df["strike"] == p["strike"]].iloc[0]
        ltp = s_row["ce_ltp"] if p["type"] == "CE" else s_row["pe_ltp"]
        pnl = (p["entry"] - ltp) * p["qty"] if p["side"] == "SELL" else (ltp - p["entry"]) * p["qty"]
        running_pnl += pnl

        pc1, pc2, pc3, pc4, pc5 = st.columns([2.2, 1.2, 1.2, 1.5, 1])
        badge = "🔴 SELL" if p["side"] == "SELL" else "🟢 BUY"
        pc1.write(f"{badge} **{p['strike']} {p['type']}** ({p['lots']}L @ {p['time']})")
        pc2.write(f"Entry: ₹{p['entry']}")
        pc3.write(f"LTP: ₹{ltp}")
        color = "green" if pnl >= 0 else "red"
        pc4.markdown(f"MTM: :{color}[**₹{pnl:,.2f}**]")
        if pc5.button("Exit", key=f"ex_{p['id']}"):
            p["status"] = "CLOSED"
            st.session_state.sim_booked_pnl += pnl
            st.rerun()

    # StockMock जैसा Payoff Graph (Expiry पर प्रॉफिट/लॉस कैसा दिखेगा)
    spot_range = np.linspace(spot_now * 0.96, spot_now * 1.04, 100)
    payoff = np.zeros_like(spot_range) + st.session_state.sim_booked_pnl
    for p in open_pos:
        if p["type"] == "CE":
            intr = np.maximum(0, spot_range - p["strike"])
        else:
            intr = np.maximum(0, p["strike"] - spot_range)
        leg_pnl = (p["entry"] - intr) * p["qty"] if p["side"] == "SELL" else (intr - p["entry"]) * p["qty"]
        payoff += leg_pnl

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=spot_range, y=payoff, mode="lines", name="Expiry Payoff", line=dict(color="#00CC96", width=3)))
    fig.add_hline(y=0, line_dash="dash", line_color="gray")
    fig.add_vline(x=spot_now, line_dash="dot", line_color="orange", annotation_text="Current Spot")
    fig.update_layout(title="📈 Expiry Payoff Graph (StockMock Style)", xaxis_title=f"{sel_index} Price", yaxis_title="Profit / Loss (₹)", height=320, margin=dict(l=20, r=20, t=40, b=20))
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("ऊपर Option Chain में किसी भी स्ट्राइक के सामने **S** (Sell) या **B** (Buy) दबाकर अपनी स्ट्रैटेजी बनाएं।")

net_total = st.session_state.sim_booked_pnl + running_pnl
m1, m2, m3 = st.columns(3)
m1.metric("Running MTM", f"₹{running_pnl:,.2f}")
m2.metric("Booked P&L", f"₹{st.session_state.sim_booked_pnl:,.2f}")
m3.metric("Net Total P&L", f"₹{net_total:,.2f}", delta=f"{net_total:,.2f}")
