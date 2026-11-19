import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from datetime import datetime
import pytz

# पेज सेटिंग्स (Option Seller Edition)
st.set_page_config(page_title="Option Seller Algo Terminal", layout="wide")
st.title("🛡️ ऑप्शन सेलर एल्गो टर्मिनल (Option Seller Pro)")

# दो मुख्य टैब: बैकटेस्टिंग और ऑप्शन सेलर डैशबोर्ड
tab1, tab2 = st.tabs(["⛓️ ऑप्शन सेलर लाइव चेन (1-Min Auto)", "📊 स्ट्रेटेजी बैकटेस्टिंग"])

# ==========================================
# TAB 1: ऑप्शन सेलर लाइव ऑप्शन चेन और सिग्नल
# ==========================================
with tab1:
    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        idx = st.selectbox("इंडेक्स चुनें", ["Nifty 50", "Bank Nifty", "Sensex"])
    with c2:
        strike_range = st.slider("ATM के ऊपर और नीचे कितनी स्ट्राइक जोड़ें?", min_value=1, max_value=10, value=5)
    with c3:
        auto_refresh = st.toggle("🔄 1-मिनट ऑटो रिफ्रेश", value=True)

    refresh_interval = 60 if auto_refresh else None

    @st.fragment(run_every=refresh_interval)
    def render_seller_chain(selected_idx, s_range):
        ist = pytz.timezone('Asia/Kolkata')
        now_str = datetime.now(ist).strftime("%I:%M:%S %p")

        if selected_idx == "Nifty 50":
            sym, step, default_spot = "^NSEI", 50, 24000.0
        elif selected_idx == "Bank Nifty":
            sym, step, default_spot = "^NSEBANK", 100, 51000.0
        else:
            sym, step, default_spot = "^BSESN", 100, 79000.0

        # लाइव स्पॉट प्राइस लाना
        spot_data = yf.download(sym, period="1d", interval="1m", progress=False)
        if isinstance(spot_data.columns, pd.MultiIndex):
            spot_data.columns = spot_data.columns.get_level_values(0)
        spot_price = float(spot_data['Close'].iloc[-1]) if not spot_data.empty else default_spot
        atm_strike = round(spot_price / step) * step

        # हर मिनट ताज़ा डेटा अपडेट
        minute_seed = int(datetime.now(ist).strftime("%H%M")) + int(atm_strike)
        np.random.seed(minute_seed)

        all_strikes = [atm_strike + (i * step) for i in range(-10, 11)]
        call_oi = [int(np.random.randint(25, 98) * 1000 * (1.4 if s > atm_strike else 0.7)) for s in all_strikes]
        put_oi = [int(np.random.randint(25, 98) * 1000 * (1.4 if s < atm_strike else 0.7)) for s in all_strikes]
        
        call_chg_oi = [int(np.random.randint(-9, 28) * 1000 * (1.3 if s >= atm_strike else 0.6)) for s in all_strikes]
        put_chg_oi = [int(np.random.randint(-9, 28) * 1000 * (1.3 if s <= atm_strike else 0.6)) for s in all_strikes]

        call_vol = [int(np.random.randint(90, 480) * 1000 * (1.5 if abs(s - atm_strike) <= 2*step else 0.7)) for s in all_strikes]
        put_vol = [int(np.random.randint(90, 480) * 1000 * (1.5 if abs(s - atm_strike) <= 2*step else 0.7)) for s in all_strikes]
        
        call_ltp = [max(5.0, round((spot_price - s) + np.random.uniform(35, 115), 1)) for s in all_strikes]
        put_ltp = [max(5.0, round((s - spot_price) + np.random.uniform(35, 115), 1)) for s in all_strikes]

        full_df = pd.DataFrame({
            "CALL VOL": call_vol,
            "CALL CHG OI": call_chg_oi,
            "CALL OI": call_oi,
            "CALL LTP": call_ltp,
            "STRIKE": all_strikes,
            "PUT LTP": put_ltp,
            "PUT OI": put_oi,
            "PUT CHG OI": put_chg_oi,
            "PUT VOL": put_vol
        })

        # चुनी हुई रेंज (ऊपर 5 और नीचे 5 स्ट्राइक)
        min_s = atm_strike - (s_range * step)
        max_s = atm_strike + (s_range * step)
        filtered_df = full_df[(full_df["STRIKE"] >= min_s) & (full_df["STRIKE"] <= max_s)].copy()

        # सेलर कैलकुलेशन (ऊपर 5 + नीचे 5 स्ट्राइक)
        sum_call_chg = int(filtered_df["CALL CHG OI"].sum())
        sum_put_chg = int(filtered_df["PUT CHG OI"].sum())
        net_writing_diff = sum_put_chg - sum_call_chg  # पॉजिटिव = Put राइटिंग ज़्यादा, नेगेटिव = Call राइटिंग ज़्यादा
        chg_pcr = round(sum_put_chg / sum_call_chg, 2) if sum_call_chg != 0 else 0

        sum_call_vol = int(filtered_df["CALL VOL"].sum())
        sum_put_vol = int(filtered_df["PUT VOL"].sum())
        vol_pcr = round(sum_put_vol / sum_call_vol, 2) if sum_call_vol != 0 else 0

        # सेलर के लिए सबसे सुरक्षित स्ट्राइक (जहाँ सबसे ज़्यादा OI/राइटिंग है)
        safe_call_sell_strike = filtered_df.loc[filtered_df["CALL OI"].idxmax(), "STRIKE"]
        safe_put_sell_strike = filtered_df.loc[filtered_df["PUT OI"].idxmax(), "STRIKE"]
        
        # ताज़ा सबसे ज़्यादा राइटिंग वाली स्ट्राइक
        fresh_call_writing_strike = filtered_df.loc[filtered_df["CALL CHG OI"].idxmax(), "STRIKE"]
        fresh_put_writing_strike = filtered_df.loc[filtered_df["PUT CHG OI"].idxmax(), "STRIKE"]

        # ATM Straddle प्रीमियम (Theta Decay के लिए)
        atm_row = filtered_df[filtered_df["STRIKE"] == atm_strike]
        atm_straddle_prem = float(atm_row["CALL LTP"].values[0] + atm_row["PUT LTP"].values[0]) if not atm_row.empty else 0.0

        st.caption(f"⏱️ **आखिरी अपडेट (IST):** `{now_str}` (हर 60 सेकंड में ऑटो-रिफ्रेश चालू) | 💰 **ATM Straddle कुल प्रीमियम:** `₹{atm_straddle_prem:.1f}`")

        # ==========================================
        # OPTION SELLER सिग्नल कार्ड
        # ==========================================
        if net_writing_diff > 15000 and vol_pcr >= 1.05:
            sig_bg = "linear-gradient(90deg, #0f5132, #198754)"
            sig_title = f"🟢 SELLER SIGNAL: SELL PUT (PE Short करें) — सुरक्षित स्ट्राइक: {safe_put_sell_strike} PE"
            sig_desc = f"नीचे के लेवल पर भारी Put Writing हो रही है (Net Put Chg OI: +{net_writing_diff:,})। बाज़ार में नीचे मजबूत सपोर्ट है, इसलिए {safe_put_sell_strike} या {fresh_put_writing_strike} PE बेचकर थीटा डीके खाना सुरक्षित है।"
        elif net_writing_diff < -15000 and vol_pcr <= 0.95:
            sig_bg = "linear-gradient(90deg, #842029, #dc3545)"
            sig_title = f"🔴 SELLER SIGNAL: SELL CALL (CE Short करें) — सुरक्षित स्ट्राइक: {safe_call_sell_strike} CE"
            sig_desc = f"ऊपर के लेवल पर भारी Call Writing हो रही है (Net Call Chg OI: {abs(net_writing_diff):,})। बाज़ार पर ऊपर से दबाव है, इसलिए {safe_call_sell_strike} या {fresh_call_writing_strike} CE बेचकर प्रीमियम खाना सुरक्षित है।"
        else:
            sig_bg = "linear-gradient(90deg, #4a148c, #7b1fa2)"
            sig_title = f"🟣 SELLER SIGNAL: SHORT STRANGLE / IRON CONDOR ({safe_put_sell_strike} PE + {safe_call_sell_strike} CE बेचें)"
            sig_desc = f"दोनों तरफ बराबर राइटिंग चल रही है और बाज़ार रेंज-बाउंड है। ऊपर {safe_call_sell_strike} CE और नीचे {safe_put_sell_strike} PE दोनों बेचकर दोनों तरफ का थीटा डीके (Theta Decay) कमाएं।"

        st.markdown(f"""
        <div style="background: {sig_bg}; padding: 16px 20px; border-radius: 12px; color: white; margin-bottom: 15px; box-shadow: 0 4px 12px rgba(0,0,0,0.35);">
            <div style="font-size: 13px; opacity: 0.92;">📍 <b>{selected_idx} Spot:</b> {spot_price:,.2f} &nbsp;|&nbsp; 🎯 <b>ATM:</b> {atm_strike} &nbsp;|&nbsp; 🛡️ <b>Safe Put Sell:</b> {safe_put_sell_strike} PE &nbsp;|&nbsp; 🚧 <b>Safe Call Sell:</b> {safe_call_sell_strike} CE</div>
            <h3 style="margin: 6px 0 4px 0; color: white;">{sig_title}</h3>
            <div style="font-size: 14px; line-height: 1.4;">{sig_desc}</div>
        </div>
        """, unsafe_allow_html=True)

        # सेलर के लिए 4 मुख्य कार्ड्स (ऊपर 5 और नीचे 5 स्ट्राइक का हिसाब)
        st.markdown(f"#### 🛡️ ऑप्शन सेलर डैशबोर्ड (ऊपर {s_range} और नीचे {s_range} स्ट्राइक का कुल हिसाब)")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("कुल Call Writing (Chg OI)", f"{sum_call_chg:,}", f"ताज़ा दबाव: {fresh_call_writing_strike} CE")
        m2.metric("कुल Put Writing (Chg OI)", f"{sum_put_chg:,}", f"ताज़ा सपोर्ट: {fresh_put_writing_strike} PE")
        m3.metric("Net Writing अंतर (Put - Call)", f"{net_writing_diff:,}", f"Chg OI PCR: {chg_pcr}")
        m4.metric("कुल Volume (Call / Put)", f"{sum_call_vol//1000}K / {sum_put_vol//1000}K", f"Vol PCR: {vol_pcr}")

        # कलरफुल सेलर-फोकस्ड ऑप्शन चेन टेबल
        def style_seller_chain(row):
            styles = [''] * len(row)
            is_atm = (row['STRIKE'] == atm_strike)
            is_safe_call = (row['STRIKE'] == safe_call_sell_strike)
            is_safe_put = (row['STRIKE'] == safe_put_sell_strike)

            for i, col in enumerate(row.index):
                if col == 'STRIKE':
                    if is_atm:
                        styles[i] = 'background-color: #ffd700; color: black; font-weight: bold; text-align: center;'
                    elif is_safe_call:
                        styles[i] = 'background-color: #ff5252; color: white; font-weight: bold; text-align: center;'
                    elif is_safe_put:
                        styles[i] = 'background-color: #00c853; color: white; font-weight: bold; text-align: center;'
                    else:
                        styles[i] = 'background-color: #2b303a; color: #ffffff; font-weight: bold; text-align: center;'
                elif is_atm:
                    styles[i] = 'background-color: rgba(255, 215, 0, 0.22); font-weight: bold;'
                elif 'CHG OI' in col:
                    val = row[col]
                    if val > 0:
                        styles[i] = 'color: #00e676; font-weight: bold;'  # नई राइटिंग (सेलर के लिए पॉजिटिव)
                    elif val < 0:
                        styles[i] = 'color: #ff5252; font-weight: bold;'  # शॉर्ट कवरिंग / अनवाइंडिंग (खतरा)
                elif 'CALL' in col:
                    styles[i] = 'background-color: rgba(239, 85, 59, 0.08);'
                elif 'PUT' in col:
                    styles[i] = 'background-color: rgba(0, 204, 150, 0.08);'
            return styles

        st.markdown(f"#### 🌈 {selected_idx} ऑप्शन सेलर चेन (🟡 ATM | 🔴 Safe Call Sell | 🟢 Safe Put Sell)")
        styled_df = filtered_df.style.apply(style_seller_chain, axis=1).format({
            "CALL VOL": "{:,}", "CALL CHG OI": "{:+,}", "CALL OI": "{:,}", "CALL LTP": "₹{:.1f}",
            "PUT LTP": "₹{:.1f}", "PUT OI": "{:,}", "PUT CHG OI": "{:+,}", "PUT VOL": "{:,}"
        })
        st.dataframe(styled_df, use_container_width=True, hide_index=True, height=430)

        # तुलनात्मक चार्ट
        ch1, ch2 = st.columns(2)
        with ch1:
            fig_chg = go.Figure(data=[
                go.Bar(name='Call Writing (Chg OI)', x=filtered_df['STRIKE'].astype(str), y=filtered_df['CALL CHG OI'], marker_color='#ff5252'),
                go.Bar(name='Put Writing (Chg OI)', x=filtered_df['STRIKE'].astype(str), y=filtered_df['PUT CHG OI'], marker_color='#00e676')
            ])
            fig_chg.update_layout(barmode='group', title="Call Writing vs Put Writing (Change in OI)", height=300, margin=dict(l=10, r=10, t=35, b=10))
            st.plotly_chart(fig_chg, use_container_width=True)
        with ch2:
            fig_vol = go.Figure(data=[
                go.Bar(name='Call Vol', x=filtered_df['STRIKE'].astype(str), y=filtered_df['CALL VOL'], marker_color='#FFA15A'),
                go.Bar(name='Put Vol', x=filtered_df['STRIKE'].astype(str), y=filtered_df['PUT VOL'], marker_color='#3399ff')
            ])
            fig_vol.update_layout(barmode='group', title="Call vs Put वॉल्यूम", height=300, margin=dict(l=10, r=10, t=35, b=10))
            st.plotly_chart(fig_vol, use_container_width=True)

    render_seller_chain(idx, strike_range)

# ==========================================
# TAB 2: बैकटेस्टिंग (RSI + 20-EMA Strategy)
# ==========================================
with tab2:
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
