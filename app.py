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
        symbol = st.selectbox("इंडेक्स / शेयर चुनें", ["^NSEI (Nifty 50)", "^NSEBANK (Bank Nifty)", "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS"])
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
                # 20-EMA और RSI की गणना
                df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
                delta = df['Close'].diff()
                gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                rs = gain / loss
                df['RSI'] = 100 - (100 / (1 + rs))
                df.dropna(inplace=True)

                # ट्रेडिंग नियम: Close > EMA20 और RSI > 50 पर Buy, वरना Exit
                df['Signal'] = np.where((df['Close'] > df['EMA20']) & (df['RSI'] > 50), 1, 0)
                df['Position'] = df['Signal'].shift(1).fillna(0)
                df['Market_Return'] = df['Close'].pct_change().fillna(0)
                df['Strategy_Return'] = df['Position'] * df['Market_Return']
                
                df['Equity'] = capital * (1 + df['Strategy_Return']).cumprod()
                
                final_value = df['Equity'].iloc[-1]
                net_profit = final_value - capital
                roi = (net_profit / capital) * 100
                total_trades = int(df['Position'].diff().abs().sum() / 2)

                # रिजल्ट कार्ड्स
                m1, m2, m3 = st.columns(3)
                m1.metric("अंतिम पूंजी", f"₹{final_value:,.0f}", f"{roi:.2f}%")
                m2.metric("कुल लाभ / हानि", f"₹{net_profit:,.0f}")
                m3.metric("कुल ट्रेड्स", f"{total_trades}")

                # इक्विटी कर्व चार्ट
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=df.index, y=df['Equity'], mode='lines', name='स्ट्रेटेजी पूंजी (₹)', line=dict(color='#00CC96', width=2.5)))
                fig.update_layout(title="पूंजी ग्रोथ चार्ट (Equity Curve)", xaxis_title="तारीख", yaxis_title="रुपये (₹)", height=380, margin=dict(l=10, r=10, t=40, b=10))
                st.plotly_chart(fig, use_container_width=True)

                # प्राइस और EMA चार्ट
                fig2 = go.Figure()
                fig2.add_trace(go.Scatter(x=df.index, y=df['Close'], mode='lines', name='Close Price', line=dict(color='#636EFA')))
                fig2.add_trace(go.Scatter(x=df.index, y=df['EMA20'], mode='lines', name='20-EMA', line=dict(color='#FFA15A')))
                fig2.update_layout(title="प्राइस vs 20-EMA", height=350, margin=dict(l=10, r=10, t=40, b=10))
                st.plotly_chart(fig2, use_container_width=True)

# ==========================================
# TAB 2: ऑप्शन चेन और एनालिसिस
# ==========================================
with tab2:
    st.subheader("ऑप्शन चेन और OI एनालिसिस")
    st.info("💡 नोट: अभी यह लाइव स्पॉट प्राइस के आधार पर ऑप्शन चेन सिमुलेशन दिखा रहा है। अगले चरण में हम इसमें आपके ब्रोकर (Angel One/Dhan/Upstox) का फ्री लाइव डेटा जोड़ेंगे।")

    idx = st.selectbox("इंडेक्स चुनें", ["Nifty 50", "Bank Nifty"])
    sym = "^NSEI" if idx == "Nifty 50" else "^NSEBANK"
    step = 50 if idx == "Nifty 50" else 100

    # ताज़ा स्पॉट प्राइस लाना
    spot_data = yf.download(sym, period="5d", interval="1d", progress=False)
    if isinstance(spot_data.columns, pd.MultiIndex):
        spot_data.columns = spot_data.columns.get_level_values(0)
    spot_price = float(spot_data['Close'].iloc[-1]) if not spot_data.empty else (24000.0 if idx == "Nifty 50" else 51000.0)
    atm_strike = round(spot_price / step) * step

    st.write(f"**वर्तमान स्पॉट प्राइस:** `{spot_price:,.2f}` | **ATM स्ट्राइक:** `{atm_strike}`")

    # स्ट्राइक प्राइस जनरेट करना
    strikes = [atm_strike + (i * step) for i in range(-6, 7)]
    np.random.seed(int(atm_strike))
    
    call_oi = [int(np.random.randint(15, 95) * 1000 * (1.4 if s > atm_strike else 0.7)) for s in strikes]
    put_oi = [int(np.random.randint(15, 95) * 1000 * (1.4 if s < atm_strike else 0.7)) for s in strikes]
    call_ltp = [max(5.0, round((spot_price - s) + np.random.uniform(40, 120), 1)) for s in strikes]
    put_ltp = [max(5.0, round((s - spot_price) + np.random.uniform(40, 120), 1)) for s in strikes]

    oc_df = pd.DataFrame({
        "CALL OI": call_oi,
        "CALL LTP (₹)": call_ltp,
        "STRIKE": strikes,
        "PUT LTP (₹)": put_ltp,
        "PUT OI": put_oi
    })

    # एनालिसिस कैलकुलेशन (PCR, Resistance, Support, Max Pain)
    total_call_oi = oc_df["CALL OI"].sum()
    total_put_oi = oc_df["PUT OI"].sum()
    pcr = round(total_put_oi / total_call_oi, 2)
    
    resistance_strike = oc_df.loc[oc_df["CALL OI"].idxmax(), "STRIKE"]
    support_strike = oc_df.loc[oc_df["PUT OI"].idxmax(), "STRIKE"]

    # Max Pain गणना
    pain_values = []
    for expiry_price in strikes:
        call_loss = sum(max(0, expiry_price - s) * oi for s, oi in zip(strikes, call_oi))
        put_loss = sum(max(0, s - expiry_price) * oi for s, oi in zip(strikes, put_oi))
        pain_values.append(call_loss + put_loss)
    max_pain = strikes[np.argmin(pain_values)]

    # एनालिसिस मेट्रिक्स दिखाना
    a1, a2, a3, a4 = st.columns(4)
    sentiment = "Bullish (तेज़ी)" if pcr > 1.0 else ("Bearish (मंदी)" if pcr < 0.8 else "Sideways")
    a1.metric("PCR (Put-Call Ratio)", f"{pcr}", sentiment)
    a2.metric("मजबूत Resistance", f"{resistance_strike}", "सबसे ज़्यादा Call OI")
    a3.metric("मजबूत Support", f"{support_strike}", "सबसे ज़्यादा Put OI")
    a4.metric("Max Pain लेवल", f"{max_pain}")

    # OI तुलनात्मक बार चार्ट
    fig_oi = go.Figure(data=[
        go.Bar(name='Call OI (Resistance)', x=oc_df['STRIKE'].astype(str), y=oc_df['CALL OI'], marker_color='#EF553B'),
        go.Bar(name='Put OI (Support)', x=oc_df['STRIKE'].astype(str), y=oc_df['PUT OI'], marker_color='#00CC96')
    ])
    fig_oi.update_layout(barmode='group', title="स्ट्राइक-वाइज़ Call vs Put Open Interest", height=350, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig_oi, use_container_width=True)

    # ऑप्शन चेन टेबल
    st.write("### 📋 ऑप्शन चेन डेटा")
    st.dataframe(oc_df, use_container_width=True, hide_index=True)
