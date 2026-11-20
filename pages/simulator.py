import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# पेज सेटअप
st.set_page_config(page_title="Options Simulator (StockMock Style)", layout="wide")

LOT_SIZE = 75  # Nifty Lot Size

# --- 1. सैंपल 1-मिनट Historical Data जनरेटर (Price + OI + Change in OI) ---
@st.cache_data
def generate_sample_market_data():
    np.random.seed(42)
    start_time = datetime(2026, 11, 19, 9, 15)
    minutes = 376  # 09:15 से 15:30 तक
    timestamps = [start_time + timedelta(minutes=i) for i in range(minutes)]

    # Nifty Spot का मूवमेंट बनाना
    spot_changes = np.random.normal(0.5, 6.5, size=minutes)
    spot_prices = 24200 + np.cumsum(spot_changes)
    spot_prices = np.round(spot_prices, 2)

    strikes = list(range(23900, 24550, 50))
    records = []

    # सुबह 9:15 का शुरुआती OI बेसलाइन
    base_ce_oi = {k: np.random.randint(15, 45) * 100000 for k in strikes}
    base_pe_oi = {k: np.random.randint(15, 45) * 100000 for k in strikes}
    curr_ce_oi = base_ce_oi.copy()
    curr_pe_oi = base_pe_oi.copy()

    for idx, ts in enumerate(timestamps):
        spot = spot_prices[idx]
        time_decay = (minutes - idx) / minutes  # समय के साथ थीटा डीके

        for strike in strikes:
            # प्रीमियम कैलकुलेशन (Intrinsic + Time Value)
            ce_intrinsic = max(0.0, spot - strike)
            pe_intrinsic = max(0.0, strike - spot)
            dist = abs(spot - strike)
            time_val = max(2.0, (130 * np.exp(-dist / 220)) * (0.4 + 0.6 * time_decay))

            ce_price = round(ce_intrinsic + time_val + np.random.uniform(-0.8, 0.8), 2)
            pe_price = round(pe_intrinsic + time_val + np.random.uniform(-0.8, 0.8), 2)

            # हर मिनट OI में बदलाव
            if idx > 0:
                curr_ce_oi[strike] = max(100000, curr_ce_oi[strike] + np.random.randint(-25000, 45000))
                curr_pe_oi[strike] = max(100000, curr_pe_oi[strike] + np.random.randint(-25000, 45000))

            ce_coi = curr_ce_oi[strike] - base_ce_oi[strike]
            pe_coi = curr_pe_oi[strike] - base_pe_oi[strike]

            records.append({
                "timestamp": ts,
                "time_str": ts.strftime("%H:%M"),
                "spot": spot,
                "strike": strike,
                "ce_price": max(0.5, ce_price),
                "ce_oi": curr_ce_oi[strike],
                "ce_coi": ce_coi,
                "pe_price": max(0.5, pe_price),
                "pe_oi": curr_pe_oi[strike],
                "pe_coi": pe_coi,
            })

    return pd.DataFrame(records), [ts.strftime("%H:%M") for ts in timestamps]

df_data, all_times = generate_sample_market_data()

# --- 2. Session State इनिशियलाइज़ेशन ---
if "sim_idx" not in st.session_state:
    st.session_state.sim_idx = 45  # डिफ़ॉल्ट सुबह 10:00 AM से शुरू
if "positions" not in st.session_state:
    st.session_state.positions = []
if "realized_pnl" not in st.session_state:
    st.session_state.realized_pnl = 0.0

def shift_time(delta_mins):
    new_idx = st.session_state.sim_idx + delta_mins
    st.session_state.sim_idx = max(0, min(len(all_times) - 1, new_idx))

# --- 3. हेडर और Time Machine कंट्रोल बार ---
st.title("📊 Manual Options Simulator (OI + Price)")
st.caption("सैंपल डेटा: NIFTY Weekly Expiry | अपनी स्ट्रैटेजी मैन्युअल टेस्ट करें")

curr_time_str = all_times[st.session_state.sim_idx]
curr_slice = df_data[df_data["time_str"] == curr_time_str].copy()
curr_spot = curr_slice["spot"].iloc[0]
atm_strike = min(curr_slice["strike"], key=lambda x: abs(x - curr_spot))

# टॉप मेट्रिक्स और टाइम कंट्रोल
c1, c2, c3, c4 = st.columns([1.2, 1.2, 1.2, 1.4])
c1.metric("⏰ सिम्युलेटर समय", curr_time_str)
c2.metric("📈 NIFTY Spot", f"{curr_spot:,.2f}")
c3.metric("🎯 ATM Strike", f"{atm_strike}")

# टाइम जंप ड्रॉपडाउन
selected_jump = c4.selectbox("सीधे समय पर जाएं:", all_times, index=st.session_state.sim_idx)
if selected_jump != curr_time_str:
    st.session_state.sim_idx = all_times.index(selected_jump)
    st.rerun()

# आगे-पीछे जाने वाले बटन
st.markdown("##### ⏩ Time Machine Controls")
b1, b2, b3, b4, b5, b6, b7 = st.columns([1, 1, 1, 1, 1, 1, 1.5])
if b1.button("◀ -15m", use_container_width=True):
    shift_time(-15)
    st.rerun()
if b2.button("◀ -5m", use_container_width=True):
    shift_time(-5)
    st.rerun()
if b3.button("◀ -1m", use_container_width=True):
    shift_time(-1)
    st.rerun()
if b4.button("▶ +1m", type="primary", use_container_width=True):
    shift_time(1)
    st.rerun()
