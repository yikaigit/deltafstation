/* 回测中心：参数表单、运行态、结果详情（七区块）、历史对比 */
const BacktestPage = (() => {
  const el = (id) => document.getElementById(id);

  let navChart = null;
  let yearChart = null;
  let cmpChart = null;
  let detail = null;          // 当前详情 payload
  let tradeShown = 0;         // 交易明细已渲染行数
  let cache = new Map();      // id -> payload（对比用，避免重复请求）
  const picked = [];          // 已勾选的回测 id（最多 2 个）
  let running = false;
  let timer = null;

  const TRADE_PAGE = 150;

  function fmtSigned(value, digits = 2) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
    const n = Number(value);
    return (n > 0 ? '+' : '') + n.toFixed(digits) + '%';
  }

  function toneOf(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return 'faint';
    return Number(value) >= 0 ? 'up' : 'down';
  }

  function numText(value, digits = 2) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
    return Number(value).toFixed(digits);
  }

  /* ---------------- 记录列表 ---------------- */

  function renderRows(items) {
    const body = el('bt-body');
    const counter = el('bt-count');
    if (counter) counter.textContent = items.length ? `共 ${items.length} 条` : '暂无记录';
    if (!body) return;
    if (!items.length) {
      body.innerHTML = `<tr><td colspan="10" style="padding:0">${App.empty(
        '尚无回测记录',
        '填好左侧参数点「运行回测」，结果会归档在这里'
      )}</td></tr>`;
      return;
    }
    body.innerHTML = items
      .map((row) => {
        const params = row.params || {};
        const period = row.period || {};
        const range =
          period.start && period.end
            ? `${App.shortDate(period.start)} ~ ${App.shortDate(period.end)}`
            : '—';
        const checked = picked.includes(row.id) ? 'checked' : '';
        return `<tr class="clickable" onclick="BacktestPage.openDetail('${App.esc(row.id)}')">
          <td class="pick" onclick="event.stopPropagation()">
            <input type="checkbox" ${checked} data-id="${App.esc(row.id)}"
                   onchange="BacktestPage.onPick(this)">
          </td>
          <td class="mono small">${App.esc(row.id)}</td>
          <td class="mono small">${App.esc(range)}</td>
          <td class="right mono">${App.num(params.top_n, 0)}</td>
          <td class="right mono ${toneOf(row.total_return)}">${fmtSigned(row.total_return)}</td>
          <td class="right mono ${toneOf(row.annual_return)}">${fmtSigned(row.annual_return)}</td>
          <td class="right mono">${
            row.sharpe === null || row.sharpe === undefined ? '—' : Number(row.sharpe).toFixed(2)
          }</td>
          <td class="right mono down">${
            row.max_drawdown === null || row.max_drawdown === undefined
              ? '—'
              : fmtSigned(row.max_drawdown)
          }</td>
          <td class="mono small">${App.esc(row.created_at || '—')}</td>
          <td class="right" onclick="event.stopPropagation()">
            <button class="btn btn-sm" onclick="BacktestPage.remove('${App.esc(row.id)}')">删除</button>
          </td>
        </tr>`;
      })
      .join('');
  }

  async function loadVersions() {
    const select = el('bt-version');
    if (!select) return;
    try {
      const data = await App.get('/api/score-versions');
      const items = data.items || [];
      if (!items.length) {
        select.innerHTML = '<option value="">（暂无打分版本）</option>';
        return;
      }
      select.innerHTML = items
        .map((v) => `<option value="${App.esc(v.version)}">${App.esc(v.version)}</option>`)
        .join('');
    } catch (err) {
      select.innerHTML = '<option value="">（读取失败）</option>';
    }
  }

  function defaultDates() {
    const end = new Date();
    const start = new Date(end.getTime());
    start.setFullYear(start.getFullYear() - 5);
    const iso = (d) => d.toISOString().slice(0, 10);
    const startInput = el('bt-start');
    const endInput = el('bt-end');
    if (startInput && !startInput.value) startInput.value = iso(start);
    if (endInput && !endInput.value) endInput.value = iso(end);
  }

  async function reload() {
    defaultDates();
    await loadVersions();
    try {
      const data = await App.get('/api/backtests');
      renderRows(data.items || []);
    } catch (err) {
      App.toast('回测列表加载失败：' + err.message, 'error');
      const body = el('bt-body');
      if (body) {
        body.innerHTML = `<tr><td colspan="10" style="padding:0">${App.empty(
          '加载失败',
          err.message
        )}</td></tr>`;
      }
    }
  }

  /* ---------------- 运行态 ---------------- */

  async function submit(event) {
    event.preventDefault();
    if (running) return;
    const btn = el('bt-run');
    const form = el('bt-form');
    const hint = el('bt-hint');
    const payload = {};
    new FormData(form).forEach((value, key) => {
      payload[key] = value === '' ? null : value;
    });
    payload.exclude_st = el('bt-exclude-st').checked;
    payload.regime = el('bt-regime').checked;
    payload.grid = el('bt-grid').checked;
    for (const key of ['top_n', 'rebal_days', 'initial_cash']) {
      payload[key] = Number(payload[key]);
    }
    for (const key of ['max_weight', 'is_ratio', 'min_score_pct']) {
      payload[key] = Number(payload[key]);
    }
    if (!payload.version) delete payload.version;

    running = true;
    const started = Date.now();
    if (btn) {
      btn.disabled = true;
      btn.textContent = '运行中…';
    }
    const baseHint = '回测运行中（含 32 组参数网格可能要几十秒）… 已等待 ';
    if (timer) clearInterval(timer);
    timer = setInterval(() => {
      const seconds = Math.round((Date.now() - started) / 1000);
      if (hint) hint.textContent = baseHint + seconds + 's';
    }, 1000);

    try {
      const res = await App.post('/api/backtests', payload);
      App.toast('回测完成：' + (res.id || ''));
      await reload();
      if (res && res.id) renderDetail(res);
    } catch (err) {
      App.toast(err.message, 'error');
    } finally {
      running = false;
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
      if (btn) {
        btn.disabled = false;
        btn.textContent = '运行回测';
      }
      if (hint) hint.textContent = '已接入：T 日收盘出信号、T+1 开盘成交';
    }
  }

  async function remove(id) {
    if (!window.confirm(`确认删除回测 ${id}？该操作不可恢复。`)) return;
    try {
      await App.del('/api/backtests/' + encodeURIComponent(id));
      App.toast('已删除 ' + id);
      if (detail && detail.id === id) closeDetail();
      if (picked.includes(id)) picked.splice(picked.indexOf(id), 1);
      syncCompareBar();
      await reload();
    } catch (err) {
      App.toast('删除失败：' + err.message, 'error');
    }
  }

  /* ---------------- 详情：外壳 ---------------- */

  async function openDetail(id) {
    try {
      const data = await App.get('/api/backtests/' + encodeURIComponent(id));
      cache.set(id, data);
      renderDetail(data);
    } catch (err) {
      App.toast('读取回测详情失败：' + err.message, 'error');
    }
  }

  function closeDetail() {
    const box = el('bt-detail');
    if (box) box.hidden = true;
    detail = null;
  }

  function renderDetail(payload) {
    detail = payload;
    const box = el('bt-detail');
    if (!box) return;
    box.hidden = false;
    el('bt-detail-id').textContent =
      `${payload.id || '—'} · ${App.shortDate((payload.period || {}).start)} ~ ${App.shortDate(
        (payload.period || {}).end
      )} · 打分 ${payload.score_version || '—'}`;

    const p = payload.params || {};
    const chips = [
      `持仓 ${p.top_n ?? '—'} 只`,
      `每 ${p.rebal_days ?? '—'} 交易日调仓`,
      `单票上限 ${p.max_weight != null ? Number(p.max_weight).toFixed(2) : '—'}`,
      `权重 ${p.weight_method === 'score' ? '按打分占比' : '等权'}`,
      `择时 ${p.regime ? '开（MA' + p.regime_ma_fast + '/' + p.regime_ma_slow + '）' : '关'}`,
      `分位门槛 ${p.min_score_pct || 0}`,
      `ST ${p.exclude_st === false ? '不剔除' : '剔除'}`,
      `初始资金 ${App.num(p.initial_cash, 0)} 元`,
      `IS/OOS ${p.is_ratio != null ? Number(p.is_ratio).toFixed(2) : '—'}`,
      `换手 ${payload.turnover != null ? payload.turnover.toFixed(2) : '—'}x/年`,
      `平均持仓 ${payload.avg_positions != null ? payload.avg_positions.toFixed(2) : '—'} 只`,
      `调仓 ${payload.rebalance_count ?? '—'} 次`,
    ];
    el('bt-detail-params').innerHTML = chips
      .map((c) => `<span class="chip">${App.esc(c)}</span>`)
      .join('');

    renderMetrics(payload);
    renderNavChart(payload);
    renderYearChart(payload);
    renderIsoos(payload);
    renderGridHeat(payload);
    renderHoldings(payload);
    tradeShown = 0;
    renderTrades(payload);
    const warnBox = el('bt-detail-warn');
    const warnings = payload.warnings || [];
    if (warnBox) {
      if (warnings.length) {
        warnBox.hidden = false;
        warnBox.innerHTML = `<div class="card sub" style="border-left:3px solid var(--warn)">
          <div class="card-body">
            <div class="card-title" style="font-size:12px;color:var(--warn);margin-bottom:4px">
              引擎自检告警（${warnings.length}）
            </div>
            <ul class="small muted" style="margin:0;padding-left:18px">
              ${warnings.map((w) => `<li>${App.esc(w)}</li>`).join('')}
            </ul>
          </div>
        </div>`;
      } else {
        warnBox.hidden = true;
        warnBox.innerHTML = '';
      }
    }
    box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  /* ---------------- 详情：指标卡 ---------------- */

  function renderMetrics(payload) {
    const full = (payload.metrics || {}).full || {};
    const cards = [
      ['全区间总收益', fmtSigned(full.total_return), `基准 ${fmtSigned(full.bench_return)}`],
      ['年化收益', fmtSigned(full.annual_return), `年化波动 ${full.annual_vol != null ? full.annual_vol.toFixed(2) + '%' : '—'}`],
      ['夏普', numText(full.sharpe, 3), `索提诺 ${numText(full.sortino, 3)}`],
      ['最大回撤', fmtSigned(full.max_drawdown), `卡玛 ${numText(full.calmar, 3)}`],
      ['超额收益', fmtSigned(full.excess_return), `信息比 ${numText(full.information_ratio, 3)}`],
      ['月胜率', full.month_win_rate != null ? full.month_win_rate.toFixed(1) + '%' : '—', '月度收益为正占比'],
      ['交易笔数', App.num(full.trades, 0), `阻挡 ${full.blocked_trades ?? 0} 笔`],
      ['交易日', App.num(full.days, 0), `平均持仓 ${payload.avg_positions != null ? payload.avg_positions.toFixed(2) : '—'} 只`],
    ];
    el('bt-metrics').innerHTML = cards
      .map(([label, value, hint]) => {
        const tone = label.indexOf('回撤') >= 0 ? 'down' : toneOf(value);
        return `<div class="card stat metric">
          <span class="metric-label">${App.esc(label)}</span>
          <span class="metric-value ${tone}">${App.esc(value)}</span>
          <span class="metric-hint">${App.esc(hint)}</span>
        </div>`;
      })
      .join('');
  }

  /* ---------------- 详情：净值曲线 ---------------- */

  function renderNavChart(payload) {
    const dom = el('chart-nav');
    const rows = (payload.equity || []).filter((r) => r && r.date && r.nav != null);
    if (!rows.length) {
      dom.innerHTML = App.empty('无净值数据', '回测区间内没有可交易的信号');
      return;
    }
    const base = rows[0].nav;
    const navData = rows.map((r) => [r.date, Number(((r.nav / base) * 100).toFixed(4))]);
    const benchRows = rows.filter((r) => r.bench != null);
    const bBase = benchRows.length ? benchRows[0].bench : null;
    const benchData = bBase
      ? benchRows.map((r) => [r.date, Number(((r.bench / bBase) * 100).toFixed(4))])
      : [];
    // 回撤阴影：负值部分涂在零轴下方
    const ddData = rows.map((r) => [
      r.date,
      r.drawdown != null ? Number((r.drawdown * 100).toFixed(3)) : null,
    ]);
    const period = payload.period || {};
    const c = App.chartColors();
    dom.innerHTML = '';
    if (!navChart) navChart = echarts.init(dom);
    navChart.setOption({
      grid: { left: 48, right: 16, top: 28, bottom: 32 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        valueFormatter: (v) => (v == null ? '—' : Number(v).toFixed(2)),
      },
      legend: { top: 0, textStyle: { color: c.text, fontSize: 12 } },
      xAxis: {
        type: 'time',
        axisLine: { lineStyle: { color: '#e5e7eb' } },
        axisLabel: { color: c.axis, fontSize: 11 },
        splitLine: { show: false },
      },
      yAxis: [
        {
          type: 'value',
          scale: true,
          name: '净值',
          nameTextStyle: { color: c.text, fontSize: 11 },
          axisLabel: { color: c.axis, fontSize: 11, formatter: '{value}' },
          splitLine: { lineStyle: { color: c.split } },
        },
        {
          type: 'value',
          scale: true,
          name: '回撤%',
          nameTextStyle: { color: c.text, fontSize: 11 },
          axisLabel: { color: c.axis, fontSize: 11, formatter: '{value}' },
          splitLine: { show: false },
          max: 0,
        },
      ],
      series: [
        {
          name: '策略净值',
          type: 'line',
          data: navData,
          showSymbol: false,
          lineStyle: { width: 1.8, color: c.primary },
          itemStyle: { color: c.primary },
          areaStyle: { color: 'rgba(24,95,165,0.06)' },
        },
        {
          name: '沪深300',
          type: 'line',
          data: benchData,
          showSymbol: false,
          lineStyle: { width: 1.2, color: c.axis, type: 'dashed' },
          itemStyle: { color: c.axis },
        },
        {
          name: '回撤',
          type: 'line',
          yAxisIndex: 1,
          data: ddData,
          showSymbol: false,
          lineStyle: { width: 0 },
          areaStyle: { color: 'rgba(214,69,69,0.14)' },
          silent: true,
        },
      ],
    });
    // IS / OOS 分界线
    if (period.oos_start) {
      navChart.setOption({
        series: [
          {},
          {},
          {
            markLine: {
              symbol: 'none',
              label: { formatter: 'OOS 起点', color: c.text, fontSize: 11 },
              lineStyle: { color: c.axis, type: 'dashed' },
              data: [{ xAxis: period.oos_start }],
            },
          },
        ],
      });
    }
  }

  /* ---------------- 详情：年度收益 ---------------- */

  function renderYearChart(payload) {
    const dom = el('chart-year');
    const rows = (payload.equity || []).filter((r) => r && r.date && r.nav != null);
    const c = App.chartColors();
    if (!rows.length) {
      dom.innerHTML = App.empty('无净值数据', '');
      return;
    }
    const byYear = new Map();
    rows.forEach((r) => {
      const y = String(r.date).slice(0, 4);
      if (!byYear.has(y)) byYear.set(y, []);
      byYear.get(y).push(Number(r.nav));
    });
    const years = [...byYear.keys()].sort();
    const values = [];
    let prev = null;
    years.forEach((y) => {
      const arr = byYear.get(y);
      const last = arr[arr.length - 1];
      const first = arr[0];
      const startBase = prev != null ? prev : first;
      values.push(Number((((last / startBase) - 1) * 100).toFixed(2)));
      prev = last;
    });
    dom.innerHTML = '';
    if (!yearChart) yearChart = echarts.init(dom);
    yearChart.setOption({
      grid: { left: 48, right: 16, top: 16, bottom: 28 },
      tooltip: { trigger: 'axis', valueFormatter: (v) => (v == null ? '—' : v + '%') },
      xAxis: {
        type: 'category',
        data: years,
        axisLine: { lineStyle: { color: '#e5e7eb' } },
        axisLabel: { color: c.axis, fontSize: 11 },
      },
      yAxis: {
        type: 'value',
        axisLabel: { color: c.axis, fontSize: 11, formatter: '{value}%' },
        splitLine: { lineStyle: { color: c.split } },
      },
      series: [
        {
          type: 'bar',
          data: values.map((v) => ({ value: v, itemStyle: { color: v >= 0 ? c.up : c.down } })),
          barWidth: 26,
          label: { show: true, position: 'top', color: c.text, fontSize: 11, formatter: '{c}%' },
        },
      ],
    });
  }

  /* ---------------- 详情：IS / OOS 表 ---------------- */

  function renderIsoos(payload) {
    const m = payload.metrics || {};
    const rows = [
      ['总收益', 'total_return', (v) => fmtSigned(v)],
      ['年化收益', 'annual_return', (v) => fmtSigned(v)],
      ['年化波动', 'annual_vol', (v) => (v == null ? '—' : v.toFixed(2) + '%')],
      ['夏普', 'sharpe', (v) => numText(v, 3)],
      ['索提诺', 'sortino', (v) => numText(v, 3)],
      ['最大回撤', 'max_drawdown', (v) => fmtSigned(v)],
      ['卡玛', 'calmar', (v) => numText(v, 3)],
      ['月胜率', 'month_win_rate', (v) => (v == null ? '—' : v.toFixed(1) + '%')],
      ['交易日', 'days', (v) => (v == null ? '—' : v)],
    ];
    el('bt-isoos-body').innerHTML = rows
      .map(([label, key, fmt]) => {
        const full = (m.full || {})[key];
        const is = (m.is || {})[key];
        const oos = (m.oos || {})[key];
        return `<tr>
          <td>${App.esc(label)}</td>
          <td class="right mono">${App.esc(fmt(full))}</td>
          <td class="right mono">${App.esc(fmt(is))}</td>
          <td class="right mono ${oos == null ? '' : toneOf(oos)}">${App.esc(fmt(oos))}</td>
        </tr>`;
      })
      .join('');
  }

  /* ---------------- 详情：参数网格热力图 ---------------- */

  function heatColor(value, lo, hi) {
    if (value === null || value === undefined || !isFinite(Number(value))) return '#f3f4f6';
    const t = Math.max(0, Math.min(1, (Number(value) - lo) / ((hi - lo) || 1)));
    const mid = [243, 244, 246];
    const edge = t < 0.5 ? [46, 158, 107] : [214, 69, 69];
    const k = t < 0.5 ? t / 0.5 : (t - 0.5) / 0.5;
    const rgb = mid.map((m, i) => Math.round(m + (edge[i] - m) * k));
    return `rgb(${rgb.join(',')})`;
  }

  function renderGridHeat(payload) {
    const box = el('bt-grid-heat');
    const records = payload.grid || [];
    if (!records.length) {
      box.innerHTML = App.empty(
        '本次回测没有参数网格',
        '网格默认开启（32 组 top_n × 调仓间隔 × 择时）；主回测太慢时会被自动跳过'
      );
      return;
    }
    const sharpes = records
      .map((r) => r.oos_sharpe)
      .filter((v) => v !== null && v !== undefined && isFinite(Number(v)))
      .map(Number);
    const lo = sharpes.length ? Math.min(...sharpes) : 0;
    const hi = sharpes.length ? Math.max(...sharpes) : 1;
    const topNs = [...new Set(records.map((r) => r.top_n))].sort((a, b) => a - b);
    const rebals = [...new Set(records.map((r) => r.rebal_days))].sort((a, b) => a - b);
    const bestIs = records.reduce(
      (acc, r) => (r.is_sharpe != null && (acc === null || r.is_sharpe > acc.is_sharpe) ? r : acc),
      null
    );

    const blocks = [true, false]
      .map((regime) => {
        const rows = records.filter((r) => Boolean(r.regime) === regime);
        if (!rows.length) return '';
        const head = `<tr><th></th>${rebals.map((d) => `<th>${d} 日</th>`).join('')}</tr>`;
        const body = topNs
          .map((n) => {
            const cells = rebals
              .map((d) => {
                const r = rows.find((x) => x.top_n === n && x.rebal_days === d);
                if (!r) return '<td>—</td>';
                const oos = r.oos_sharpe != null ? Number(r.oos_sharpe).toFixed(2) : '—';
                const is = r.is_sharpe != null ? Number(r.is_sharpe).toFixed(2) : '—';
                const title = `top_n=${n} 调仓=${d} 择时=${r.regime ? '开' : '关'}\n` +
                  `IS 夏普 ${is} / OOS 夏普 ${oos}\n` +
                  `IS 收益 ${r.is_total_return != null ? r.is_total_return + '%' : '—'} / ` +
                  `OOS 收益 ${r.oos_total_return != null ? r.oos_total_return + '%' : '—'}`;
                const isBest = bestIs && bestIs.top_n === n && bestIs.rebal_days === d;
                return `<td class="${isBest ? 'best' : ''}" title="${App.esc(title)}"
                          style="background:${heatColor(r.oos_sharpe, lo, hi)}">${oos}<span class="faint">(${is})</span></td>`;
              })
              .join('');
            return `<tr><th>${n} 只</th>${cells}</tr>`;
          })
          .join('');
        return `<div class="heat-block">
          <div class="heat-title">大盘择时：${regime ? '开' : '关'}</div>
          <table class="heat">${head}${body}</table>
        </div>`;
      })
      .join('');

    box.innerHTML = `<div class="heat-grid">${blocks}</div>
      <div class="heat-scale">
        <span>OOS 夏普 ${lo.toFixed(2)}</span>
        <span class="heat-bar" style="background:linear-gradient(90deg,#2e9e6b,#f3f4f6,#d64546)"></span>
        <span>${hi.toFixed(2)}</span>
        <span>· 共 ${records.length} 组</span>
      </div>`;
  }

  /* ---------------- 详情：持仓历史 ---------------- */

  function renderHoldings(payload) {
    const body = el('bt-holdings-body');
    const rows = payload.holdings || [];
    const sub = el('bt-holdings-sub');
    if (sub) sub.textContent = rows.length ? `${new Set(rows.map((r) => r.date)).size} 个调仓日` : '—';
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="3" style="padding:0">${App.empty('无持仓记录', '')}</td></tr>`;
      return;
    }
    const byDate = new Map();
    rows.forEach((r) => {
      if (!byDate.has(r.date)) byDate.set(r.date, []);
      byDate.get(r.date).push(r);
    });
    const dates = [...byDate.keys()].sort().reverse();
    body.innerHTML = dates
      .map((d) => {
        const items = byDate.get(d).slice().sort((a, b) => (b.amount || 0) - (a.amount || 0));
        const top = items
          .slice(0, 4)
          .map((it) => `${App.esc(it.code)} ${it.weight != null ? (it.weight * 100).toFixed(1) + '%' : ''}`)
          .join(' · ');
        return `<tr>
          <td class="mono">${App.esc(d)}</td>
          <td class="right mono">${items.length}</td>
          <td class="small muted">${top}${items.length > 4 ? ` · 等 ${items.length} 只` : ''}</td>
        </tr>`;
      })
      .join('');
  }

  /* ---------------- 详情：交易明细 ---------------- */

  function renderTrades(payload) {
    const body = el('bt-trades-body');
    const sub = el('bt-trades-sub');
    const rows = payload.trades || [];
    if (sub) sub.textContent = rows.length ? `共 ${rows.length} 笔` : '—';
    if (!tradeShown) tradeShown = Math.min(TRADE_PAGE, rows.length);
    const shown = rows.slice(0, tradeShown);
    if (!shown.length) {
      body.innerHTML = `<tr><td colspan="6" style="padding:0">${App.empty('无成交', '')}</td></tr>`;
      return;
    }
    body.innerHTML = shown
      .map((t) => `<tr>
        <td class="mono">${App.esc(t.date)}</td>
        <td class="mono">${App.esc(t.code)}</td>
        <td class="${t.side === 'buy' ? 'up' : 'down'}">${t.side === 'buy' ? '买入' : '卖出'}</td>
        <td class="right mono">${t.price != null ? Number(t.price).toFixed(2) : '—'}</td>
        <td class="right mono">${App.num(t.shares, 0)}</td>
        <td class="right mono">${App.num(t.amount, 2)}</td>
      </tr>`)
      .join('');
    const more = el('bt-trades-more');
    if (more) more.hidden = tradeShown >= rows.length;
  }

  function moreTrades() {
    if (!detail) return;
    tradeShown += TRADE_PAGE;
    renderTrades(detail);
  }

  /* ---------------- 历史对比 ---------------- */

  function onPick(node) {
    const id = node.dataset.id;
    if (node.checked) {
      if (picked.length >= 2) {
        const drop = picked.shift();
        const old = document.querySelector(`input[data-id="${CSS.escape(drop)}"]`);
        if (old) old.checked = false;
      }
      picked.push(id);
    } else {
      const at = picked.indexOf(id);
      if (at >= 0) picked.splice(at, 1);
    }
    syncCompareBar();
  }

  function toggleAll(node) {
    document.querySelectorAll('#bt-body input[type="checkbox"]').forEach((cb) => {
      cb.checked = false;
    });
    picked.length = 0;
    if (node.checked) {
      const first = document.querySelector('#bt-body input[type="checkbox"]');
      if (first) {
        first.checked = true;
        picked.push(first.dataset.id);
      }
      node.checked = false;
    }
    syncCompareBar();
  }

  function syncCompareBar() {
    const bar = el('bt-compare-bar');
    const btn = el('bt-compare-btn');
    const hint = el('bt-compare-hint');
    if (!bar) return;
    if (!picked.length) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;
    if (hint) hint.textContent = `已选 ${picked.length}/2${picked.length === 1 ? '，再选 1 条' : ''}`;
    if (btn) btn.disabled = picked.length !== 2;
  }

  function closeCompare() {
    const box = el('bt-compare');
    if (box) box.hidden = true;
  }

  async function compareSelected() {
    if (picked.length !== 2) return;
    const load = async (id) => {
      if (cache.has(id)) return cache.get(id);
      const data = await App.get('/api/backtests/' + encodeURIComponent(id));
      cache.set(id, data);
      return data;
    };
    try {
      const [a, b] = await Promise.all([load(picked[0]), load(picked[1])]);
      renderCompare(a, b);
    } catch (err) {
      App.toast('读取对比数据失败：' + err.message, 'error');
    }
  }

  function renderCompare(a, b) {
    const box = el('bt-compare');
    if (!box) return;
    box.hidden = false;
    el('bt-compare-sub').textContent = `${a.id} vs ${b.id}`;
    el('bt-cmp-a').textContent = a.id;
    el('bt-cmp-b').textContent = b.id;

    const series = [a, b].map((item, idx) => {
      const rows = (item.equity || []).filter((r) => r && r.date && r.nav != null);
      const base = rows.length ? rows[0].nav : 1;
      return {
        name: item.id,
        type: 'line',
        showSymbol: false,
        smooth: false,
        lineStyle: { width: idx === 0 ? 2 : 1.6, type: idx === 0 ? 'solid' : 'dashed' },
        data: rows.map((r) => [r.date, Number(((r.nav / base) * 100).toFixed(4))]),
      };
    });
    const dom = el('chart-compare');
    const c = App.chartColors();
    dom.innerHTML = '';
    if (!cmpChart) cmpChart = echarts.init(dom);
    cmpChart.setOption({
      grid: { left: 48, right: 16, top: 28, bottom: 32 },
      tooltip: { trigger: 'axis', valueFormatter: (v) => (v == null ? '—' : Number(v).toFixed(2)) },
      legend: { top: 0, textStyle: { color: c.text, fontSize: 12 } },
      xAxis: {
        type: 'time',
        axisLine: { lineStyle: { color: '#e5e7eb' } },
        axisLabel: { color: c.axis, fontSize: 11 },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { color: c.axis, fontSize: 11 },
        splitLine: { lineStyle: { color: c.split } },
      },
      series: series.map((s, i) => ({
        ...s,
        itemStyle: { color: i === 0 ? c.primary : c.up },
        lineStyle: { width: i === 0 ? 2 : 1.6, color: i === 0 ? c.primary : c.up },
      })),
    });

    const keys = [
      ['总收益', 'total_return', (v) => fmtSigned(v)],
      ['年化收益', 'annual_return', (v) => fmtSigned(v)],
      ['夏普', 'sharpe', (v) => numText(v, 3)],
      ['最大回撤', 'max_drawdown', (v) => fmtSigned(v)],
      ['超额收益', 'excess_return', (v) => fmtSigned(v)],
      ['月胜率', 'month_win_rate', (v) => (v == null ? '—' : v.toFixed(1) + '%')],
      ['平均持仓', 'avg_positions', (v) => (v == null ? '—' : Number(v).toFixed(2))],
      ['调仓间隔', null, null],
      ['持仓只数', null, null],
    ];
    const cell = (item, [label, key, fmt]) => {
      if (key === null) {
        if (label === '调仓间隔') return `${item.params?.rebal_days ?? '—'} 交易日`;
        return `${item.params?.top_n ?? '—'} 只`;
      }
      const src = key === 'avg_positions' ? item : (item.metrics || {}).full || {};
      return fmt(src[key]);
    };
    el('bt-compare-body').innerHTML = keys
      .map(([label, key]) => `<tr>
        <td>${App.esc(label)}</td>
        <td class="right mono">${App.esc(cell(a, [label, key]))}</td>
        <td class="right mono">${App.esc(cell(b, [label, key]))}</td>
      </tr>`)
      .join('');
    box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  window.addEventListener('resize', () => {
    if (navChart) navChart.resize();
    if (yearChart) yearChart.resize();
    if (cmpChart) cmpChart.resize();
  });

  document.addEventListener('DOMContentLoaded', reload);

  return {
    reload, submit, remove,
    openDetail, closeDetail, moreTrades,
    onPick, toggleAll, compareSelected, closeCompare,
  };
})();
