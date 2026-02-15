"""
Web dashboard for the Polymarket arbitrage bot.

Runs a lightweight HTTP server that serves a single-page dashboard
with Server-Sent Events (SSE) for live updates. No external dependencies.

Usage:
    python bot.py dashboard          # bot + web UI on :8080
    python bot.py dashboard 9090     # custom port
"""

import json
import queue
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Shared state – written by bot thread, read by HTTP handlers
# ---------------------------------------------------------------------------

@dataclass
class DashboardState:
    """Thread-safe shared state between bot and web server."""

    # Config (set once at startup)
    config: dict = field(default_factory=dict)

    # Live scanning
    status: str = "starting"          # starting | scanning | waiting | trading | resolved | stopped
    current_market: str = ""
    market_asset: str = ""
    market_duration: str = ""
    market_ends: str = ""
    markets_found: int = 0
    windows_scanned: int = 0

    # Order book
    up_best_ask: float = 0.0
    up_best_bid: float = 0.0
    down_best_ask: float = 0.0
    down_best_bid: float = 0.0
    combined_ask: float = 0.0
    up_depth: float = 0.0
    down_depth: float = 0.0
    fee_bps: int = 0

    # Capital
    bankroll: float = 0.0
    deployed: float = 0.0
    available: float = 0.0
    realized_pnl: float = 0.0
    daily_spent: float = 0.0
    exchange_balance: float | None = None

    # Strategy
    opportunities_seen: int = 0
    opportunities_traded: int = 0
    last_plan: dict | None = None

    # Execution
    fill_rate: str = ""
    api_stats: str = ""

    # Positions (list of dicts for JSON)
    positions: list[dict] = field(default_factory=list)

    # Log lines (rolling buffer)
    _log_lines: list[str] = field(default_factory=list)
    _max_log_lines: int = 500
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # SSE subscribers
    _subscribers: list[queue.Queue] = field(default_factory=list)

    def add_log(self, line: str):
        with self._lock:
            self._log_lines.append(line)
            if len(self._log_lines) > self._max_log_lines:
                self._log_lines = self._log_lines[-self._max_log_lines:]
        self._send_event("log", line)

    def get_logs(self, last_n: int = 200) -> list[str]:
        with self._lock:
            return list(self._log_lines[-last_n:])

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "current_market": self.current_market,
            "market_asset": self.market_asset,
            "market_duration": self.market_duration,
            "market_ends": self.market_ends,
            "markets_found": self.markets_found,
            "windows_scanned": self.windows_scanned,
            "up_best_ask": self.up_best_ask,
            "up_best_bid": self.up_best_bid,
            "down_best_ask": self.down_best_ask,
            "down_best_bid": self.down_best_bid,
            "combined_ask": self.combined_ask,
            "up_depth": self.up_depth,
            "down_depth": self.down_depth,
            "fee_bps": self.fee_bps,
            "bankroll": self.bankroll,
            "deployed": self.deployed,
            "available": self.available,
            "realized_pnl": self.realized_pnl,
            "daily_spent": self.daily_spent,
            "exchange_balance": self.exchange_balance,
            "opportunities_seen": self.opportunities_seen,
            "opportunities_traded": self.opportunities_traded,
            "last_plan": self.last_plan,
            "fill_rate": self.fill_rate,
            "api_stats": self.api_stats,
            "positions": self.positions,
            "config": self.config,
        }

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=50)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _notify(self):
        self._send_event("state", json.dumps(self.to_dict()))

    def _send_event(self, event_type: str, data: str):
        msg = (event_type, data)
        dead = []
        with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)


# Global instance
state = DashboardState()


# ---------------------------------------------------------------------------
# Log handler that feeds the dashboard
# ---------------------------------------------------------------------------

import logging

