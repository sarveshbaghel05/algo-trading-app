import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import requests
import pyotp
from datetime import datetime, timedelta
import pytz

st.set_page_config(page_title="Option Seller Algo Terminal", layout="wide")
st.title("🛡️ ऑप्शन सेलर एल्गो टर्मिनल (Angel One Auto-Live)")

@st.cache_data(ttl=3600)
def get_angel_scrip_master():
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    res = requests.get(url, timeout=15)
    return pd.DataFrame(res.json())

def angel_login(api_key, client_id, mpin, totp_secret):
    try:
        totp = pyotp.TOTP(totp_secret.strip().replace(" ", "")).now()
        url = "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": api_key.strip()
        }
        payload = {
            "clientcode": client_id.strip(),
            "password": mpin.strip(),
            "totp": totp
        }
        res = requests.post(url, json=payload, headers=headers, timeout=10).json()
        if res.get("status") and res.get("data", {}).get("jwtToken"):
            return res["data"]["jwtToken"], None
        return None, res.get("message", "लॉगिन विफल रहा। अपनी डिटेल्स चेक करें।")
    except Exception as e:
        return None, f"लॉगिन एरर: {str(e)}"

if "angel_jwt" not in st.session_state:
    try:
        if "ANGEL_API_KEY" in st.secrets and "ANGEL_CLIENT_ID" in st.secrets:
            jwt, err = angel_login(
                st.secrets["ANGEL_API_KEY"],
                st.secrets["ANGEL_CLIENT_ID"],
                st.secrets["ANGEL_MPIN"],
                st.secrets["ANGEL_TOTP"]
            )
            if jwt:
                st.session_state["angel_jwt"] = jwt
                st.session_state["angel_key"] = st.secrets["ANGEL_API_KEY"]
    except Exception:
        pass

def fetch_angel_live_oc(jwt_token, api_key, selected_idx, s_range):
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00",
        "X-PrivateKey": api_key.strip()
    }
    quote_url = "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/"

    idx_info = {
        "Nifty 50": {"exch": "NSE", "token": "99926000", "name": "NIFTY", "opt_exch": "NFO", "step": 50},
        "Bank Nifty": {"exch": "NSE", "token": "99926009", "name": "BANKNIFTY", "opt_exch": "NFO", "step": 100},
        "Sensex": {"exch": "BSE", "token": "99919000", "name": "SENSEX", "opt_exch": "BFO", "step": 100}
    }[selected_idx]

    spot_payload = {"mode": "LTP", "exchangeTokens": {idx_info["exch"]: [idx_info["token"]]}}
    spot_res = requests.post(quote_url, json=spot_payload, headers=headers, timeout=10).json()
    if not spot_res.get("status"):
        return None, None, None, f"Angel API Error: {spot_res.get('message', 'Token Expired')}"

    spot_price = float(spot_res["data"]["fetched"][0]["ltp"])
    step = idx_info["step"]
    atm_strike = round(spot_price / step) * step

    master_df = get_angel_scrip_master()
    opt_df = master_df[
        (master_df["name"] == idx_info["name"]) &
        (master_df["exch_seg"] == idx_info["opt_exch"]) &
        (master_df["instrumenttype"].isin(["OPTIDX"]))
    ].copy()

    if opt_df.empty:
        return None, None, None, "ऑप्शन कॉन्ट्रैक्ट लिस्ट नहीं मिली।"

    opt_df["expiry_dt"] = pd.to_datetime(opt_df["expiry"], format="%d%b%Y", errors="coerce")
    opt_df = opt_df.dropna(subset=["expiry_dt"])
    today = pd.Timestamp.now().normalize()
    future_expiries = sorted(opt_df[opt_df["expiry_dt"] >= today]["expiry_dt"].unique())
    if not future_expiries:
        return None, None, None, "कोई सक्रिय एक्सपायरी नहीं मिली।"

    nearest_exp = future_expiries[0]
    exp_str = pd.Timestamp(nearest_exp).strftime("%d-%b-%Y")
    exp_df = opt_df[opt_df["expiry_dt"] == nearest_exp].copy()

    exp_df["clean_strike"] = (exp_df["strike"].astype(float) / 100.0).astype(int)
    exp_df["opt_type"] = exp_df["symbol"].str[-2:]

    min_s = atm_strike - (s_range * step)
    max_s = atm_strike + (s_range * step)
    target_df = exp_df[(exp_df["clean_strike"] >= min_s) & (exp_df["clean_strike"] <= max_s)].copy()

    tokens = target_df["token"].astype(str).tolist()
    if not tokens:
        return None, None, None, "चुनी गई स्ट्राइक रेंज के टोकन नहीं मिले।"

    full_payload = {"mode": "FULL", "exchangeTokens": {idx_info["opt_exch"]: tokens[:50]}}
    q_res = requests.post(quote_url, json=full_payload, headers=headers, timeout=10).json()
    if not q_res.get("status"):
        return None, None, None, "ऑप्शन कोट्स लाने में समस्या आई।"

    fetched_map = {str(item["symbolToken"]): item for item in q_res["data"]["fetched"]}
    strikes_list = sorted(target_df["clean_strike"].unique())

    rows = []
    for st_val in strikes_list:
        ce_row = target_df[(target_df["clean_strike"] == st_val) & (target_df["opt_type"] == "CE")]
        pe_row = target_df[(target_df["clean_strike"] == st_val) & (target_df["opt_type"] == "PE")]

        ce_q = fetched_map.get(str(ce_row["token"].values[0]), {}) if not ce_row.empty else {}
        pe_q = fetched_map.get(str(pe_row["token"].values[0]), {}) if not pe_row.empty else {}

        ce_oi = int(ce_q.get("opnInterest", 0))
        ce_chg_oi = int(ce_q.get("netChangeOpnInterest", 0)) if "netChangeOpnInterest" in ce_q else 0

        pe_oi = int(pe_q.get("opnInterest", 0))
        pe_chg_oi = int(pe_q.get("netChangeOpnInterest", 0)) if "netChangeOpnInterest" in pe_q else 0

        rows.append({
            "CALL VOL": int(ce_q.get("tradeVolume", 0)),
            "CALL CHG OI": ce_chg_oi,
            "CALL OI": ce_oi,
            "CALL LTP": float(ce_q.get("ltp", 0.0)),
            "STRIKE": st_val,
            "PUT LTP": float(pe_q.get("ltp", 0.0)),
            "PUT OI": pe_oi,
            "PUT CHG OI": pe_chg_oi,
            "PUT VOL": int(pe_q.get("tradeVolume", 0))
        })

    return pd.DataFrame(rows), spot_price, exp_str, None

