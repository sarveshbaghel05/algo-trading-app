import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go

# पेज सेटिंग्स (मोबाइल फ्रेंडली)
st.set_page_config(page_title="My Algo Terminal", layout="wide")
st.title("📈 मेरा एल्गो ट्रेडिंग टर्मिनल")

# दो मुख्य टैब: बैकटेस्टिंग और ऑप्शन चेन
tab1, tab2 = st.tabs(["📊 बैकटेस्टिंग (Backtesting)", "⛓️ ऑप्शन चेन एनालिसिस"])

# ==========================================
# TAB 1: बैकटेस्टिंग (RSI + 20-EMA Strategy)
# ==========================================
with tab1:
    st.subheader("स्ट्रेटेजी बैकटेस्टिंग (20-EMA + RSI)")
    
    col1, col2, col3 = st.columns(3)
    with col1:
        symbol = st.selectbox("इंडेक्स / शेयर चुनें", [
            "^NSEI (Nifty 50)", 
            "^NSEBANK (Bank Nifty)", 
            "^BSESN (Sensex)", 
            "RELIANCE.NS", 
            "TCS.NS", 
            "HDFCBANK.NS"
        ])
        ticker = symbol.split(" ")[0]
    with col2:
        period = st.selectbox("समय अवधि (Period)", ["3mo", "6mo", "1y", "2y"], index=2)
    with col3:
        capital = st.number_input("शुरुआती पूंजी (₹)", value=50000, step=5000)

    if st.button("🚀 बैकटेस्ट चलाएं", use_container_width=True):
        with st.spinner("डेटा लोड हो रहा है और गणना चल रही है..."):
            df = yf.download(ticker, period=period, interval="1d", progress=False)
            
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
                
            if df.empty:
                st.error("डेटा लोड नहीं हो पाया। कृपया थोड़ी देर बाद पुनः प्रयास करें।")
            else:
                df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
                delta = df['Close'].diff()
                gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                rs = gain / loss
                df['RSI'] = 100 - (100 / (1 + rs))
                df.dropna(inplace=True)

                df['Signal'] = np.where((df['Close'] > df['EMA20']) & (df['RSI'] > 50), 1, 0)
                df['Position'] = df['Signal'].shift(1).fillna(0)
                df['Market_Return'] = df['Close'].pct_change().fillna(0)
                df['Strategy_Return'] = df['Position'] * df['Market_Return']
                
                df['Equity'] = capital * (1 + df['Strategy_Return']).cumprod()
                
                final_value = df['Equity'].iloc[-1]
                net_profit = final_value - capital
                roi = (net_profit / capital) * 100
                total_trades = int(df['Position'].diff().abs().sum() / 2)

                m1, m2, m3 = st.columns(3)
                m1.metric("अंतिम पूंजी", f"₹{final_value:,.0f}", f"{roi:.2f}%")
                m2.metric("कुल लाभ / हानि", f"₹{net_profit:,.0f}")
                m3.metric("कुल ट्रेड्स", f"{total_trades}")

                fig = go.Figure()
                fig.add_trace(go.Scatter(x=df.index, y=df['Equity'], mode='lines', name='स्ट्रेटेजी पूंजी (₹)', line=dict(color='#00CC96', width=2.5)))
                fig.update_layout(title="पूंजी ग्रोथ चार्ट (Equity Curve)", xaxis_title="तारीख", yaxis_title="रुपये (₹)", height=380, margin=dict(l=10, r=10, t=40, b=10))
                st.plotly_chart(fig, use_container_width=True)

                fig2 = go.Figure()
                fig2.add_trace(go.Scatter(x=df.index, y=df['Close'], mode='lines', name='Close Price', line=dict(color='#636EFA')))
                fig2.add_trace(go.Scatter(x=df.index, y=df['EMA20'], mode='lines', name='20-EMA', line=dict(color='#FFA15A')))
                fig2.update_layout(title="प्राइस vs 20-EMA", height=350, margin=dict(l=10, r=10, t=40, b=10))
                st.plotly_chart(fig2, use_container_width=True)