if b5.button("▶ +5m", type="primary", use_container_width=True):
    shift_time(5)
    st.rerun()
if b6.button("▶ +15m", type="primary", use_container_width=True):
    shift_time(15)
    st.rerun()
if b7.button("🔄 Reset Day", use_container_width=True):
    st.session_state.sim_idx = 45
    st.session_state.positions = []
    st.session_state.realized_pnl = 0.0
    st.rerun()

st.divider()

# --- 4. क्विक ऑर्डर पैनल (Buy / Sell करने के लिए) ---
st.subheader("🛒 नया ट्रेड लें (Order Entry)")
oc1, oc2, oc3, oc4, oc5 = st.columns([1.2, 1, 1, 1, 1.2])

order_strike = oc1.selectbox("Strike चुनें", curr_slice["strike"].tolist(), index=curr_slice["strike"].tolist().index(atm_strike))
order_type = oc2.selectbox("Option Type", ["CE", "PE"])
order_side = oc3.selectbox("Action", ["SELL", "BUY"])
order_lots = oc4.number_input("Lots (1 Lot = 75)", min_value=1, max_value=50, value=1)

row_selected = curr_slice[curr_slice["strike"] == order_strike].iloc[0]
exec_price = row_selected["ce_price"] if order_type == "CE" else row_selected["pe_price"]

if oc5.button(f"✅ {order_side} @ ₹{exec_price}", type="primary", use_container_width=True):
    st.session_state.positions.append({
        "id": len(st.session_state.positions) + 1,
        "entry_time": curr_time_str,
        "strike": order_strike,
        "type": order_type,
        "side": order_side,
        "lots": order_lots,
        "qty": order_lots * LOT_SIZE,
        "entry_price": exec_price,
        "status": "OPEN",
        "exit_price": None,
        "exit_time": None
    })
    st.rerun()

# --- 5. लाइव पोजीशन और MTM ट्रैकर ---
st.subheader("💼 आपकी पोजीशंस और Live MTM")

unrealized_pnl = 0.0
open_positions = [p for p in st.session_state.positions if p["status"] == "OPEN"]

if open_positions:
    for pos in open_positions:
        s_row = curr_slice[curr_slice["strike"] == pos["strike"]].iloc[0]
        ltp = s_row["ce_price"] if pos["type"] == "CE" else s_row["pe_price"]

        if pos["side"] == "SELL":
            pnl = (pos["entry_price"] - ltp) * pos["qty"]
        else:
            pnl = (ltp - pos["entry_price"]) * pos["qty"]

        unrealized_pnl += pnl

        p1, p2, p3, p4, p5, p6 = st.columns([2, 1, 1.2, 1.2, 1.5, 1])
        p1.write(f"**{pos['strike']} {pos['type']}** ({pos['side']} {pos['lots']}L @ {pos['entry_time']})")
        p2.write(f"Entry: ₹{pos['entry_price']}")
        p3.write(f"LTP: ₹{ltp}")
        pnl_color = "green" if pnl >= 0 else "red"
        p4.markdown(f"P&L: :{pnl_color}[**₹{pnl:,.2f}**]")
        if p6.button("❌ Exit", key=f"exit_{pos['id']}"):
            pos["status"] = "CLOSED"
            pos["exit_price"] = ltp
            pos["exit_time"] = curr_time_str
            st.session_state.realized_pnl += pnl
            st.rerun()
else:
    st.info("अभी कोई ओपन पोजीशन नहीं है। ऊपर से Buy या Sell करके ट्रेड शुरू करें।")

total_mtm = st.session_state.realized_pnl + unrealized_pnl
m1, m2, m3 = st.columns(3)
m1.metric("Running MTM (Open)", f"₹{unrealized_pnl:,.2f}")
m2.metric("Booked P&L (Closed)", f"₹{st.session_state.realized_pnl:,.2f}")
m3.metric("Total Net P&L", f"₹{total_mtm:,.2f}", delta=f"{total_mtm:,.2f}")

st.divider()

# --- 6. Option Chain टेबल (Price + OI + Change in OI) ---
st.subheader(f"📋 Option Chain @ {curr_time_str} (ATM: {atm_strike})")

# टॉप 3 Call और Put OI समरी (आपके पसंदीदा फीचर के अनुसार)
top_ce = curr_slice.nlargest(3, "ce_oi")
top_pe = curr_slice.nlargest(3, "pe_oi")
s_col1, s_col2 = st.columns(2)
s_col1.warning(f"🔴 **Top 3 Call OI Strikes:** {', '.join(top_ce['strike'].astype(str))} | Total Change in OI: **{top_ce['ce_coi'].sum():+,}**")
s_col2.success(f"🟢 **Top 3 Put OI Strikes:** {', '.join(top_pe['strike'].astype(str))} | Total Change in OI: **{top_pe['pe_coi'].sum():+,}**")

display_df = pd.DataFrame({
    "CE OI": curr_slice["ce_oi"].apply(lambda x: f"{x:,}"),
    "CE Chg OI": curr_slice["ce_coi"].apply(lambda x: f"{x:+,}"),
    "CE LTP": curr_slice["ce_price"],
    "STRIKE": curr_slice["strike"].apply(lambda x: f"⭐ {x}" if x == atm_strike else f"{x}"),
    "PE LTP": curr_slice["pe_price"],
    "PE Chg OI": curr_slice["pe_coi"].apply(lambda x: f"{x:+,}"),
    "PE OI": curr_slice["pe_oi"].apply(lambda x: f"{x:,}"),
})

st.dataframe(display_df, use_container_width=True, hide_index=True, height=500)
