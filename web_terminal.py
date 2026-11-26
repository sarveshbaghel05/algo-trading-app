import os
import json
from datetime import datetime
import pytz
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from oi_base import (
    ensure_angel_session,
    get_top_tickers_snapshot,
    load_algo_settings,
    gh_read_json,
    get_live_expiry_info,
)

app = FastAPI(title="Algo Trading Smooth Terminal")


def _build_mtm_summary():
    """सक्रिय ट्रेड्स और सेटिंग्स का सुरक्षित MTM और स्टेटस निकालता है।"""
    cfg = load_algo_settings()
    active_multi = gh_read_json("active_trades_multi.json", {})
    ema_active = gh_read_json("ema16_oi_multi_active.json", {})
    pro_active = gh_read_json("pro_multi_active.json", {})

    trips = []
    total_mtm = 0.0

    def _collect(source_dict, label_prefix):
        nonlocal total_mtm
        if not isinstance(source_dict, dict):
            return
        for k, v in source_dict.items():
            if not isinstance(v, dict):
                continue
            status = str(v.get("status", "OPEN")).upper()
            if status in ["CLOSED", "EXITED", "DONE"]:
                continue
            mtm_val = float(v.get("mtm", v.get("pnl", v.get("unrealized_pnl", 0.0))) or 0.0)
            total_mtm += mtm_val
            trips.append({
                "id": f"{label_prefix} • {k}",
                "strategy": label_prefix,
                "index": v.get("index", v.get("selected_idx", "Nifty 50")),
                "mode": v.get("mode", "PAPER"),
                "mtm": round(mtm_val, 2),
                "sl": v.get("sl", -1700),
                "tsl": v.get("tsl_active", False),
            })

    _collect(active_multi, "10AM / Main")
    _collect(ema_active, "16 EMA")
    _collect(pro_active, "4-Layer Pro")

    return {
        "total_mtm": round(total_mtm, 2),
        "open_count": len(trips),
        "trips": trips,
        "settings": {
            "main_auto_10am": bool(cfg.get("main_auto_10am", False)),
            "ema_auto_arm": bool(cfg.get("ema_auto_arm", False)),
            "pro_auto": bool(cfg.get("pro_auto", False)),
            "main_lots": int(cfg.get("main_lots", 1) or 1),
            "ema_lots": int(cfg.get("ema_lots", 1) or 1),
            "pro_lots": int(cfg.get("pro_lots", 1) or 1),
        },
    }


@app.get("/health")
def health():
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    jwt_tok, _ = ensure_angel_session()
    return {
        "ok": True,
        "time_ist": now_ist.strftime("%d-%b-%Y %H:%M:%S"),
        "angel_connected": bool(jwt_tok),
    }


@app.get("/api/snapshot")
def api_snapshot():
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    tickers = get_top_tickers_snapshot()
    mtm_data = _build_mtm_summary()

    expiries = {}
    for idx_name in ["Nifty 50", "Sensex", "Bank Nifty"]:
        exp_str, dte, _ = get_live_expiry_info(idx_name)
        expiries[idx_name] = {"expiry": exp_str, "dte": dte}

    return JSONResponse({
        "time_ist": now_ist.strftime("%H:%M:%S"),
        "date_ist": now_ist.strftime("%d %b %Y"),
        "tickers": tickers,
        "expiries": expiries,
        "mtm": mtm_data,
    })


