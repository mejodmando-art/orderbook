/* ── Helpers ──────────────────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);
const fmt = (n, d=4) => n == null ? '—' : (+n).toFixed(d);
const fmtUsdt = n => n == null ? '—' : (+n).toFixed(2) + ' USDT';
const fmtDate = s => s ? new Date(s).toLocaleString('ar-EG', {hour12:false}) : '—';
const pnlClass = n => +n >= 0 ? 'pos' : 'neg';

async function api(path) {
  try {
    const r = await fetch(path);
    if (!r.ok) throw new Error(r.status);
    return await r.json();
  } catch(e) {
    console.error('API error', path, e);
    return null;
  }
}

function riskBadge(r) {
  const map = {low:'badge-low', medium:'badge-medium', high:'badge-high'};
  return `<span class="badge ${map[r]||'badge-hold'}">${r||'—'}</span>`;
}
function sideBadge(s) {
  const cls = s==='buy'?'badge-buy':s==='sell'?'badge-sell':'badge-hold';
  return `<span class="badge ${cls}">${s||'—'}</span>`;
}

/* ── Navigation ───────────────────────────────────────────────────────────── */
const PAGE_TITLES = {
  overview: 'نظرة عامة',
  grids:    'بوتات الشبكة',
  super:    'SuperConsensus',
  auto:     'الوضع الآلي',
  trades:   'سجل الصفقات',
};

let currentPage = 'overview';

document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', e => {
    e.preventDefault();
    const page = el.dataset.page;
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    el.classList.add('active');
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    $('page-' + page).classList.add('active');
    $('pageTitle').textContent = PAGE_TITLES[page];
    currentPage = page;
    loadPage(page);
  });
});

$('btnRefresh').addEventListener('click', () => loadPage(currentPage));

/* ── PnL Chart ────────────────────────────────────────────────────────────── */
let pnlChart = null;

async function loadPnlChart() {
  const data = await api('/api/pnl/chart?days=30');
  if (!data || !data.length) return;

  const labels = data.map(d => d.day);
  const cumulative = data.map(d => d.cumulative);
  const daily = data.map(d => d.daily_pnl);

  const ctx = $('pnlChart').getContext('2d');
  if (pnlChart) pnlChart.destroy();

  pnlChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'تراكمي',
          data: cumulative,
          borderColor: '#6366f1',
          backgroundColor: 'rgba(99,102,241,0.08)',
          fill: true,
          tension: 0.4,
          pointRadius: 3,
          pointHoverRadius: 5,
        },
        {
          label: 'يومي',
          data: daily,
          borderColor: '#22d3a0',
          backgroundColor: 'rgba(34,211,160,0.06)',
          fill: false,
          tension: 0.4,
          pointRadius: 2,
          borderDash: [4,3],
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#7a8499', font: { size: 11 } } },
        tooltip: { mode: 'index', intersect: false }
      },
      scales: {
        x: { ticks: { color: '#7a8499', maxTicksLimit: 8 }, grid: { color: '#1a1e2a' } },
        y: { ticks: { color: '#7a8499' }, grid: { color: '#1a1e2a' } }
      }
    }
  });
}

/* ── Overview ─────────────────────────────────────────────────────────────── */
async function loadOverview() {
  const s = await api('/api/summary');
  if (!s) { setStatus(false); return; }
  setStatus(true);

  const pnl = +s.total_pnl;
  $('totalPnl').textContent = fmt(pnl, 2);
  $('totalPnl').className = 'stat-value ' + (pnl >= 0 ? 'green' : 'red');
  $('totalTrades').textContent = s.total_trades ?? '—';
  $('gridBotsActive').textContent = s.grid_bots_active ?? '—';
  $('superBotsActive').textContent = s.super_bots_active ?? '—';
  $('gridPnl').textContent = fmt(s.total_pnl_grid, 2);
  $('gridPnl').className = 'stat-value ' + (+s.total_pnl_grid >= 0 ? 'green' : 'red');

  if (s.auto_mode_active) {
    $('autoModeStatus').textContent = 'مفعّل';
    $('autoModeStatus').className = 'stat-value green';
    $('autoPositions').textContent = `${s.open_auto_positions} صفقة مفتوحة`;
  } else {
    $('autoModeStatus').textContent = 'موقوف';
    $('autoModeStatus').className = 'stat-value';
    $('autoPositions').textContent = '—';
  }

  await loadPnlChart();
  await loadRecentTrades();
}

