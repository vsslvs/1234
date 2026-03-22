"""
Lightweight web dashboard for the Polymarket BTC market maker bot.

Serves a single HTML page with live auto-updating stats.
Runs as a background asyncio task alongside the bot.
"""
import asyncio
import logging
from aiohttp import web

from bot_state import state

log = logging.getLogger(__name__)

DASHBOARD_PORT = 8080

# ── HTML template ────────────────────────────────────────────────────────
HTML_PAGE = """\
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket BTC Bot</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    background: #0d1117; color: #c9d1d9;
    padding: 20px; max-width: 900px; margin: 0 auto;
  }
  h1 { color: #58a6ff; font-size: 1.3em; margin-bottom: 16px; }
  .status-bar {
    display: flex; gap: 12px; align-items: center;
    margin-bottom: 20px; flex-wrap: wrap;
  }
  .badge {
    padding: 4px 10px; border-radius: 12px; font-size: 0.8em; font-weight: 600;
  }
  .badge-green  { background: #1b4332; color: #40c057; }
  .badge-yellow { background: #3d2e00; color: #ffc107; }
  .badge-red    { background: #3d0000; color: #ff6b6b; }
  .badge-blue   { background: #0d2137; color: #58a6ff; }

  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
  @media (max-width: 600px) { .grid { grid-template-columns: 1fr; } }

  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px;
  }
  .card h2 { color: #8b949e; font-size: 0.75em; text-transform: uppercase;
             letter-spacing: 1px; margin-bottom: 10px; }
  .metric { font-size: 1.8em; font-weight: 700; color: #f0f6fc; }
  .metric-sm { font-size: 1.1em; color: #c9d1d9; margin-top: 4px; }
  .sub { font-size: 0.8em; color: #8b949e; margin-top: 4px; }

  .signal-bar {
    height: 8px; background: #21262d; border-radius: 4px; margin-top: 8px;
    overflow: hidden; position: relative;
  }
  .signal-fill {
    height: 100%; border-radius: 4px; transition: width 0.3s;
  }

  table { width: 100%; border-collapse: collapse; font-size: 0.85em; }
  th { text-align: left; color: #8b949e; padding: 8px 6px; border-bottom: 1px solid #30363d;
       font-weight: 600; text-transform: uppercase; font-size: 0.75em; letter-spacing: 1px; }
  td { padding: 6px; border-bottom: 1px solid #21262d; }
  .win  { color: #40c057; }
  .loss { color: #ff6b6b; }
  .pnl-pos { color: #40c057; }
  .pnl-neg { color: #ff6b6b; }

  .paper-banner {
    background: #3d2e00; border: 1px solid #ffc107; border-radius: 8px;
    padding: 10px 16px; margin-bottom: 16px; color: #ffc107;
    font-weight: 600; text-align: center; font-size: 0.9em;
  }

  .progress-ring {
    width: 60px; height: 60px; display: inline-block;
  }
  .footer { text-align: center; color: #484f58; font-size: 0.75em; margin-top: 20px; }
  #conn-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .dot-ok { background: #40c057; }
  .dot-err { background: #ff6b6b; }
</style>
</head>
<body>

<h1>Polymarket BTC 5m Market Maker</h1>

<div id="paper-banner" class="paper-banner" style="display:none;">PAPER TRADING — виртуальный баланс: <span id="paper-bal">--</span> USDC</div>

<div class="status-bar">
  <span id="conn-dot" class="dot-ok"></span>
  <span id="phase-badge" class="badge badge-blue">INIT</span>
  <span id="uptime" class="badge badge-green">0m</span>
  <span id="wallet" style="color:#8b949e; font-size:0.8em;"></span>
</div>

<div class="grid">
  <!-- BTC Price -->
  <div class="card">
    <h2>BTC Price</h2>
    <div class="metric" id="btc-price">--</div>
    <div class="sub">Open: <span id="btc-open">--</span> | Vol: <span id="vol">--</span> bps</div>
  </div>

  <!-- Signal -->
  <div class="card">
    <h2>Signal (P up)</h2>
    <div class="metric" id="p-up">--</div>
    <div class="signal-bar"><div class="signal-fill" id="signal-fill" style="width:50%;background:#58a6ff;"></div></div>
    <div class="sub" style="margin-top:6px;">Fair YES: <span id="fair-yes">--</span> | NO: <span id="fair-no">--</span></div>
  </div>

  <!-- Window -->
  <div class="card">
    <h2>Window</h2>
    <div class="metric" id="countdown">--</div>
    <div class="sub" id="window-times">--</div>
  </div>

  <!-- Orders -->
  <div class="card">
    <h2>Active Orders</h2>
    <div id="orders-display" style="margin-top:4px;">
      <div class="metric-sm">YES: <span id="order-yes">--</span></div>
      <div class="metric-sm">NO: <span id="order-no">--</span></div>
    </div>
  </div>

  <!-- P&L -->
  <div class="card">
    <h2>Session P&L</h2>
    <div class="metric" id="pnl">$0.00</div>
    <div class="sub">Trades: <span id="trade-count">0</span> | Win rate: <span id="win-rate">--</span></div>
  </div>

  <!-- Win/Loss -->
  <div class="card">
    <h2>Win / Loss</h2>
    <div class="metric-sm"><span class="win" id="wins">0</span> W / <span class="loss" id="losses">0</span> L</div>
    <div class="sub">Rolling (50): <span id="rolling-wr">--</span></div>
  </div>
</div>

<!-- Recent trades -->
<div class="card">
  <h2>Recent Trades</h2>
  <table>
    <thead><tr><th>Time</th><th>Side</th><th>Price</th><th>Signal</th><th>Result</th><th>P&L</th></tr></thead>
    <tbody id="trades-body">
      <tr><td colspan="6" style="color:#484f58;">No trades yet</td></tr>
    </tbody>
  </table>
</div>

<div class="footer">Auto-refresh: 1s</div>

<script>
const $ = id => document.getElementById(id);

function fmt(n, d=2) { return n != null ? n.toFixed(d) : '--'; }
function fmtUsd(n) { return n != null ? '$' + n.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}) : '--'; }

const phaseColors = {
  'waiting': 'badge-blue', 'entry': 'badge-yellow',
  'exit': 'badge-red', 'vol_skip': 'badge-red',
  'initializing': 'badge-blue'
};
const phaseLabels = {
  'waiting': 'WAITING', 'entry': 'ENTRY WINDOW',
  'exit': 'EXIT WINDOW', 'vol_skip': 'VOL SKIP',
  'initializing': 'INIT'
};

async function refresh() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();

    $('conn-dot').className = 'dot-ok';

    // Paper trading banner
    if (d.paper_trading) {
      $('paper-banner').style.display = 'block';
      $('paper-bal').textContent = fmt(d.paper_balance, 2);
    } else {
      $('paper-banner').style.display = 'none';
    }

    // Uptime
    const mins = Math.floor(d.uptime_seconds / 60);
    const hrs = Math.floor(mins / 60);
    $('uptime').textContent = hrs > 0 ? hrs + 'h ' + (mins % 60) + 'm' : mins + 'm';
    $('wallet').textContent = d.wallet;

    // Phase
    const pb = $('phase-badge');
    pb.className = 'badge ' + (phaseColors[d.phase] || 'badge-blue');
    pb.textContent = phaseLabels[d.phase] || d.phase.toUpperCase();

    // BTC
    $('btc-price').textContent = fmtUsd(d.btc_price);
    $('btc-open').textContent = fmtUsd(d.btc_open_price);
    $('vol').textContent = fmt(d.candle_vol_bps, 0);

    // Signal
    const pPct = (d.p_up * 100);
    $('p-up').textContent = fmt(pPct, 1) + '%';
    const fill = $('signal-fill');
    fill.style.width = pPct + '%';
    fill.style.background = pPct > 94 ? '#40c057' : pPct < 6 ? '#ff6b6b' : '#58a6ff';
    $('fair-yes').textContent = fmt(d.fair_yes, 4);
    $('fair-no').textContent = fmt(d.fair_no, 4);

    // Window
    $('countdown').textContent = fmt(d.seconds_to_close, 0) + 's';
    if (d.window_start) {
      const s = new Date(d.window_start * 1000).toLocaleTimeString();
      const e = new Date(d.window_end * 1000).toLocaleTimeString();
      $('window-times').textContent = s + ' → ' + e;
    }

    // Orders
    $('order-yes').textContent = d.yes_order_active ? fmt(d.yes_order_price, 4) : 'none';
    $('order-yes').style.color = d.yes_order_active ? '#40c057' : '#484f58';
    $('order-no').textContent = d.no_order_active ? fmt(d.no_order_price, 4) : 'none';
    $('order-no').style.color = d.no_order_active ? '#40c057' : '#484f58';

    // P&L
    const pnlEl = $('pnl');
    pnlEl.textContent = (d.total_pnl >= 0 ? '+' : '') + fmtUsd(d.total_pnl);
    pnlEl.className = 'metric ' + (d.total_pnl >= 0 ? 'pnl-pos' : 'pnl-neg');
    $('trade-count').textContent = d.total_trades;
    $('win-rate').textContent = d.total_trades > 0 ? fmt(d.win_rate, 1) + '%' : '--';

    // Win/Loss
    $('wins').textContent = d.wins;
    $('losses').textContent = d.losses;
    $('rolling-wr').textContent = d.total_trades > 0 ? fmt(d.rolling_win_rate, 1) + '%' : '--';

    // Trades table
    const tbody = $('trades-body');
    if (d.recent_trades && d.recent_trades.length > 0) {
      tbody.innerHTML = d.recent_trades.slice().reverse().map(t => {
        const tm = new Date(t.time * 1000).toLocaleTimeString();
        const cls = t.result === 'WIN' ? 'win' : 'loss';
        const pnlCls = t.pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
        return `<tr>
          <td>${tm}</td><td>${t.side}</td><td>${fmt(t.price,4)}</td>
          <td>${t.signal}%</td><td class="${cls}">${t.result}</td>
          <td class="${pnlCls}">${t.pnl >= 0 ? '+' : ''}${fmt(t.pnl,2)}</td>
        </tr>`;
      }).join('');
    }
  } catch(e) {
    $('conn-dot').className = 'dot-err';
  }
}

setInterval(refresh, 1000);
refresh();
</script>
</body>
</html>
"""


# ── API handlers ─────────────────────────────────────────────────────────

async def handle_index(request):
    return web.Response(text=HTML_PAGE, content_type="text/html")


async def handle_api_state(request):
    return web.json_response(state.to_dict())


# ── Start / stop ─────────────────────────────────────────────────────────

async def start_dashboard(port: int = DASHBOARD_PORT) -> web.AppRunner:
    """Start the dashboard as a background aiohttp server. Returns the runner for cleanup."""
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_api_state)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    log.info("Dashboard running at http://127.0.0.1:%d", port)
    return runner