HTML_TERMINAL = """<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
<meta name="theme-color" content="#0b0f17" />
<title>Algo Terminal — Live</title>
<style>
  :root {
    --bg: #0b0f17;
    --card: #121826;
    --card-2: #182032;
    --border: #243049;
    --text: #e6edf7;
    --muted: #8b9bb4;
    --green: #10b981;
    --red: #ef4444;
    --accent: #3b82f6;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  body { background: var(--bg); color: var(--text); min-height: 100vh; padding-bottom: 32px; }

  /* Top Ticker Strip: Nifty 50, Sensex, Bank Nifty, India VIX */
  .ticker-wrap {
    position: sticky; top: 0; z-index: 50;
    background: rgba(11, 15, 23, 0.94); backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--border);
    padding: 8px 10px;
  }
  .ticker-grid {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px;
  }
  .tick-card {
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 7px 8px; cursor: pointer; transition: border-color 0.2s ease, transform 0.12s ease;
    text-align: left; color: var(--text);
  }
  .tick-card.active { border-color: var(--accent); background: var(--card-2); }
  .tick-name { font-size: 10px; color: var(--muted); font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tick-val { font-size: 13px; font-weight: 700; margin-top: 2px; font-variant-numeric: tabular-nums; }
  .tick-chg { font-size: 10px; font-weight: 600; margin-top: 1px; font-variant-numeric: tabular-nums; }
  .up { color: var(--green); }
  .down { color: var(--red); }

  .container { max-width: 1120px; margin: 12px auto 0; padding: 0 12px; }

  /* Header & Total MTM Banner */
  .hero-bar {
    display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px;
    background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 14px;
  }
  .hero-title { font-size: 16px; font-weight: 700; }
  .hero-sub { font-size: 12px; color: var(--muted); margin-top: 3px; }
  .mtm-box { text-align: right; }
  .mtm-label { font-size: 11px; color: var(--muted); }
  .mtm-val { font-size: 22px; font-weight: 800; font-variant-numeric: tabular-nums; margin-top: 2px; }

  /* Tabs without full page reload */
  .tabs {
    display: flex; gap: 8px; margin-top: 12px; overflow-x: auto; padding-bottom: 4px;
  }
  .tab-btn {
    background: var(--card); border: 1px solid var(--border); color: var(--muted);
    padding: 8px 14px; border-radius: 999px; font-size: 12px; font-weight: 600;
    cursor: pointer; white-space: nowrap;
  }
  .tab-btn.active { background: var(--accent); border-color: var(--accent); color: #fff; }

  .panel { display: none; margin-top: 12px; }
  .panel.active { display: block; }

  .cards-3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px;
  }
  .card-h { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .card-big { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .badge {
    display: inline-block; padding: 3px 8px; border-radius: 999px; font-size: 11px; font-weight: 700;
    background: rgba(59, 130, 246, 0.16); color: #93c5fd; margin-top: 6px;
  }
  .badge.on { background: rgba(16, 185, 129, 0.16); color: #6ee7b7; }

  .trip-list { margin-top: 10px; display: flex; flex-direction: column; gap: 8px; }
  .trip-item {
    display: flex; justify-content: space-between; align-items: center;
    background: var(--card-2); border: 1px solid var(--border); border-radius: 10px; padding: 11px 12px;
  }
  .trip-title { font-size: 13px; font-weight: 700; }
  .trip-meta { font-size: 11px; color: var(--muted); margin-top: 2px; }
  .trip-pnl { font-size: 15px; font-weight: 800; font-variant-numeric: tabular-nums; }
</style>
</head>
<body>

  <!-- चारों टिकर: Nifty 50, Sensex, Bank Nifty, India VIX -->
  <div class="ticker-wrap">
    <div class="ticker-grid">
      <button class="tick-card active" id="tick-Nifty 50" onclick="selectIndex('Nifty 50')">
        <div class="tick-name">NIFTY 50</div>
        <div class="tick-val" id="val-Nifty 50">--</div>
        <div class="tick-chg" id="chg-Nifty 50">0.00 (0.00%)</div>
      </button>
      <button class="tick-card" id="tick-Sensex" onclick="selectIndex('Sensex')">
        <div class="tick-name">SENSEX</div>
        <div class="tick-val" id="val-Sensex">--</div>
        <div class="tick-chg" id="chg-Sensex">0.00 (0.00%)</div>
      </button>
      <button class="tick-card" id="tick-Bank Nifty" onclick="selectIndex('Bank Nifty')">
        <div class="tick-name">BANK NIFTY</div>
        <div class="tick-val" id="val-Bank Nifty">--</div>
        <div class="tick-chg" id="chg-Bank Nifty">0.00 (0.00%)</div>
      </button>
      <button class="tick-card" id="tick-India VIX" onclick="selectIndex('India VIX')">
        <div class="tick-name">INDIA VIX</div>
        <div class="tick-val" id="val-India VIX">--</div>
        <div class="tick-chg" id="chg-India VIX">0.00 (0.00%)</div>
      </button>
    </div>
  </div>

  <div class="container">
    <div class="hero-bar">
      <div>
        <div class="hero-title" id="active-idx-title">Nifty 50 • Live Terminal</div>
        <div class="hero-sub" id="clock-line">IST: --:--:-- | Expiry: --</div>
      </div>
      <div class="mtm-box">
        <div class="mtm-label">कुल MTM (ALL ACTIVE)</div>
        <div class="mtm-val" id="total-mtm">₹0.00</div>
      </div>
    </div>

    <div class="tabs">
      <button class="tab-btn active" onclick="switchTab('overview', this)">Overview & MTM</button>
      <button class="tab-btn" onclick="switchTab('algos', this)">3 Strategies Status</button>
      <button class="tab-btn" onclick="switchTab('risk', this)">SL / Hedge Rules</button>
    </div>

    <!-- Tab 1: Overview -->
    <div class="panel active" id="panel-overview">
      <div class="cards-3">
        <div class="card">
          <div class="card-h">खुली पोजीशन / ट्रिप</div>
          <div class="card-big" id="open-count">0</div>
          <div class="badge" id="expiry-badge">DTE: --</div>
        </div>
        <div class="card">
          <div class="card-h">लाइव अपडेट स्थिति</div>
          <div class="card-big" style="font-size:15px;">बिना पेज रिफ्रेश के चालू</div>
          <div class="badge on" id="last-sync-badge">Sync: --:--:--</div>
        </div>
      </div>

      <div class="card" style="margin-top:10px;">
        <div class="card-h">सक्रिय ट्रेड्स / ट्रिप MTM</div>
        <div class="trip-list" id="trip-list">
          <div class="trip-meta">अभी कोई खुला ट्रेड नहीं दिख रहा है।</div>
        </div>
      </div>
    </div>

    <!-- Tab 2: 3 Strategies -->
    <div class="panel" id="panel-algos">
      <div class="cards-3">
        <div class="card">
          <div class="card-h">10:00 AM Auto-Trade</div>
          <div class="card-big" id="st-main">OFF</div>
          <div class="badge" id="lots-main">Lots: 1</div>
        </div>
        <div class="card">
          <div class="card-h">16 EMA + OI (09:45–12:00)</div>
          <div class="card-big" id="st-ema">OFF</div>
          <div class="badge" id="lots-ema">Lots: 1</div>
        </div>
        <div class="card">
          <div class="card-h">4-Layer Pro</div>
          <div class="card-big" id="st-pro">OFF</div>
          <div class="badge" id="lots-pro">Lots: 1</div>
        </div>
      </div>
    </div>

    <!-- Tab 3: Risk Rules -->
    <div class="panel" id="panel-risk">
      <div class="cards-3">
        <div class="card">
          <div class="card-h">Stop-Loss & Trailing SL</div>
          <div class="card-big">-₹1,700 / Lot</div>
          <div class="badge on">₹1,200 Profit → TSL +₹200</div>
        </div>
        <div class="card">
          <div class="card-h">Hedge Distance</div>
          <div class="card-big" style="font-size:15px;">Nifty 500 • Bank Nifty 1000 • Sensex 1500</div>
          <div class="badge">Hedged Option Selling</div>
        </div>
      </div>
    </div>
  </div>

<script>
  let selectedIdx = "Nifty 50";

  function selectIndex(idxName) {
    selectedIdx = idxName;
    document.querySelectorAll(".tick-card").forEach(el => el.classList.remove("active"));
    const activeCard = document.getElementById("tick-" + idxName);
    if (activeCard) activeCard.classList.add("active");
    document.getElementById("active-idx-title").textContent = idxName + " • Live Terminal";
    updateExpiryLine();
  }

  function switchTab(tabName, btnEl) {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    if (btnEl) btnEl.classList.add("active");
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    const target = document.getElementById("panel-" + tabName);
    if (target) target.classList.add("active");
  }

  let latestExpiries = {};

  function updateExpiryLine() {
    const info = latestExpiries[selectedIdx] || { expiry: "--", dte: "--" };
    const clk = document.getElementById("clock-line").dataset.timeStr || "--:--:--";
    document.getElementById("clock-line").textContent = `IST: ${clk} | Expiry: ${info.expiry}`;
    document.getElementById("expiry-badge").textContent = `DTE: ${info.dte}`;
  }

  function formatINR(num) {
    const n = Number(num || 0);
    const sign = n > 0 ? "+" : (n < 0 ? "-" : "");
    return sign + "₹" + Math.abs(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  async function refreshData() {
    try {
      const res = await fetch("/api/snapshot", { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();

      // Update 4 tickers smoothly in place (no full page reload)
      const tickers = data.tickers || {};
      for (const [name, obj] of Object.entries(tickers)) {
        const valEl = document.getElementById("val-" + name);
        const chgEl = document.getElementById("chg-" + name);
        if (!valEl || !chgEl) continue;
        const ltp = Number(obj.ltp || 0);
        const chg = Number(obj.chg || 0);
        const pct = Number(obj.pct || 0);
        valEl.textContent = ltp > 0 ? ltp.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "--";
        const cls = chg >= 0 ? "up" : "down";
        const prefix = chg > 0 ? "+" : "";
        chgEl.className = "tick-chg " + cls;
        chgEl.textContent = `${prefix}${chg.toFixed(2)} (${prefix}${pct.toFixed(2)}%)`;
      }

      latestExpiries = data.expiries || {};
      document.getElementById("clock-line").dataset.timeStr = data.time_ist || "--:--:--";
      updateExpiryLine();
      document.getElementById("last-sync-badge").textContent = "Sync: " + (data.time_ist || "--:--:--");

      // Update MTM
      const mtmObj = data.mtm || { total_mtm: 0, open_count: 0, trips: [], settings: {} };
      const totMtm = Number(mtmObj.total_mtm || 0);
      const mtmEl = document.getElementById("total-mtm");
      mtmEl.textContent = formatINR(totMtm);
      mtmEl.className = "mtm-val " + (totMtm > 0 ? "up" : (totMtm < 0 ? "down" : ""));
      document.getElementById("open-count").textContent = String(mtmObj.open_count || 0);

      // Strategy statuses
      const st = mtmObj.settings || {};
      document.getElementById("st-main").textContent = st.main_auto_10am ? "ARMED / ON" : "OFF";
      document.getElementById("st-ema").textContent = st.ema_auto_arm ? "ARMED / ON" : "OFF";
      document.getElementById("st-pro").textContent = st.pro_auto ? "ARMED / ON" : "OFF";
      document.getElementById("lots-main").textContent = "Lots: " + (st.main_lots || 1);
      document.getElementById("lots-ema").textContent = "Lots: " + (st.ema_lots || 1);
      document.getElementById("lots-pro").textContent = "Lots: " + (st.pro_lots || 1);

      // Trips list
      const listEl = document.getElementById("trip-list");
      const trips = mtmObj.trips || [];
      if (!trips.length) {
        listEl.innerHTML = '<div class="trip-meta">अभी कोई खुला ट्रेड नहीं दिख रहा है।</div>';
      } else {
        listEl.innerHTML = trips.map(t => {
          const pnl = Number(t.mtm || 0);
          const cls = pnl > 0 ? "up" : (pnl < 0 ? "down" : "");
          return `<div class="trip-item">
            <div>
              <div class="trip-title">${t.id}</div>
              <div class="trip-meta">${t.index} • ${t.mode} • SL ${t.sl}</div>
            </div>
            <div class="trip-pnl ${cls}">${formatINR(pnl)}</div>
          </div>`;
        }).join("");
      }
    } catch (e) {
      // silent retry on next tick
    }
  }

  refreshData();
  setInterval(refreshData, 4000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index_page():
    return HTMLResponse(content=HTML_TERMINAL)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    uvicorn.run("web_terminal:app", host="0.0.0.0", port=port, log_level="warning")