async function loadRecentTrades() {
  const trades = await api('/api/trades/recent?limit=20');
  const el = $('recentTradesTable');
  if (!trades || !trades.length) { el.innerHTML = '<div class="empty">لا توجد صفقات بعد</div>'; return; }

  el.innerHTML = `<table>
    <thead><tr>
      <th>الرمز</th><th>النوع</th><th>السعر</th><th>الكمية</th><th>الربح</th><th>المصدر</th><th>الوقت</th>
    </tr></thead>
    <tbody>${trades.map(t => `<tr>
      <td><strong>${t.symbol}</strong></td>
      <td>${sideBadge(t.side)}</td>
      <td>${fmt(t.price, 6)}</td>
      <td>${fmt(t.qty, 4)}</td>
      <td class="${pnlClass(t.pnl)}">${fmt(t.pnl, 4)}</td>
      <td><span class="badge badge-hold">${t.source}</span></td>
      <td style="color:var(--text-muted)">${fmtDate(t.executed_at)}</td>
    </tr>`).join('')}</tbody>
  </table>`;
}

/* ── Grids ────────────────────────────────────────────────────────────────── */
async function loadGrids() {
  const grids = await api('/api/grids');
  const el = $('gridsTable');
  if (!grids || !grids.length) { el.innerHTML = '<div class="empty">لا توجد بوتات شبكة نشطة</div>'; return; }

  el.innerHTML = `<table>
    <thead><tr>
      <th>الرمز</th><th>المخاطرة</th><th>الاستثمار</th><th>السعر الأدنى</th><th>السعر الأعلى</th>
      <th>الشبكات</th><th>الكمية المحتفظ بها</th><th>متوسط الشراء</th><th>الربح</th><th>الصفقات</th><th>بدأ في</th>
    </tr></thead>
    <tbody>${grids.map(g => `<tr>
      <td><strong>${g.symbol}</strong></td>
      <td>${riskBadge(g.risk_level)}</td>
      <td>${fmt(g.total_investment, 2)}</td>
      <td>${fmt(g.lower_price, 6)}</td>
      <td>${fmt(g.upper_price, 6)}</td>
      <td>${g.grid_count}</td>
      <td>${fmt(g.held_qty, 4)}</td>
      <td>${fmt(g.avg_buy_price, 6)}</td>
      <td class="${pnlClass(g.realized_pnl)}">${fmt(g.realized_pnl, 4)}</td>
      <td>${g.sell_count ?? '—'}</td>
      <td style="color:var(--text-muted)">${fmtDate(g.started_at)}</td>
    </tr>`).join('')}</tbody>
  </table>`;
}

/* ── SuperConsensus ───────────────────────────────────────────────────────── */
async function loadSuper() {
  const [bots, logs] = await Promise.all([
    api('/api/super_bots'),
    api('/api/logs/super?limit=50'),
  ]);

  const botsEl = $('superTable');
  if (!bots || !bots.length) {
    botsEl.innerHTML = '<div class="empty">لا توجد بوتات SuperConsensus نشطة</div>';
  } else {
    botsEl.innerHTML = `<table>
      <thead><tr>
        <th>الرمز</th><th>الاستثمار</th><th>الكمية</th><th>سعر الدخول</th><th>الربح المحقق</th><th>الصفقات</th><th>الحالة</th>
      </tr></thead>
      <tbody>${bots.map(b => {
        const paused = b.is_paused;
        const badge = paused
          ? '<span class="badge badge-paused">موقوف مؤقتاً</span>'
          : '<span class="badge badge-active">نشط</span>';
        return `<tr>
          <td><strong>${b.symbol}</strong></td>
          <td>${fmt(b.total_investment, 2)}</td>
          <td>${fmt(b.current_position_qty, 4)}</td>
          <td>${fmt(b.current_position_entry_price, 6)}</td>
          <td class="${pnlClass(b.realized_pnl)}">${fmt(b.realized_pnl, 4)}</td>
          <td>${b.total_trades}</td>
          <td>${badge}</td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;
  }

  const logsEl = $('superLogsTable');
  if (!logs || !logs.length) {
    logsEl.innerHTML = '<div class="empty">لا توجد سجلات بعد</div>';
  } else {
    logsEl.innerHTML = `<table>
      <thead><tr>
        <th>الرمز</th><th>السعر</th><th>RSI</th><th>MACD</th><th>EMA</th><th>BB</th><th>حجم</th>
        <th>شراء</th><th>بيع</th><th>الإجراء</th><th>الوقت</th>
      </tr></thead>
      <tbody>${logs.map(l => `<tr>
        <td><strong>${l.symbol}</strong></td>
        <td>${fmt(l.price, 6)}</td>
        <td>${signalBadge(l.rsi_signal)}</td>
        <td>${signalBadge(l.macd_signal)}</td>
        <td>${signalBadge(l.ema_signal)}</td>
        <td>${signalBadge(l.bb_signal)}</td>
        <td>${signalBadge(l.volume_signal)}</td>
        <td style="color:var(--green)">${l.buy_votes}</td>
        <td style="color:var(--red)">${l.sell_votes}</td>
        <td>${sideBadge(l.action_taken)}</td>
        <td style="color:var(--text-muted)">${fmtDate(l.timestamp)}</td>
      </tr>`).join('')}</tbody>
    </table>`;
  }
}

