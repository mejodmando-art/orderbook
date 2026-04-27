/* ── Helpers ──────────────────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);
const fmt  = (n, d=4) => n == null ? '—' : (+n).toFixed(d);
const fmtDate = s => s ? new Date(s).toLocaleString('ar-EG',{hour12:false}) : '—';
const pnlClass = n => +n >= 0 ? 'pos' : 'neg';

async function api(path) {
  try {
    const r = await fetch(path);
    if (!r.ok) throw new Error(r.status);
    return await r.json();
  } catch(e) { console.error('API', path, e); return null; }
}

function riskBadge(r) {
  const m = {low:'badge-low', medium:'badge-medium', high:'badge-high'};
  return `<span class="badge ${m[r]||'badge-hold'}">${r||'—'}</span>`;
}
function sideBadge(s) {
  const c = s==='buy'?'badge-buy':s==='sell'?'badge-sell':'badge-hold';
  const l = s==='buy'?'شراء':s==='sell'?'بيع':'محايد';
  return `<span class="badge ${c}">${l}</span>`;
}
function signalBadge(s) {
  if (!s) return '—';
  const u = s.toUpperCase();
  if (u==='BUY')  return '<span class="badge badge-buy">شراء</span>';
  if (u==='SELL') return '<span class="badge badge-sell">بيع</span>';
  return '<span class="badge badge-hold">محايد</span>';
}

/* ── Navigation ───────────────────────────────────────────────────────────── */
const PAGE_TITLES = {
  overview:'نظرة عامة', grids:'بوتات الشبكة',
  super:'SuperConsensus', auto:'الوضع الآلي', trades:'سجل الصفقات'
};
let currentPage = 'overview';

function navigate(page) {
  currentPage = page;
  $('pageTitle').textContent = PAGE_TITLES[page];
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  $('page-' + page).classList.add('active');
  // sync both navs
  document.querySelectorAll('.bnav-item, .snav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.page === page);
  });
  loadPage(page);
}

document.querySelectorAll('.bnav-item, .snav-item').forEach(el => {
  el.addEventListener('click', () => navigate(el.dataset.page));
});
$('btnRefresh').addEventListener('click', () => loadPage(currentPage));

/* ── Status ───────────────────────────────────────────────────────────────── */
function setStatus(online) {
  ['statusDot','statusDotSidebar'].forEach(id => {
    const el = $(id); if (!el) return;
    el.className = 'status-dot ' + (online ? 'online' : 'offline');
  });
  ['statusText','statusTextSidebar'].forEach(id => {
    const el = $(id); if (!el) return;
    el.textContent = online ? 'متصل' : 'غير متصل';
  });
}