class DashboardLogHandler(logging.Handler):
    """Captures log records into DashboardState."""

    def __init__(self, dash_state: DashboardState):
        super().__init__()
        self.dash_state = dash_state

    def emit(self, record):
        try:
            msg = self.format(record)
            self.dash_state.add_log(msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket Arb Dashboard</title>
<style>
  :root {
    --bg: #0a0e17; --surface: #111827; --border: #1e293b;
    --text: #e2e8f0; --dim: #64748b; --accent: #38bdf8;
    --green: #22c55e; --red: #ef4444; --yellow: #eab308; --orange: #f97316;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
    background: var(--bg); color: var(--text); font-size: 13px;
    line-height: 1.5;
  }
  header {
    background: var(--surface); border-bottom: 1px solid var(--border);
    padding: 12px 20px; display: flex; align-items: center; gap: 16px;
  }
  header h1 { font-size: 15px; font-weight: 600; }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%;
    display: inline-block; margin-right: 6px;
  }
  .status-dot.scanning { background: var(--accent); animation: pulse 1.5s infinite; }
  .status-dot.trading { background: var(--green); animation: pulse 0.8s infinite; }
  .status-dot.waiting { background: var(--yellow); }
  .status-dot.stopped { background: var(--red); }
  .status-dot.starting { background: var(--dim); animation: pulse 2s infinite; }
  .status-dot.resolved { background: var(--green); }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }

  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    grid-template-rows: auto auto 1fr;
    gap: 1px; background: var(--border);
    height: calc(100vh - 49px);
  }
  .panel {
    background: var(--surface); padding: 12px 16px; overflow: hidden;
    display: flex; flex-direction: column;
  }
  .panel-title {
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--dim); margin-bottom: 8px; font-weight: 600;
  }

  /* Specific panels */
  .panel-market { grid-column: 1; }
  .panel-book { grid-column: 2; }
  .panel-capital { grid-column: 3; }
  .panel-strategy { grid-column: 1; }
  .panel-positions { grid-column: 2; }
  .panel-stats { grid-column: 3; }
  .panel-log { grid-column: 1 / -1; overflow: hidden; }

  .kv { display: flex; justify-content: space-between; padding: 2px 0; }
  .kv .label { color: var(--dim); }
  .kv .value { font-weight: 500; text-align: right; }
  .kv .value.green { color: var(--green); }
  .kv .value.red { color: var(--red); }
  .kv .value.accent { color: var(--accent); }
  .kv .value.yellow { color: var(--yellow); }

  .book-row {
    display: grid; grid-template-columns: 1fr auto 1fr; gap: 8px;
    padding: 2px 0; text-align: center;
  }
  .book-row .bid { color: var(--green); text-align: right; }
  .book-row .ask { color: var(--red); text-align: left; }
  .book-row .label { color: var(--dim); }

  .combined-bar {
    margin-top: 8px; padding: 6px 10px; border-radius: 4px;
    text-align: center; font-weight: 700; font-size: 16px;
  }
  .combined-bar.arb { background: rgba(34,197,94,0.15); color: var(--green); }
  .combined-bar.no-arb { background: rgba(239,68,68,0.1); color: var(--red); }

  .log-container {
    flex: 1; overflow-y: auto; font-size: 12px;
    scrollbar-width: thin; scrollbar-color: var(--border) transparent;
  }
  .log-container::-webkit-scrollbar { width: 6px; }
  .log-container::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  .log-line { padding: 1px 0; white-space: pre-wrap; word-break: break-all; }
  .log-line.error { color: var(--red); }
  .log-line.warning { color: var(--yellow); }
  .log-line.arb { color: var(--green); font-weight: 600; }

  .pos-table { width: 100%; font-size: 12px; }
  .pos-table th {
    text-align: left; color: var(--dim); font-weight: 500;
    padding: 2px 4px; border-bottom: 1px solid var(--border);
  }
  .pos-table td { padding: 2px 4px; }

  .tag {
    display: inline-block; padding: 1px 6px; border-radius: 3px;
    font-size: 11px; font-weight: 600;
  }
  .tag.maker { background: rgba(56,189,248,0.15); color: var(--accent); }
  .tag.taker { background: rgba(249,115,22,0.15); color: var(--orange); }

  @media (max-width: 900px) {
    .grid { grid-template-columns: 1fr 1fr; }
    .panel-log { grid-column: 1 / -1; }
  }
  @media (max-width: 600px) {
    .grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<header>
  <span class="status-dot starting" id="statusDot"></span>
  <h1>Polymarket Arb Bot</h1>
  <span id="statusText" style="color: var(--dim)">connecting...</span>
  <span style="flex:1"></span>
  <span id="clock" style="color: var(--dim)"></span>
</header>

<div class="grid">
  <!-- Row 1 -->
  <div class="panel panel-market">
    <div class="panel-title">Current Market</div>
    <div class="kv"><span class="label">Market</span><span class="value" id="mTitle">--</span></div>
    <div class="kv"><span class="label">Asset</span><span class="value" id="mAsset">--</span></div>
    <div class="kv"><span class="label">Duration</span><span class="value" id="mDuration">--</span></div>
    <div class="kv"><span class="label">Ends</span><span class="value" id="mEnds">--</span></div>
    <div class="kv"><span class="label">Fee</span><span class="value" id="mFee">--</span></div>
    <div class="kv"><span class="label">Markets found</span><span class="value" id="mFound">0</span></div>
    <div class="kv"><span class="label">Windows scanned</span><span class="value" id="mScanned">0</span></div>
  </div>

  <div class="panel panel-book">
    <div class="panel-title">Order Book</div>
    <div class="book-row">
      <span class="bid">Bid</span><span class="label"></span><span class="ask">Ask</span>
    </div>
    <div class="book-row">
      <span class="bid" id="upBid">--</span>
      <span class="label">UP</span>
      <span class="ask" id="upAsk">--</span>
    </div>
    <div class="book-row">
      <span class="bid" id="downBid">--</span>
      <span class="label">DOWN</span>
      <span class="ask" id="downAsk">--</span>
    </div>
    <div class="kv" style="margin-top:4px">
      <span class="label">Up depth</span><span class="value" id="upDepth">--</span>
    </div>
    <div class="kv">
      <span class="label">Down depth</span><span class="value" id="downDepth">--</span>
    </div>
    <div class="combined-bar no-arb" id="combinedBar">Combined: --</div>
  </div>

  <div class="panel panel-capital">
    <div class="panel-title">Capital</div>
    <div class="kv"><span class="label">Bankroll</span><span class="value" id="cBank">--</span></div>
    <div class="kv"><span class="label">Deployed</span><span class="value" id="cDeployed">--</span></div>
    <div class="kv"><span class="label">Available</span><span class="value accent" id="cAvail">--</span></div>
    <div class="kv"><span class="label">Exchange bal.</span><span class="value" id="cExchange">--</span></div>
    <div class="kv"><span class="label">Realized P&L</span><span class="value" id="cPnl">--</span></div>
    <div class="kv"><span class="label">Daily spent</span><span class="value" id="cDaily">--</span></div>
  </div>

  <!-- Row 2 -->
  <div class="panel panel-strategy">
    <div class="panel-title">Strategy</div>
    <div class="kv"><span class="label">Mode</span><span class="value" id="sMode">--</span></div>
    <div class="kv"><span class="label">Max combined</span><span class="value" id="sMaxComb">--</span></div>
    <div class="kv"><span class="label">Min margin</span><span class="value" id="sMinMargin">--</span></div>
    <div class="kv"><span class="label">Opps seen</span><span class="value" id="sOppSeen">0</span></div>
    <div class="kv"><span class="label">Opps traded</span><span class="value" id="sOppTraded">0</span></div>
    <div id="planInfo" style="margin-top:6px; display:none">
      <div class="panel-title" style="margin-top:4px">Last Plan</div>
      <div class="kv"><span class="label">Combined</span><span class="value" id="pComb">--</span></div>
      <div class="kv"><span class="label">Margin</span><span class="value green" id="pMargin">--</span></div>
      <div class="kv"><span class="label">VWAP margin</span><span class="value" id="pVwap">--</span></div>
      <div class="kv"><span class="label">Cost</span><span class="value" id="pCost">--</span></div>
      <div class="kv"><span class="label">Exp. profit</span><span class="value green" id="pProfit">--</span></div>
    </div>
  </div>

  <div class="panel panel-positions">
    <div class="panel-title">Positions</div>
    <div style="overflow-y:auto; flex:1">
      <table class="pos-table">
        <thead><tr><th>Market</th><th>Pairs</th><th>Cost</th><th>P&L</th></tr></thead>
        <tbody id="posBody"></tbody>
      </table>
      <div id="posEmpty" style="color:var(--dim); padding:8px">No positions yet</div>
    </div>
  </div>

  <div class="panel panel-stats">
    <div class="panel-title">Stats</div>
    <div class="kv"><span class="label">Fill rate</span><span class="value" id="statFill">--</span></div>
    <div class="kv"><span class="label">API requests</span><span class="value" id="statApi">--</span></div>
    <div class="kv" style="margin-top:8px">
      <span class="label">Order size</span><span class="value" id="cfgSize">--</span>
    </div>
    <div class="kv"><span class="label">Slices</span><span class="value" id="cfgSlices">--</span></div>
    <div class="kv"><span class="label">Entry delay</span><span class="value" id="cfgDelay">--</span></div>
    <div class="kv"><span class="label">Assets</span><span class="value" id="cfgAssets">--</span></div>
    <div class="kv"><span class="label">Durations</span><span class="value" id="cfgDurations">--</span></div>
  </div>

  <!-- Row 3: log -->
  <div class="panel panel-log">
    <div class="panel-title">Live Log</div>
    <div class="log-container" id="logContainer"></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const fmt = (n, d=2) => n != null ? n.toFixed(d) : '--';
const fmtUsd = n => n != null ? '$' + n.toFixed(2) : '--';

function updateUI(d) {
  // Status
  const dot = $('statusDot');
  dot.className = 'status-dot ' + d.status;
  $('statusText').textContent = d.status;

  // Market
  $('mTitle').textContent = d.current_market || '--';
  $('mAsset').textContent = d.market_asset ? d.market_asset.toUpperCase() : '--';
  $('mDuration').textContent = d.market_duration || '--';
  $('mEnds').textContent = d.market_ends || '--';
  $('mFee').textContent = d.fee_bps ? d.fee_bps + ' bps' : '0 bps';
  $('mFound').textContent = d.markets_found;
  $('mScanned').textContent = d.windows_scanned;

  // Book
  $('upBid').textContent = fmt(d.up_best_bid, 4);
  $('upAsk').textContent = fmt(d.up_best_ask, 4);
  $('downBid').textContent = fmt(d.down_best_bid, 4);
  $('downAsk').textContent = fmt(d.down_best_ask, 4);
  $('upDepth').textContent = fmt(d.up_depth, 0) + ' shares';
  $('downDepth').textContent = fmt(d.down_depth, 0) + ' shares';

  const cb = $('combinedBar');
  const combined = d.combined_ask;
  cb.textContent = 'Combined: ' + fmt(combined, 4);
  if (combined > 0 && combined < 1.0) {
    cb.className = 'combined-bar arb';
    cb.textContent += '  (' + ((1 - combined) * 100).toFixed(2) + '% edge)';
  } else {
    cb.className = 'combined-bar no-arb';
  }

  // Capital
  $('cBank').textContent = fmtUsd(d.bankroll);
  $('cDeployed').textContent = fmtUsd(d.deployed);
  $('cAvail').textContent = fmtUsd(d.available);
  $('cExchange').textContent = d.exchange_balance != null ? fmtUsd(d.exchange_balance) : '--';
  const pnlEl = $('cPnl');
  pnlEl.textContent = fmtUsd(d.realized_pnl);
  pnlEl.className = 'value ' + (d.realized_pnl >= 0 ? 'green' : 'red');
  $('cDaily').textContent = fmtUsd(d.daily_spent);

  // Strategy
  const cfg = d.config || {};
  $('sMode').innerHTML = '<span class="tag ' + (cfg.order_mode||'maker') + '">' +
    (cfg.order_mode||'maker').toUpperCase() + '</span>';
  $('sMaxComb').textContent = fmt(cfg.max_combined_cost, 4) || '--';
  $('sMinMargin').textContent = fmt(cfg.min_profit_margin, 4) || '--';
  $('sOppSeen').textContent = d.opportunities_seen;
  $('sOppTraded').textContent = d.opportunities_traded;

  if (d.last_plan) {
    $('planInfo').style.display = 'block';
    $('pComb').textContent = fmt(d.last_plan.combined_net, 4);
    $('pMargin').textContent = (d.last_plan.margin_net * 100).toFixed(2) + '%';
    $('pVwap').textContent = (d.last_plan.vwap_margin * 100).toFixed(2) + '%';
    $('pCost').textContent = fmtUsd(d.last_plan.total_cost);
    $('pProfit').textContent = fmtUsd(d.last_plan.expected_profit);
  }

  // Config
  $('cfgSize').textContent = fmtUsd(cfg.order_size_usdc);
  $('cfgSlices').textContent = cfg.num_slices || '--';
  $('cfgDelay').textContent = (cfg.entry_delay_sec || '--') + 's';
  $('cfgAssets').textContent = cfg.assets || '--';
  $('cfgDurations').textContent = cfg.durations || '--';

  // Stats
  $('statFill').textContent = d.fill_rate || '--';
  $('statApi').textContent = d.api_stats || '--';

  // Positions
  const tbody = $('posBody');
  const empty = $('posEmpty');
  if (d.positions && d.positions.length > 0) {
    empty.style.display = 'none';
    tbody.innerHTML = d.positions.map(p => {
      const pnlClass = p.expected_profit >= 0 ? 'green' : 'red';
      const title = p.market_title.length > 30 ?
        p.market_title.substring(0, 28) + '..' : p.market_title;
      return `<tr>
        <td>${title}</td>
        <td>${p.pairs.toFixed(0)}</td>
        <td>${fmtUsd(p.total_invested)}</td>
        <td class="${pnlClass}">${fmtUsd(p.expected_profit)}</td>
      </tr>`;
    }).join('');
  } else {
    empty.style.display = 'block';
    tbody.innerHTML = '';
  }
}

function addLogLine(line) {
  const c = $('logContainer');
  const div = document.createElement('div');
  div.className = 'log-line';
  if (line.includes('[ERROR]')) div.className += ' error';
  else if (line.includes('[WARNING]')) div.className += ' warning';
  else if (line.includes('ARB FOUND') || line.includes('OPPORTUNITY')) div.className += ' arb';
  div.textContent = line;
  c.appendChild(div);
  // Keep max 500 lines in DOM
  while (c.children.length > 500) c.removeChild(c.firstChild);
  c.scrollTop = c.scrollHeight;
}

// Clock
setInterval(() => {
  $('clock').textContent = new Date().toLocaleTimeString();
}, 1000);

// SSE connection
function connect() {
  const es = new EventSource('/events');

  es.addEventListener('state', e => {
    try { updateUI(JSON.parse(e.data)); } catch(err) {}
  });

  es.addEventListener('log', e => {
    addLogLine(e.data);
  });

  es.addEventListener('init', e => {
    try {
      const d = JSON.parse(e.data);
      updateUI(d.state);
      d.logs.forEach(addLogLine);
    } catch(err) {}
  });

  es.onerror = () => {
    es.close();
    $('statusDot').className = 'status-dot stopped';
    $('statusText').textContent = 'disconnected - reconnecting...';
    setTimeout(connect, 3000);
  };
}

connect();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    """Serves the dashboard HTML, JSON API, and SSE stream."""

    # Suppress default access logging
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self._serve_html()
        elif self.path == '/api/state':
            self._serve_json()
        elif self.path == '/events':
            self._serve_sse()
        else:
            self.send_error(404)

    def _serve_html(self):
        content = DASHBOARD_HTML.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_json(self):
        body = json.dumps(state.to_dict()).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

        # Send initial state + recent logs
        init_data = json.dumps({
            "state": state.to_dict(),
            "logs": state.get_logs(200),
        })
        self.wfile.write(f"event: init\ndata: {init_data}\n\n".encode())
        self.wfile.flush()

        q = state.subscribe()
        try:
            while True:
                try:
                    event_type, data = q.get(timeout=15)
                    self.wfile.write(f"event: {event_type}\ndata: {data}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    # Keepalive
                    self.wfile.write(b":keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            state.unsubscribe(q)


def start_dashboard_server(port: int = 8080) -> HTTPServer:
    """Start the dashboard HTTP server in a daemon thread."""
    server = HTTPServer(('0.0.0.0', port), DashboardHandler)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server