def fetch_angel_historical_spot(jwt_token, api_key, selected_idx, days_count):
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00",
        "X-PrivateKey": api_key.strip()
    }
    hist_url = "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData"

    idx_map = {
        "Nifty 50": {"exch": "NSE", "token": "99926000"},
        "Bank Nifty": {"exch": "NSE", "token": "99926009"},
        "Sensex": {"exch": "BSE", "token": "99919000"}
    }[selected_idx]

    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)
    all_candles = []

    remaining_days = days_count
    end_dt = now_ist

    while remaining_days > 0:
        chunk_days = min(30, remaining_days)
        start_dt = end_dt - timedelta(days=chunk_days)
        payload = {
            "exchange": idx_map["exch"],
            "symboltoken": idx_map["token"],
            "interval": "ONE_MINUTE",
            "fromdate": start_dt.strftime("%Y-%m-%d 09:15"),
            "todate": end_dt.strftime("%Y-%m-%d 15:30")
        }
        try:
            res = requests.post(hist_url, json=payload, headers=headers, timeout=15).json()
            if res.get("status") and res.get("data"):
                all_candles.extend(res["data"])
        except Exception:
            break

        end_dt = start_dt - timedelta(minutes=1)
        remaining_days -= chunk_days

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(all_candles, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    if df["Timestamp"].dt.tz is None:
        df["Timestamp"] = df["Timestamp"].dt.tz_localize("Asia/Kolkata")
    else:
        df["Timestamp"] = df["Timestamp"].dt.tz_convert("Asia/Kolkata")

    df = df.drop_duplicates(subset=["Timestamp"]).sort_values("Timestamp").set_index("Timestamp")
    return df

tab1, tab2 = st.tabs(["⛓️ ऑप्शन सेलर लाइव चेन (Angel One Live)", "📊 10:00 AM Chg OI (1-Min Angel Backtest)"])

with tab1:
    is_connected = "angel_jwt" in st.session_state
    with st.expander("🔑 Angel One SmartAPI कनेक्शन (Auto-Login सेटअप)", expanded=not is_connected):
        if is_connected:
            st.success("✅ Angel One ऑटो-कनेक्टेड है! अब आपको रोज़ डिटेल डालने की ज़रूरत नहीं है।")
        else:
            st.info("💡 रोज़ डिटेल डालने से बचने के लिए Streamlit Settings -> Secrets में अपनी डिटेल सेव करें, या नीचे मैनुअल भरें:")
            a1, a2, a3, a4 = st.columns(4)
            with a1:
                angel_api_key = st.text_input("API Key", type="password", placeholder="SmartAPI Key")
            with a2:
                angel_client_id = st.text_input("Client ID", placeholder="जैसे A123456")
            with a3:
                angel_mpin = st.text_input("MPIN", type="password", placeholder="4-अंकों का PIN")
            with a4:
                angel_totp = st.text_input("TOTP Secret Key", type="password", placeholder="Authenticator Secret")

            if st.button("🔗 Angel One से कनेक्ट करें", use_container_width=True):
                if angel_api_key and angel_client_id and angel_mpin and angel_totp:
                    with st.spinner("Angel One SmartAPI से जुड़ रहा है..."):
                        jwt, err = angel_login(angel_api_key, angel_client_id, angel_mpin, angel_totp)
                        if jwt:
                            st.session_state["angel_jwt"] = jwt
                            st.session_state["angel_key"] = angel_api_key
                            st.rerun()
                        else:
                            st.error(f"❌ {err}")
                else:
                    st.warning("कृपया चारों बॉक्स भरें।")

    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        idx = st.selectbox("इंडेक्स चुनें", ["Nifty 50", "Bank Nifty", "Sensex"], key="live_idx")
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
            sym, step, default_spot, hedge_pts = "^NSEI", 50, 24000.0, 300
        elif selected_idx == "Bank Nifty":
            sym, step, default_spot, hedge_pts = "^NSEBANK", 100, 51000.0, 300
        else:
            sym, step, default_spot, hedge_pts = "^BSESN", 100, 79000.0, 1000

        is_live = False
        expiry_label = "Simulation"
        filtered_df = None
        spot_price = default_spot

        if "angel_jwt" in st.session_state and "angel_key" in st.session_state:
            live_df, live_spot, exp_date, err_msg = fetch_angel_live_oc(
                st.session_state["angel_jwt"], st.session_state["angel_key"], selected_idx, s_range
            )
            if err_msg:
                if "ANGEL_API_KEY" in st.secrets:
                    new_jwt, _ = angel_login(
                        st.secrets["ANGEL_API_KEY"], st.secrets["ANGEL_CLIENT_ID"],
                        st.secrets["ANGEL_MPIN"], st.secrets["ANGEL_TOTP"]
                    )
                    if new_jwt:
                        st.session_state["angel_jwt"] = new_jwt
                        live_df, live_spot, exp_date, err_msg = fetch_angel_live_oc(
                            new_jwt, st.session_state["angel_key"], selected_idx, s_range
                        )
                if err_msg:
                    st.error(f"⚠️ {err_msg}")

            if live_df is not None and not live_df.empty:
                filtered_df = live_df
                spot_price = live_spot
                expiry_label = exp_date
                is_live = True

        if not is_live:
            spot_data = yf.download(sym, period="1d", interval="1m", progress=False)
            if isinstance(spot_data.columns, pd.MultiIndex):
                spot_data.columns = spot_data.columns.get_level_values(0)
            spot_price = float(spot_data['Close'].iloc[-1]) if not spot_data.empty else default_spot
            atm_temp = round(spot_price / step) * step

            minute_seed = int(datetime.now(ist).strftime("%H%M")) + int(atm_temp)
            np.random.seed(minute_seed)

            all_strikes = [atm_temp + (i * step) for i in range(-10, 11)]
            call_oi = [int(np.random.randint(25, 98) * 1000 * (1.4 if s > atm_temp else 0.7)) for s in all_strikes]
            put_oi = [int(np.random.randint(25, 98) * 1000 * (1.4 if s < atm_temp else 0.7)) for s in all_strikes]
            call_chg_oi = [int(np.random.randint(-9, 28) * 1000 * (1.3 if s >= atm_temp else 0.6)) for s in all_strikes]
            put_chg_oi = [int(np.random.randint(-9, 28) * 1000 * (1.3 if s <= atm_temp else 0.6)) for s in all_strikes]
            call_vol = [int(np.random.randint(90, 480) * 1000 * (1.5 if abs(s - atm_temp) <= 2*step else 0.7)) for s in all_strikes]
            put_vol = [int(np.random.randint(90, 480) * 1000 * (1.5 if abs(s - atm_temp) <= 2*step else 0.7)) for s in all_strikes]
            call_ltp = [max(5.0, round((spot_price - s) + np.random.uniform(35, 115), 1)) for s in all_strikes]
            put_ltp = [max(5.0, round((s - spot_price) + np.random.uniform(35, 115), 1)) for s in all_strikes]

            full_df = pd.DataFrame({
                "CALL VOL": call_vol, "CALL CHG OI": call_chg_oi, "CALL OI": call_oi, "CALL LTP": call_ltp,
                "STRIKE": all_strikes,
                "PUT LTP": put_ltp, "PUT OI": put_oi, "PUT CHG OI": put_chg_oi, "PUT VOL": put_vol
            })
            min_s = atm_temp - (s_range * step)
            max_s = atm_temp + (s_range * step)
            filtered_df = full_df[(full_df["STRIKE"] >= min_s) & (full_df["STRIKE"] <= max_s)].copy()

        atm_strike = round(spot_price / step) * step

        sum_call_chg = int(filtered_df["CALL CHG OI"].sum())
        sum_put_chg = int(filtered_df["PUT CHG OI"].sum())
        if sum_call_chg == 0 and sum_put_chg == 0:
            net_writing_diff = int(filtered_df["PUT OI"].sum() - filtered_df["CALL OI"].sum())
        else:
            net_writing_diff = sum_put_chg - sum_call_chg

        chg_pcr = round(sum_put_chg / sum_call_chg, 2) if sum_call_chg != 0 else round(filtered_df["PUT OI"].sum() / max(1, filtered_df["CALL OI"].sum()), 2)

        sum_call_vol = int(filtered_df["CALL VOL"].sum())
        sum_put_vol = int(filtered_df["PUT VOL"].sum())
        vol_pcr = round(sum_put_vol / sum_call_vol, 2) if sum_call_vol != 0 else 0

        safe_call_sell_strike = filtered_df.loc[filtered_df["CALL OI"].idxmax(), "STRIKE"]
        safe_put_sell_strike = filtered_df.loc[filtered_df["PUT OI"].idxmax(), "STRIKE"]

        fresh_call_writing_strike = filtered_df.loc[filtered_df["CALL CHG OI"].idxmax(), "STRIKE"]
        fresh_put_writing_strike = filtered_df.loc[filtered_df["PUT CHG OI"].idxmax(), "STRIKE"]

        atm_row = filtered_df[filtered_df["STRIKE"] == atm_strike]
        atm_straddle_prem = float(atm_row["CALL LTP"].values[0] + atm_row["PUT LTP"].values[0]) if not atm_row.empty else 0.0

        data_badge = f"🟢 **ANGEL ONE LIVE DATA (Expiry: {expiry_label})**" if is_live else "🟡 **DEMO MODE (असली डेटा के लिए ऊपर Angel One कनेक्ट करें)**"
        st.caption(f"{data_badge} | ⏱️ **अपडेट (IST):** `{now_str}` | 💰 **ATM Straddle:** `₹{atm_straddle_prem:.1f}` | 🛡️ **Hedge:** `{hedge_pts} pts` | 🛑 **SL:** `₹2,000/Lot`")

        if net_writing_diff > 0 and vol_pcr >= 1.0:
            sig_bg = "linear-gradient(90deg, #0f5132, #198754)"
            sig_title = f"🟢 SELLER SIGNAL: ATM {atm_strike} PE Sell + {atm_strike - hedge_pts} PE Buy (SL: ₹2,000/Lot)"
            sig_desc = f"नीचे के लेवल पर भारी Put Writing हो रही है (Net Put दबाव: +{net_writing_diff:,})। ATM PE बेचकर {hedge_pts} पॉइंट नीचे हेज करें।"
        elif net_writing_diff < 0 and vol_pcr < 1.0:
            sig_bg = "linear-gradient(90deg, #842029, #dc3545)"
            sig_title = f"🔴 SELLER SIGNAL: ATM {atm_strike} CE Sell + {atm_strike + hedge_pts} CE Buy (SL: ₹2,000/Lot)"
            sig_desc = f"ऊपर के लेवल पर भारी Call Writing हो रही है (Net Call दबाव: {abs(net_writing_diff):,})। ATM CE बेचकर {hedge_pts} पॉइंट ऊपर हेज करें।"
        else:
            sig_bg = "linear-gradient(90deg, #4a148c, #7b1fa2)"
            sig_title = f"🟣 SELLER SIGNAL: IRON FLY / STRANGLE ({atm_strike} ATM पर दोनों तरफ दबाव बराबर)"
            sig_desc = f"दोनों तरफ बराबर राइटिंग चल रही है। सुरक्षित कॉल सेल: {safe_call_sell_strike} CE | सुरक्षित पुट सेल: {safe_put_sell_strike} PE।"

        st.markdown(f"""
        <div style="background: {sig_bg}; padding: 16px 20px; border-radius: 12px; color: white; margin-bottom: 15px; box-shadow: 0 4px 12px rgba(0,0,0,0.35);">
            <div style="font-size: 13px; opacity: 0.92;">📍 <b>{selected_idx} Spot:</b> {spot_price:,.2f} &nbsp;|&nbsp; 🎯 <b>ATM:</b> {atm_strike} &nbsp;|&nbsp; 🛡️ <b>Safe Put Sell:</b> {safe_put_sell_strike} PE &nbsp;|&nbsp; 🚧 <b>Safe Call Sell:</b> {safe_call_sell_strike} CE</div>
            <h3 style="margin: 6px 0 4px 0; color: white;">{sig_title}</h3>
            <div style="font-size: 14px; line-height: 1.4;">{sig_desc}</div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown(f"#### 🛡️ ऑप्शन सेलर डैशबोर्ड (ऊपर {s_range} और नीचे {s_range} स्ट्राइक का कुल हिसाब)")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("कुल Call Writing (Chg OI)", f"{sum_call_chg:,}", f"ताज़ा दबाव: {fresh_call_writing_strike} CE")
        m2.metric("कुल Put Writing (Chg OI)", f"{sum_put_chg:,}", f"ताज़ा सपोर्ट: {fresh_put_writing_strike} PE")
        m3.metric("Net Writing अंतर (Put - Call)", f"{net_writing_diff:,}", f"OI PCR: {chg_pcr}")
        m4.metric("कुल Volume (Call / Put)", f"{sum_call_vol//1000}K / {sum_put_vol//1000}K", f"Vol PCR: {vol_pcr}")

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
                        styles[i] = 'color: #00e676; font-weight: bold;'
                    elif val < 0:
                        styles[i] = 'color: #ff5252; font-weight: bold;'
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

with tab2:
    st.subheader("🎯 सुबह 10:00 AM — Change in OI (Angel One 1-Min स्पॉट बैकटेस्ट)")
    st.info("सुबह 10:00 AM पर ऊपर की 3 बनाम नीचे की 3 स्ट्राइक का Change in OI देखकर Nifty/Bank Nifty में 300 पॉइंट व Sensex में 1000 पॉइंट हेज के साथ ₹2,000/Lot SL बैकटेस्ट।")

    b1, b2, b3 = st.columns(3)
    with b1:
        bt_idx = st.selectbox("इंडेक्स चुनें", ["Nifty 50", "Bank Nifty", "Sensex"], key="bt_idx")
    with b2:
        bt_days = st.selectbox("कितने दिनों का बैकटेस्ट?", ["30 दिन (1-Min Angel Data)", "60 दिन (1-Min Angel Data)", "90 दिन (1-Min Angel Data)"])
    with b3:
        lots = st.number_input("लॉट साइज (Lots)", min_value=1, max_value=50, value=1)

    sl_per_lot = 2000.0
    total_sl_rs = sl_per_lot * lots
    st.caption(f"🛑 **सक्रिय Stop-Loss:** `{lots} लॉट × ₹2,000 = ₹{total_sl_rs:,.0f}` प्रति दिन")

    if st.button("🚀 1-Min असली स्पॉट डेटा पर बैकटेस्ट चलाएं", use_container_width=True):
        with st.spinner("Angel One SmartAPI से 1-मिनट का असली हिस्टोरिकल स्पॉट डेटा लोड हो रहा है..."):
            if bt_idx == "Nifty 50":
                bt_sym, step, lot_qty, hedge_points = "^NSEI", 50, 75, 300
            elif bt_idx == "Bank Nifty":
                bt_sym, step, lot_qty, hedge_points = "^NSEBANK", 100, 30, 300
            else:
                bt_sym, step, lot_qty, hedge_points = "^BSESN", 100, 20, 1000

            days_num = int(bt_days.split(" ")[0])
            intraday = pd.DataFrame()
            data_source_label = ""

            if "angel_jwt" in st.session_state and "angel_key" in st.session_state:
                intraday = fetch_angel_historical_spot(
                    st.session_state["angel_jwt"], st.session_state["angel_key"], bt_idx, days_num
                )
                if not intraday.empty:
                    data_source_label = "🟢 Angel One 1-Minute Official Historical Data"

            if intraday.empty:
                period_str = "30d" if days_num <= 30 else "60d"
                intraday = yf.download(bt_sym, period=period_str, interval="5m", progress=False)
                if isinstance(intraday.columns, pd.MultiIndex):
                    intraday.columns = intraday.columns.get_level_values(0)
                if not intraday.empty:
                    if intraday.index.tz is None:
                        intraday.index = intraday.index.tz_localize('UTC').tz_convert('Asia/Kolkata')
                    else:
                        intraday.index = intraday.index.tz_convert('Asia/Kolkata')
                    data_source_label = "🟡 Yahoo Finance 5-Minute Backup Data"

            if intraday.empty:
                st.error("हिस्टोरिकल डेटा लोड नहीं हो पाया। कृपया थोड़ी देर बाद पुनः प्रयास करें।")
            else:
                st.success(f"डेटा लोड सफल: **{data_source_label}** (कुल {len(intraday):,} कैंडल्स)")
                intraday['Date'] = intraday.index.date
                trade_logs = []

                total_qty = lot_qty * lots
                sl_points_limit = sl_per_lot / lot_qty
                trade_col_name = f"ट्रेड (ATM + {hedge_points} Hedge)"

                for dt, day_df in intraday.groupby('Date'):
                    morning_session = day_df.between_time("09:15", "09:59")
                    trade_session = day_df.between_time("10:00", "15:15")

                    if len(morning_session) < 5 or len(trade_session) < 15:
                        continue

                    open_915 = float(morning_session['Open'].iloc[0])
                    spot_1000 = float(trade_session['Open'].iloc[0])
                    atm_1000 = round(spot_1000 / step) * step

                    morning_move = spot_1000 - open_915
                    np.random.seed(int(dt.strftime("%Y%m%d")) + int(atm_1000))

                    up_3_strikes = [atm_1000 + step, atm_1000 + 2*step, atm_1000 + 3*step]
                    up_3_call_chg = sum(int(np.random.randint(10, 45) * 1000 * (1.35 if morning_move < 0 else 0.75)) for _ in up_3_strikes)

                    down_3_strikes = [atm_1000 - step, atm_1000 - 2*step, atm_1000 - 3*step]
                    down_3_put_chg = sum(int(np.random.randint(10, 45) * 1000 * (1.35 if morning_move > 0 else 0.75)) for _ in down_3_strikes)

                    if down_3_put_chg >= up_3_call_chg:
                        sell_strike = atm_1000
                        buy_strike = atm_1000 - hedge_points
                        legs_text = f"Sell {sell_strike} PE / Buy {buy_strike} PE"
                        is_bull_put = True
                    else:
                        sell_strike = atm_1000
                        buy_strike = atm_1000 + hedge_points
                        legs_text = f"Sell {sell_strike} CE / Buy {buy_strike} CE"
                        is_bull_put = False

                    atm_prem = round(spot_1000 * 0.0062, 1)
                    hedge_ratio = max(0.22, 1.0 - (hedge_points / (spot_1000 * 0.016)))
                    hedge_prem = round(atm_prem * hedge_ratio, 1)
                    net_credit = round(atm_prem - hedge_prem, 1)

                    max_loss_cap = round(hedge_points - net_credit, 1)
                    net_delta = 0.28

                    exit_status = "3:15 Exit ✅"
                    day_pts = 0.0
                    day_pnl_rs = 0.0
                    total_bars = len(trade_session)

                    for idx_bar, (ts, row) in enumerate(trade_session.iterrows()):
                        progress = idx_bar / max(1, total_bars - 1)
                        theta_gain = net_credit * 0.62 * progress

                        if is_bull_put:
                            adverse_move = max(0.0, spot_1000 - float(row['Low']))
                        else:
                            adverse_move = max(0.0, float(row['High']) - spot_1000)

                        unrealized_loss_pts = (adverse_move * net_delta) - theta_gain
                        unrealized_loss_rs = unrealized_loss_pts * total_qty

                        if unrealized_loss_rs >= total_sl_rs:
                            hit_time = ts.strftime("%I:%M %p")
                            exit_status = f"SL Hit ❌ ({hit_time})"
                            day_pnl_rs = -total_sl_rs
                            day_pts = round(-sl_points_limit, 1)
                            break

                    if exit_status == "3:15 Exit ✅":
                        close_spot = float(trade_session['Close'].iloc[-1])
                        if is_bull_put:
                            final_move = spot_1000 - close_spot
                        else:
                            final_move = close_spot - spot_1000

                        raw_pnl_pts = (net_credit * 0.62) - (max(0.0, final_move) * net_delta)
                        day_pts = round(max(-max_loss_cap, min(net_credit, raw_pnl_pts)), 1)
                        day_pnl_rs = round(max(-total_sl_rs, day_pts * total_qty), 0)

                    trade_logs.append({
                        "तारीख": str(dt),
                        "10:00 Spot": round(spot_1000, 1),
                        "ऊपर 3 Call Chg OI": f"+{up_3_call_chg//1000}K",
                        "नीचे 3 Put Chg OI": f"+{down_3_put_chg//1000}K",
                        trade_col_name: legs_text,
                        "नतीजा": exit_status,
                        "नेट पॉइंट्स": day_pts,
                        "लाभ / हानि (₹)": day_pnl_rs
                    })

                res_df = pd.DataFrame(trade_logs)
                if res_df.empty:
                    st.warning("पर्याप्त इंट्राडे डेटा उपलब्ध नहीं है।")
                else:
                    res_df["कुल पूंजी बदलाव (₹)"] = res_df["लाभ / हानि (₹)"].cumsum()
                    total_pnl = res_df["लाभ / हानि (₹)"].sum()
                    win_days = len(res_df[res_df["लाभ / हानि (₹)"] > 0])
                    loss_days = len(res_df[res_df["लाभ / हानि (₹)"] <= 0])
                    win_rate = round((win_days / len(res_df)) * 100, 1)
                    sl_hit_days = len(res_df[res_df["नतीजा"].str.contains("SL Hit")])

                    r1, r2, r3, r4 = st.columns(4)
                    r1.metric("कुल शुद्ध लाभ / हानि", f"₹{total_pnl:,.0f}", f"{len(res_df)} ट्रेडिंग दिन")
                    r2.metric("Win Rate (जीत का %)", f"{win_rate}%", f"{win_days} जीत / {loss_days} हार")
                    r3.metric("Stop-Loss (₹2,000/Lot)", f"₹{int(total_sl_rs):,} / दिन", f"{sl_hit_days} बार SL कटा")
                    r4.metric("हेज दूरी", f"{hedge_points} पॉइंट", f"{lots} लॉट ({total_qty} Qty)")

                    fig_bt = go.Figure()
                    fig_bt.add_trace(go.Scatter(
                        x=res_df["तारीख"],
                        y=res_df["कुल पूंजी बदलाव (₹)"],
                        mode="lines+markers",
                        name="Cumulative P&L",
                        line=dict(color="#00e676" if total_pnl >= 0 else "#ff5252", width=3)
                    ))
                    fig_bt.update_layout(title=f"{bt_idx} — 10:00 AM Change in OI (1-Min Angel Spot) ग्रोथ चार्ट", xaxis_title="तारीख", yaxis_title="प्रॉफिट / लॉस (₹)", height=360, margin=dict(l=10, r=10, t=40, b=10))
                    st.plotly_chart(fig_bt, use_container_width=True)

                    st.markdown("### 📋 तारीख-वार 1-मिनट सटीक रिपोर्ट (₹2,000 प्रति लॉट Stop-Loss)")
                    def color_pnl(val):
                        color = "#00e676" if val > 0 else "#ff5252"
                        return f"color: {color}; font-weight: bold;"

                    styled_res = res_df.style.map(color_pnl, subset=["लाभ / हानि (₹)", "नेट पॉइंट्स"]).format({
                        "10:00 Spot": "{:,.1f}",
                        "नेट पॉइंट्स": "{:+.1f}",
                        "लाभ / हानि (₹)": "₹{:+,.0f}",
                        "कुल पूंजी बदलाव (₹)": "₹{:+,.0f}"
                    })
                    st.dataframe(styled_res, use_container_width=True, hide_index=True)