function signalBadge(s) {
  if (!s) return '—';
  const up = s.toUpperCase();
  if (up === 'BUY')  return '<span class="badge badge-buy">شراء</span>';
  if (up === 'SELL') return '<span class="badge badge-sell">بيع</span>';
  return '<span class="badge badge-hold">محايد</span>';
}

/* ── Auto Mode ────────────────────────────────────────────────────────────── */
async function loadAuto() {
  const data = await api('/api/auto_mode');
  const cardsEl = $('autoSettingsCards');
  const posEl = $('autoPositionsTable');

  if (!data) { cardsEl.innerHTML = ''; posEl.innerHTML = '<div class="empty">لا توجد بيانات</div>'; return; }

  const s = data.settings || {};
  const active = s.is_active;

  cardsEl.innerHTML = `
    <div class="card stat-card">
      <div class="stat-label">الحالة</div>
      <div class="stat-value ${active ? 'green' : ''}">${active ? 'مفعّل' : 'موقوف'}</div>
    </div>
    <div class="card stat-card">
      <div class="stat-label">جني الأرباح</div>
      <div class="stat-value yellow">${fmt(s.take_profit_pct, 1)}%</div>
    </div>
    <div class="card stat-card">
      <div class="stat-label">وقف الخسارة</div>
      <div class="stat-value red">${fmt(s.stop_loss_pct, 1)}%</div>
    </div>
    <div class="card stat-card">
      <div class="stat-label">أقصى صفقات</div>
      <div class="stat-value blue">${s.max_open_trades ?? '—'}</div>
    </div>
    <div class="card stat-card">
      <div class="stat-label">أقصى رأس مال</div>
      <div class="stat-value purple">${fmt(s.max_capital_pct, 0)}%</div>
    </div>
    <div class="card stat-card">
      <div class="stat-label">فترة المسح</div>
      <div class="stat-value">${s.scan_interval_minutes ?? '—'} د</div>
    </div>
  `;

  const positions = data.positions || [];
  if (!positions.length) {
    posEl.innerHTML = '<div class="empty">لا توجد صفقات مفتوحة</div>';
  } else {
    posEl.innerHTML = `<table>
      <thead><tr>
        <th>الرمز</th><th>الكمية</th><th>سعر الدخول</th><th>السعر الحالي</th><th>الربح/الخسارة</th><th>فُتحت في</th>
      </tr></thead>
      <tbody>${positions.map(p => {
        const pnl = p.unrealized_pnl ?? p.pnl ?? 0;
        return `<tr>
          <td><strong>${p.symbol}</strong></td>
          <td>${fmt(p.qty, 4)}</td>
          <td>${fmt(p.entry_price, 6)}</td>
          <td>${fmt(p.current_price, 6)}</td>
          <td class="${pnlClass(pnl)}">${fmt(pnl, 4)}</td>
          <td style="color:var(--text-muted)">${fmtDate(p.opened_at)}</td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;
  }
}

/* ── All Trades ───────────────────────────────────────────────────────────── */
async function loadTrades() {
  const trades = await api('/api/trades/recent?limit=200');
  const el = $('allTradesTable');
  if (!trades || !trades.length) { el.innerHTML = '<div class="empty">لا توجد صفقات بعد</div>'; return; }

  el.innerHTML = `<table>
    <thead><tr>
      <th>الرمز</th><th>النوع</th><th>السعر</th><th>الكمية</th><th>الربح</th><th>المصدر</th><th>الوقت</th>
    </tr></thead>
    <tbody>${trades.map(t => `<tr>
      <td><strong>${t.symbol}</strong></td>
      <td>${sideBadge(t.side)}</td>
      <td>${fmt(t.price, 6)}</td>
      <td>${fmt(t.qty, 4)}</td>
      <td class="${pnlClass(t.pnl)}">${fmt(t.pnl, 4)}</td>
      <td><span class="badge badge-hold">${t.source}</span></td>
      <td style="color:var(--text-muted)">${fmtDate(t.executed_at)}</td>
    </tr>`).join('')}</tbody>
  </table>`;
}

/* ── Status indicator ─────────────────────────────────────────────────────── */
function setStatus(online) {
  const dot = $('statusDot');
  const txt = $('statusText');
  dot.className = 'status-dot ' + (online ? 'online' : 'offline');
  txt.textContent = online ? 'متصل' : 'غير متصل';
}

/* ── Router ───────────────────────────────────────────────────────────────── */
function loadPage(page) {
  switch(page) {
    case 'overview': loadOverview(); break;
    case 'grids':    loadGrids();    break;
    case 'super':    loadSuper();    break;
    case 'auto':     loadAuto();     break;
    case 'trades':   loadTrades();   break;
  }
}

/* ── Init & auto-refresh ──────────────────────────────────────────────────── */
loadPage('overview');
setInterval(() => loadPage(currentPage), 30000);