/* ── PnL Chart ────────────────────────────────────────────────────────────── */
let pnlChart = null;
async function loadPnlChart() {
  const data = await api('/api/pnl/chart?days=30');
  if (!data || !data.length) return;
  const ctx = $('pnlChart').getContext('2d');
  if (pnlChart) pnlChart.destroy();
  pnlChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: data.map(d => d.day),
      datasets: [
        { label:'تراكمي', data: data.map(d=>d.cumulative),
          borderColor:'#6366f1', backgroundColor:'rgba(99,102,241,0.08)',
          fill:true, tension:0.4, pointRadius:2, pointHoverRadius:4 },
        { label:'يومي', data: data.map(d=>d.daily_pnl),
          borderColor:'#10d9a0', backgroundColor:'rgba(16,217,160,0.05)',
          fill:false, tension:0.4, pointRadius:1, borderDash:[4,3] }
      ]
    },
    options: {
      responsive:true, maintainAspectRatio:false,
      plugins: {
        legend:{ labels:{ color:'#6b7280', font:{size:11}, boxWidth:12 } },
        tooltip:{ mode:'index', intersect:false }
      },
      scales: {
        x:{ ticks:{ color:'#6b7280', maxTicksLimit:6, font:{size:10} }, grid:{ color:'#1a1e2c' } },
        y:{ ticks:{ color:'#6b7280', font:{size:10} }, grid:{ color:'#1a1e2c' } }
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
  $('totalPnl').textContent = (+pnl).toFixed(2);
  $('totalPnl').className = 'stat-value ' + (pnl>=0?'green':'red');
  $('totalTrades').textContent = s.total_trades ?? '—';
  $('gridBotsActive').textContent = s.grid_bots_active ?? '—';
  $('superBotsActive').textContent = s.super_bots_active ?? '—';
  $('gridPnl').textContent = (+s.total_pnl_grid).toFixed(2);
  $('gridPnl').className = 'stat-value ' + (+s.total_pnl_grid>=0?'green':'red');

  if (s.auto_mode_active) {
    $('autoModeStatus').textContent = 'مفعّل';
    $('autoModeStatus').className = 'stat-value green';
    $('autoPositions').textContent = s.open_auto_positions + ' صفقة';
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
  if (!trades || !trades.length) { el.innerHTML='<div class="empty">لا توجد صفقات بعد</div>'; return; }
  el.innerHTML = `<table>
    <thead><tr><th>الرمز</th><th>النوع</th><th>السعر</th><th>الكمية</th><th>الربح</th><th>المصدر</th></tr></thead>
    <tbody>${trades.map(t=>`<tr>
      <td><strong>${t.symbol}</strong></td>
      <td>${sideBadge(t.side)}</td>
      <td>${fmt(t.price,6)}</td>
      <td>${fmt(t.qty,4)}</td>
      <td class="${pnlClass(t.pnl)}">${fmt(t.pnl,4)}</td>
      <td><span class="badge ${t.source==='grid'?'badge-grid':'badge-super'}">${t.source}</span></td>
    </tr>`).join('')}</tbody>
  </table>`;
}

/* ── Grids ────────────────────────────────────────────────────────────────── */
async function loadGrids() {
  const grids = await api('/api/grids');
  const el = $('gridsTable');
  if (!grids || !grids.length) { el.innerHTML='<div class="empty">لا توجد بوتات شبكة نشطة</div>'; return; }
  el.innerHTML = `<table>
    <thead><tr>
      <th>الرمز</th><th>المخاطرة</th><th>الاستثمار</th><th>أدنى</th><th>أعلى</th>
      <th>شبكات</th><th>الكمية</th><th>متوسط الشراء</th><th>الربح</th><th>صفقات</th>
    </tr></thead>
    <tbody>${grids.map(g=>`<tr>
      <td><strong>${g.symbol}</strong></td>
      <td>${riskBadge(g.risk_level)}</td>
      <td>${fmt(g.total_investment,2)}</td>
      <td>${fmt(g.lower_price,6)}</td>
      <td>${fmt(g.upper_price,6)}</td>
      <td>${g.grid_count}</td>
      <td>${fmt(g.held_qty,4)}</td>
      <td>${fmt(g.avg_buy_price,6)}</td>
      <td class="${pnlClass(g.realized_pnl)}">${fmt(g.realized_pnl,4)}</td>
      <td>${g.sell_count??'—'}</td>
    </tr>`).join('')}</tbody>
  </table>`;
}

/* ── SuperConsensus ───────────────────────────────────────────────────────── */
async function loadSuper() {
  const [bots, logs] = await Promise.all([api('/api/super_bots'), api('/api/logs/super?limit=50')]);

  const botsEl = $('superTable');
  if (!bots || !bots.length) {
    botsEl.innerHTML = '<div class="empty">لا توجد بوتات SuperConsensus نشطة</div>';
  } else {
    botsEl.innerHTML = `<table>
      <thead><tr><th>الرمز</th><th>الاستثمار</th><th>الكمية</th><th>سعر الدخول</th><th>الربح</th><th>صفقات</th><th>الحالة</th></tr></thead>
      <tbody>${bots.map(b=>`<tr>
        <td><strong>${b.symbol}</strong></td>
        <td>${fmt(b.total_investment,2)}</td>
        <td>${fmt(b.current_position_qty,4)}</td>
        <td>${fmt(b.current_position_entry_price,6)}</td>
        <td class="${pnlClass(b.realized_pnl)}">${fmt(b.realized_pnl,4)}</td>
        <td>${b.total_trades}</td>
        <td>${b.is_paused?'<span class="badge badge-paused">موقوف</span>':'<span class="badge badge-active">نشط</span>'}</td>
      </tr>`).join('')}</tbody>
    </table>`;
  }

  const logsEl = $('superLogsTable');
  if (!logs || !logs.length) {
    logsEl.innerHTML = '<div class="empty">لا توجد سجلات بعد</div>';
  } else {
    logsEl.innerHTML = `<table>
      <thead><tr><th>الرمز</th><th>السعر</th><th>RSI</th><th>MACD</th><th>EMA</th><th>BB</th><th>شراء</th><th>بيع</th><th>الإجراء</th></tr></thead>
      <tbody>${logs.map(l=>`<tr>
        <td><strong>${l.symbol}</strong></td>
        <td>${fmt(l.price,6)}</td>
        <td>${signalBadge(l.rsi_signal)}</td>
        <td>${signalBadge(l.macd_signal)}</td>
        <td>${signalBadge(l.ema_signal)}</td>
        <td>${signalBadge(l.bb_signal)}</td>
        <td style="color:var(--green)">${l.buy_votes}</td>
        <td style="color:var(--red)">${l.sell_votes}</td>
        <td>${sideBadge(l.action_taken)}</td>
      </tr>`).join('')}</tbody>
    </table>`;
  }
}

/* ── Auto Mode ────────────────────────────────────────────────────────────── */
async function loadAuto() {
  const data = await api('/api/auto_mode');
  const cardsEl = $('autoSettingsCards');
  const posEl   = $('autoPositionsTable');
  if (!data) { cardsEl.innerHTML=''; posEl.innerHTML='<div class="empty">لا توجد بيانات</div>'; return; }

  const s = data.settings || {};
  cardsEl.innerHTML = `
    <div class="card stat-card"><div class="stat-label">الحالة</div>
      <div class="stat-value ${s.is_active?'green':''}">${s.is_active?'مفعّل':'موقوف'}</div></div>
    <div class="card stat-card"><div class="stat-label">جني الأرباح</div>
      <div class="stat-value yellow">${fmt(s.take_profit_pct,1)}%</div></div>
    <div class="card stat-card"><div class="stat-label">وقف الخسارة</div>
      <div class="stat-value red">${fmt(s.stop_loss_pct,1)}%</div></div>
    <div class="card stat-card"><div class="stat-label">أقصى صفقات</div>
      <div class="stat-value blue">${s.max_open_trades??'—'}</div></div>
    <div class="card stat-card"><div class="stat-label">رأس المال</div>
      <div class="stat-value purple">${fmt(s.max_capital_pct,0)}%</div></div>
    <div class="card stat-card"><div class="stat-label">فترة المسح</div>
      <div class="stat-value">${s.scan_interval_minutes??'—'} د</div></div>
  `;

  const positions = data.positions || [];
  if (!positions.length) {
    posEl.innerHTML = '<div class="empty">لا توجد صفقات مفتوحة</div>';
  } else {
    posEl.innerHTML = `<table>
      <thead><tr><th>الرمز</th><th>الكمية</th><th>سعر الدخول</th><th>الربح/الخسارة</th><th>فُتحت في</th></tr></thead>
      <tbody>${positions.map(p=>{
        const pnl = p.unrealized_pnl??p.pnl??0;
        return `<tr>
          <td><strong>${p.symbol}</strong></td>
          <td>${fmt(p.qty,4)}</td>
          <td>${fmt(p.entry_price,6)}</td>
          <td class="${pnlClass(pnl)}">${fmt(pnl,4)}</td>
          <td style="color:var(--muted)">${fmtDate(p.opened_at)}</td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;
  }
}

/* ── All Trades ───────────────────────────────────────────────────────────── */
async function loadTrades() {
  const trades = await api('/api/trades/recent?limit=200');
  const el = $('allTradesTable');
  if (!trades || !trades.length) { el.innerHTML='<div class="empty">لا توجد صفقات بعد</div>'; return; }
  el.innerHTML = `<table>
    <thead><tr><th>الرمز</th><th>النوع</th><th>السعر</th><th>الكمية</th><th>الربح</th><th>المصدر</th><th>الوقت</th></tr></thead>
    <tbody>${trades.map(t=>`<tr>
      <td><strong>${t.symbol}</strong></td>
      <td>${sideBadge(t.side)}</td>
      <td>${fmt(t.price,6)}</td>
      <td>${fmt(t.qty,4)}</td>
      <td class="${pnlClass(t.pnl)}">${fmt(t.pnl,4)}</td>
      <td><span class="badge ${t.source==='grid'?'badge-grid':'badge-super'}">${t.source}</span></td>
      <td style="color:var(--muted)">${fmtDate(t.executed_at)}</td>
    </tr>`).join('')}</tbody>
  </table>`;
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

/* ── Init ─────────────────────────────────────────────────────────────────── */
loadPage('overview');
setInterval(() => loadPage(currentPage), 30000);