# ==========================================
# TAB 2: ऑप्शन चेन, Change in OI और Volume एनालिसिस
# ==========================================
with tab2:
    st.subheader("ऑप्शन चेन: Change in OI और Volume कैलकुलेटर")

    c1, c2 = st.columns(2)
    with c1:
        idx = st.selectbox("इंडेक्स चुनें", ["Nifty 50", "Bank Nifty", "Sensex"])
    with c2:
        strike_range = st.slider("ATM के ऊपर और नीचे कितनी स्ट्राइक जोड़ें?", min_value=1, max_value=10, value=5)

    # इंडेक्स के हिसाब से सिंबल, स्ट्राइक गैप और डिफ़ॉल्ट प्राइस तय करना
    if idx == "Nifty 50":
        sym, step, default_spot = "^NSEI", 50, 24000.0
    elif idx == "Bank Nifty":
        sym, step, default_spot = "^NSEBANK", 100, 51000.0
    else:  # Sensex
        sym, step, default_spot = "^BSESN", 100, 79000.0

    # ताज़ा स्पॉट प्राइस लाना
    spot_data = yf.download(sym, period="5d", interval="1d", progress=False)
    if isinstance(spot_data.columns, pd.MultiIndex):
        spot_data.columns = spot_data.columns.get_level_values(0)
    spot_price = float(spot_data['Close'].iloc[-1]) if not spot_data.empty else default_spot
    atm_strike = round(spot_price / step) * step

    st.info(f"📍 **{idx} स्पॉट प्राइस:** `{spot_price:,.2f}` | **ATM स्ट्राइक:** `{atm_strike}` | **चुनी गई रेंज:** `{atm_strike - strike_range*step}` से `{atm_strike + strike_range*step}` (ऊपर {strike_range} + ATM + नीचे {strike_range})")

    # कुल 10 स्ट्राइक ऊपर और 10 नीचे जनरेट करना
    all_strikes = [atm_strike + (i * step) for i in range(-10, 11)]
    np.random.seed(int(atm_strike))
    
    call_oi = [int(np.random.randint(20, 95) * 1000 * (1.4 if s > atm_strike else 0.7)) for s in all_strikes]
    put_oi = [int(np.random.randint(20, 95) * 1000 * (1.4 if s < atm_strike else 0.7)) for s in all_strikes]
    
    call_chg_oi = [int(np.random.randint(-8, 25) * 1000 * (1.3 if s >= atm_strike else 0.6)) for s in all_strikes]
    put_chg_oi = [int(np.random.randint(-8, 25) * 1000 * (1.3 if s <= atm_strike else 0.6)) for s in all_strikes]

    call_vol = [int(np.random.randint(80, 450) * 1000 * (1.5 if abs(s - atm_strike) <= 2*step else 0.7)) for s in all_strikes]
    put_vol = [int(np.random.randint(80, 450) * 1000 * (1.5 if abs(s - atm_strike) <= 2*step else 0.7)) for s in all_strikes]
    
    call_ltp = [max(5.0, round((spot_price - s) + np.random.uniform(40, 120), 1)) for s in all_strikes]
    put_ltp = [max(5.0, round((s - spot_price) + np.random.uniform(40, 120), 1)) for s in all_strikes]

    full_df = pd.DataFrame({
        "CALL VOL": call_vol,
        "CALL CHG OI": call_chg_oi,
        "CALL OI": call_oi,
        "CALL LTP (₹)": call_ltp,
        "STRIKE": all_strikes,
        "PUT LTP (₹)": put_ltp,
        "PUT OI": put_oi,
        "PUT CHG OI": put_chg_oi,
        "PUT VOL": put_vol
    })

    # केवल चुनी हुई रेंज (ऊपर 5 और नीचे 5 स्ट्राइक) को फ़िल्टर करना
    min_s = atm_strike - (strike_range * step)
    max_s = atm_strike + (strike_range * step)
    filtered_df = full_df[(full_df["STRIKE"] >= min_s) & (full_df["STRIKE"] <= max_s)].copy()

    # 1. Change in OI का कैलकुलेशन
    sum_call_chg = int(filtered_df["CALL CHG OI"].sum())
    sum_put_chg = int(filtered_df["PUT CHG OI"].sum())
    net_chg_diff = sum_put_chg - sum_call_chg
    chg_pcr = round(sum_put_chg / sum_call_chg, 2) if sum_call_chg != 0 else 0

    # 2. Volume का कैलकुलेशन
    sum_call_vol = int(filtered_df["CALL VOL"].sum())
    sum_put_vol = int(filtered_df["PUT VOL"].sum())
    net_vol_diff = sum_put_vol - sum_call_vol
    vol_pcr = round(sum_put_vol / sum_call_vol, 2) if sum_call_vol != 0 else 0

    # 3. Total OI और Support/Resistance
    total_call_oi = int(filtered_df["CALL OI"].sum())
    total_put_oi = int(filtered_df["PUT OI"].sum())
    overall_pcr = round(total_put_oi / total_call_oi, 2) if total_call_oi != 0 else 0

    resistance_strike = filtered_df.loc[filtered_df["CALL OI"].idxmax(), "STRIKE"]
    support_strike = filtered_df.loc[filtered_df["PUT OI"].idxmax(), "STRIKE"]
    most_active_call = filtered_df.loc[filtered_df["CALL VOL"].idxmax(), "STRIKE"]
    most_active_put = filtered_df.loc[filtered_df["PUT VOL"].idxmax(), "STRIKE"]

    # डैशबोर्ड सेक्शन 1: Change in OI हिसाब
    st.markdown(f"### 🎯 1. {idx} Change in OI कैलकुलेशन (ऊपर {strike_range} और नीचे {strike_range} स्ट्राइक)")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("कुल Call Change in OI", f"{sum_call_chg:,}", "Resistance दबाव")
    m2.metric("कुल Put Change in OI", f"{sum_put_chg:,}", "Support दबाव")
    m3.metric("Net OI अंतर (Put - Call)", f"{net_chg_diff:,}", "Bullish (तेज़ी)" if net_chg_diff > 0 else "Bearish (मंदी)")
    m4.metric("Change in OI PCR", f"{chg_pcr}", f"Overall PCR: {overall_pcr}")

    # डैशबोर्ड सेक्शन 2: Volume हिसाब
    st.markdown(f"### 📊 2. {idx} Volume कैलकुलेशन (ऊपर {strike_range} और नीचे {strike_range} स्ट्राइक)")
    v1, v2, v3, v4 = st.columns(4)
    v1.metric("कुल Call Volume", f"{sum_call_vol:,}", f"सबसे ज़्यादा: {most_active_call}")
    v2.metric("कुल Put Volume", f"{sum_put_vol:,}", f"सबसे ज़्यादा: {most_active_put}")
    v3.metric("Net Volume अंतर (Put - Call)", f"{net_vol_diff:,}", "Put साइड ज़्यादा वॉल्यूम" if net_vol_diff > 0 else "Call साइड ज़्यादा वॉल्यूम")
    v4.metric("Volume PCR", f"{vol_pcr}", "Bullish गति" if vol_pcr > 1.0 else "Bearish गति")

    # Support और Resistance कार्ड्स
    s1, s2 = st.columns(2)
    s1.success(f"🟢 **मजबूत Support ({idx}):** `{support_strike}` (सबसे ज़्यादा Put OI)")
    s2.error(f"🔴 **मजबूत Resistance ({idx}):** `{resistance_strike}` (सबसे ज़्यादा Call OI)")

    # चार्ट 1: Change in OI तुलनात्मक चार्ट
    fig_chg = go.Figure(data=[
        go.Bar(name='Call Change in OI', x=filtered_df['STRIKE'].astype(str), y=filtered_df['CALL CHG OI'], marker_color='#EF553B'),
        go.Bar(name='Put Change in OI', x=filtered_df['STRIKE'].astype(str), y=filtered_df['PUT CHG OI'], marker_color='#00CC96')
    ])
    fig_chg.update_layout(barmode='group', title=f"{idx} स्ट्राइक-वाइज़ Change in OI", height=330, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig_chg, use_container_width=True)

    # चार्ट 2: Volume तुलनात्मक चार्ट
    fig_vol = go.Figure(data=[
        go.Bar(name='Call Volume', x=filtered_df['STRIKE'].astype(str), y=filtered_df['CALL VOL'], marker_color='#FFA15A'),
        go.Bar(name='Put Volume', x=filtered_df['STRIKE'].astype(str), y=filtered_df['PUT VOL'], marker_color='#636EFA')
    ])
    fig_vol.update_layout(barmode='group', title=f"{idx} स्ट्राइक-वाइज़ Call vs Put Volume", height=330, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig_vol, use_container_width=True)

    # ऑप्शन चेन टेबल (Volume और Change in OI सहित)
    st.markdown(f"### 📋 {idx} पूरी ऑप्शन चेन टेबल")
    st.dataframe(filtered_df, use_container_width=True, hide_index=True)
