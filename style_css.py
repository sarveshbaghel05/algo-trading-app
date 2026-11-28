import streamlit as st

def get_custom_css():
    return """
    <style>
    /* Global Theme - Black/Dark Screen */
    :root {
        --bg-color: #0b1120;
        --card-bg: #111827;
        --card-border: #1f2937;
        --text-primary: #f3f4f6;
        --text-secondary: #9ca3af;
        --green-angel: #10b981;
        --red-angel: #ef4444;
        --yellow-angel: #facc15;
    }

    /* Main Container Background */
    .stApp {
        background-color: var(--bg-color);
        color: var(--text-primary);
    }

    /* Top Bar (Title & Time) */
    header[data-testid="stHeader"] {
        background-color: transparent;
    }
    [data-testid="stToolbar"] {
        right: 2rem;
    }
    .main .block-container {
        padding-top: 1rem;
        padding-bottom: 2rem;
    }

    /* Custom Cards for Tickers */
    .ticker-card {
        background-color: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: 12px;
        padding: 15px;
        color: var(--text-primary);
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2), 0 2px 4px -1px rgba(0, 0, 0, 0.1);
        margin-bottom: 10px;
    }
    .ticker-title { font-size: 14px; color: var(--text-secondary); margin-bottom: 4px; }
    .ticker-value { font-size: 20px; font-weight: bold; }
    .ticker-change-up { font-size: 14px; color: var(--green-angel); }
    .ticker-change-down { font-size: 14px; color: var(--red-angel); }

    /* Position/Algo Cards */
    .algo-card {
        background-color: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: 12px;
        padding: 20px;
        margin-bottom: 15px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3);
    }
    .algo-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        border-bottom: 1px solid var(--card-border);
        padding-bottom: 12px;
        margin-bottom: 12px;
    }
    .algo-title { font-size: 16px; font-weight: bold; color: var(--text-primary); }
    .mtm-positive { font-size: 22px; font-weight: bold; color: var(--green-angel); }
    .mtm-negative { font-size: 22px; font-weight: bold; color: var(--red-angel); }
    .algo-details { font-size: 14px; color: var(--text-secondary); line-height: 1.6; }
    .info-pill {
        background-color: var(--bg-color);
        border: 1px solid var(--card-border);
        border-radius: 10px;
        padding: 4px 8px;
        font-size: 12px;
        color: var(--text-secondary);
        margin-right: 5px;
    }

    /* Streamlit elements styling in dark mode */
    div.stButton > button {
        background-color: var(--card-bg);
        color: var(--text-primary);
        border: 1px solid var(--card-border);
        border-radius: 8px;
    }
    div.stButton > button:hover {
        border-color: var(--yellow-angel);
        color: var(--yellow-angel);
    }
    div.stSelectbox > label, div.stNumberInput > label, div.stTextInput > label {
        color: var(--text-secondary) !important;
    }
    div[data-testid="stMetricValue"] { color: var(--text-primary); }
    div[data-testid="stMetricLabel"] { color: var(--text-secondary); }

    /* Divider */
    hr { border-top: 1px solid var(--card-border); margin: 1em 0; }

    </style>
    """

def render_ticker_card(symbol, ltp, change, percent, is_up):
    change_class = "ticker-change-up" if is_up else "ticker-change-down"
    arrow = "▲" if is_up else "▼"
    st.markdown(f"""
    <div class="ticker-card">
        <div class="ticker-title">{symbol}</div>
        <div class="ticker-value">{ltp:,.2f}</div>
        <div class="{change_class}">{arrow} {change:,.2f} ({percent:,.2f}%)</div>
    </div>
    """, unsafe_allow_html=True)

def render_position_card(index_name, strategy_name, mtm, sl_val, qty, is_active_main=False):
    mtm_class = "mtm-positive" if mtm >= 0 else "mtm-negative"
    mtm_sign = "+" if mtm >= 0 else ""
    
    # Main Algo tag (if applicable)
    main_tag = '<span class="info-pill" style="background-color:#166534; color:#86efac;">🔥 Main</span>' if is_active_main else ''

    card_html = f"""
    <div class="algo-card">
        <div class="algo-header">
            <div class="algo-title">{main_tag} {index_name} ({strategy_name})</div>
            <div class="algo-mtm">
                <span class="{mtm_class}">{mtm_sign}{mtm:,.0f}</span>
            </div>
        </div>
        <div class="algo-details">
            <span class="info-pill">SL/TSL: {sl_val}</span>
            <span class="info-pill">Qty: {qty}</span>
        </div>
    </div>
    """
    st.markdown(card_html, unsafe_allow_html=True)
    
def render_section_header(title, subtitle=""):
    st.markdown(f"""
    <h2 style="color: var(--text-primary); margin-top: 1.5rem; margin-bottom: 0.5rem; font-size: 24px;">{title}</h2>
    {f'<p style="color: var(--text-secondary); font-size: 16px; margin-bottom: 1rem;">{subtitle}</p>' if subtitle else ''}
    """, unsafe_allow_html=True)

def render_compact_entry_card(setup_reason, oi_bias, call_chg, put_chg, setup_direction, setup_summary, hedge_pts, net_cr):
    bias_color = "#10b981" if oi_bias == "BULLISH" else ("#ef4444" if oi_bias == "BEARISH" else "#facc15")
    
    st.markdown(f"""
    <div style="background-color: var(--card-bg); padding:16px 20px; border-radius:12px; border-left:6px solid {bias_color}; color:white; margin-bottom:16px;">
        <div style="font-size:14px; color:#e2e8f0;">📊 <b>ATM±3 Change in OI:</b>  <span style="color:{bias_color}; font-weight:bold;"> {oi_bias}</span> | नीचे 3 Put: {put_chg:+,} vs ऊपर 3 Call: {call_chg:+,} </div>
        <div style="font-size:17px; font-weight:bold; color:#fde047; margin:8px 0;">{setup_reason}</div>
        <div style="font-size:14px;">🎯 <b>40-Delta सेटअप ({setup_direction}):</b> {setup_summary} | 🛡️ हेज: <b>{hedge_pts} pts</b> | नेट क्रेडिट: <b>₹{net_cr:,.0f}</b></div>
    </div>
    """, unsafe_allow_html=True)

