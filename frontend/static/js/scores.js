/* 每日打分页：榜单 + 行业聚合 + 个股六维抽屉 */
const ScorePage = (() => {
  const state = {
    items: [],
    industries: [],
    meta: {},
    keyword: '',
    industry: '',
    grade: '',
    versionsLoaded: false,
  };

  const el = (id) => document.getElementById(id);

  function renderStats(items, meta) {
    const count = items.length;
    const scores = items.map((r) => Number(r.score_pct)).filter((v) => !Number.isNaN(v));
    const avg = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : null;
    const bull = items.filter((r) => r.grade === '强烈看多' || r.grade === '看多').length;
    const bear = items.filter((r) => r.grade === '看空' || r.grade === '强烈看空').length;

    el('s-count').textContent = App.num(count, 0);
    el('s-avg').textContent = avg === null ? '—' : App.num(avg, 1);
    el('s-bull').textContent = App.num(bull, 0);
    el('s-bear').textContent = App.num(bear, 0);
    el('s-version-hint').textContent = meta.version
      ? `${meta.version}${meta.date ? ' · ' + meta.date : ''}`
      : '尚无打分版本';
  }

  function renderListSub(rows) {
    const node = el('s-list-sub');
    if (!node) return;
    if (!state.items.length) {
      node.textContent = '—';
      return;
    }
    node.textContent =
      rows.length === state.items.length
        ? `共 ${rows.length} 只`
        : `筛选 ${rows.length} / ${state.items.length} 只`;
  }

  function filteredItems() {
    const kw = state.keyword.trim().toLowerCase();
    return state.items.filter((row) => {
      if (state.industry && row.industry !== state.industry) return false;
      if (state.grade && row.grade !== state.grade) return false;
      if (!kw) return true;
      return (
        String(row.code || '').toLowerCase().includes(kw) ||
        String(row.name || '').toLowerCase().includes(kw)
      );
    });
  }

  function renderTable() {
    const body = el('score-body');
    if (!body) return;
    if (!state.items.length) {
      body.innerHTML = `<tr><td colspan="8" style="padding:0">${App.empty(
        '尚无打分结果',
        '请先完成数据回填并执行打分任务（P2）'
      )}</td></tr>`;
      renderListSub([]);
      return;
    }
    const rows = filteredItems();
    renderListSub(rows);
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="8" class="muted" style="padding:24px;text-align:center">没有匹配的标的</td></tr>`;
      return;
    }
    body.innerHTML = rows
      .map((row, idx) => {
        const score = Number(row.score_pct);
        return `<tr class="clickable" onclick="ScorePage.openDetail('${App.esc(row.code)}')">
          <td class="right mono faint">${idx + 1}</td>
          <td class="mono">${App.esc(row.code)}</td>
          <td>${App.esc(row.name || '—')}</td>
          <td class="small">${App.esc(row.industry || '—')}</td>
          <td class="right mono strong">${Number.isNaN(score) ? '—' : score.toFixed(1)}</td>
          <td class="right mono faint">${
            row.score_abs === null || row.score_abs === undefined ? '—' : Number(row.score_abs).toFixed(1)
          }</td>
          <td>${App.badge(row.grade)}</td>
          <td>${App.dimBars(row)}</td>
        </tr>`;
      })
      .join('');
  }

  function renderIndustries(items) {
    const body = el('industry-body');
    if (!body) return;
    if (!items || !items.length) {
      body.innerHTML = `<tr><td colspan="4" class="muted" style="padding:24px;text-align:center">暂无行业数据</td></tr>`;
      return;
    }
    body.innerHTML = items
      .map(
        (row) => `<tr>
          <td class="small">${App.esc(row.industry || '—')}</td>
          <td class="right mono">${App.num(row.count, 0)}</td>
          <td class="right mono strong">${App.num(row.avg_score, 1)}</td>
          <td class="right mono">${App.num((row.strong_buy || 0) + (row.buy || 0), 0)}</td>
        </tr>`
      )
      .join('');
  }

  function renderIndustryOptions() {
    const select = el('score-industry');
    if (!select) return;
    const current = select.value;
    const industries = Array.from(
      new Set(state.items.map((r) => r.industry).filter(Boolean))
    ).sort((a, b) => a.localeCompare(b, 'zh-CN'));
    select.innerHTML =
      '<option value="">全部行业</option>' +
      industries.map((n) => `<option value="${App.esc(n)}">${App.esc(n)}</option>`).join('');
    if (industries.includes(current)) select.value = current;
  }

  async function loadVersions() {
    try {
      const data = await App.get('/api/score-versions');
      const select = el('score-version');
      if (!select) return;
      const items = data.items || [];
      if (!items.length) {
        select.innerHTML = '<option value="">无版本</option>';
        return;
      }
      select.innerHTML = items
        .map(
          (v) =>
            `<option value="${App.esc(v.version)}">${App.esc(v.version)}（${v.dates} 日）</option>`
        )
        .join('');
      state.versionsLoaded = true;
    } catch (err) {
      /* 版本列表失败不阻塞主流程 */
    }
  }

  function currentQuery() {
    const version = el('score-version') ? el('score-version').value : '';
    const date = el('score-date') ? el('score-date').value : '';
    const query = {};
    if (version) query.version = version;
    if (date) query.date = date;
    return query;
  }

  async function reload() {
    try {
      if (!state.versionsLoaded) await loadVersions();
      const query = currentQuery();
      const [list, industries] = await Promise.all([
        App.get('/api/scores', query),
        App.get('/api/scores/industries', query).catch(() => ({ items: [] })),
      ]);

      state.items = list.items || [];
      state.meta = list.meta || {};
      renderStats(state.items, state.meta);
      renderIndustryOptions();
      renderTable();
      renderIndustries(industries.items || []);

      const dateInput = el('score-date');
      if (dateInput && !dateInput.value && state.meta.date) dateInput.value = state.meta.date;
      const versionSelect = el('score-version');
      if (versionSelect && !versionSelect.value && state.meta.version) {
        versionSelect.value = state.meta.version;
      }

      if (list.note) App.toast(list.note);
    } catch (err) {
      App.toast('打分数据加载失败：' + err.message, 'error');
      const body = el('score-body');
      if (body) {
        body.innerHTML = `<tr><td colspan="8" style="padding:0">${App.empty(
          '加载失败',
          err.message
        )}</td></tr>`;
      }
    }
  }

  function applyFilter() {
    state.keyword = el('score-search') ? el('score-search').value : '';
    state.industry = el('score-industry') ? el('score-industry').value : '';
    state.grade = el('score-grade') ? el('score-grade').value : '';
    renderTable();
  }

  const DIMS = [
    ['trend', '趋势'],
    ['momentum', '动量'],
    ['volume', '量能'],
    ['risk', '风险'],
    ['valuation', '估值'],
    ['quality', '质量'],
  ];

  function dimRows(latest) {
    return DIMS.filter(
      ([key]) => latest['dim_' + key] !== undefined && latest['dim_' + key] !== null
    )
      .map(([key, label]) => {
        const value = Math.max(0, Math.min(100, Number(latest['dim_' + key]) || 0));
        return `<div class="dim-row">
          <span class="dim-label">${label}</span>
          <span class="dim-track"><i style="width:${value}%"></i></span>
          <span class="dim-value mono">${value.toFixed(1)}</span>
        </div>`;
      })
      .join('');
  }

  function indicatorRows(ind) {
    const val = ind.valuation || {};
    const fund = ind.fundamental || {};
    const atr = ind.atr || {};
    const rows = [
      ['PE(TTM)', val.pe_ttm != null ? Number(val.pe_ttm).toFixed(2) : '—', '估值'],
      ['PB(MRQ)', val.pb_mrq != null ? Number(val.pb_mrq).toFixed(2) : '—', '估值'],
      ['PS(TTM)', val.ps_ttm != null ? Number(val.ps_ttm).toFixed(2) : '—', '估值'],
      ['总市值', val.total_mv != null ? (Number(val.total_mv) / 1e8).toFixed(1) + ' 亿' : '—', '估值'],
      ['ROE(加权)', fund.roe_wa != null ? Number(fund.roe_wa).toFixed(2) + '%' : '—', fund.report_date || '财务'],
      ['营收同比', fund.rev_yoy != null ? Number(fund.rev_yoy).toFixed(2) + '%' : '—', fund.report_date || '财务'],
      ['净利同比', fund.profit_yoy != null ? Number(fund.profit_yoy).toFixed(2) + '%' : '—', fund.report_date || '财务'],
      ['毛利率', fund.gross_margin != null ? Number(fund.gross_margin).toFixed(2) + '%' : '—', fund.report_date || '财务'],
      ['股息率', fund.div_yield != null ? Number(fund.div_yield).toFixed(2) + '%' : '—', fund.report_date || '财务'],
      ['ATR%', atr.atr_pct != null ? Number(atr.atr_pct).toFixed(2) + '%' : '—', '近 20 日均真实波幅'],
    ];
    return rows
      .map(
        ([label, value, hint]) => `<div class="metric">
          <span class="metric-label">${App.esc(label)}</span>
          <span class="metric-value" style="font-size:16px">${App.esc(value)}</span>
          <span class="metric-hint">${App.esc(hint)}</span>
        </div>`
      )
      .join('');
  }

  async function openDetail(code) {
    App.openDrawer(code, '<div class="faint small">加载中…</div>');
    try {
      const query = currentQuery();
      const data = await App.get('/api/scores/' + encodeURIComponent(code), query);
      const latest = data.latest || {};
      const meta = data.meta || {};
      const ind = data.indicators || {};
      const title = `${code} ${meta.name || ''}`.trim();
      document.getElementById('drawer-title').textContent = title;

      document.getElementById('drawer-body').innerHTML = `
        <div class="drawer-summary">
          <div>
            <div class="stat-label">综合分</div>
            <div class="stat-value">${App.num(latest.score_pct, 1)}</div>
          </div>
          <div>
            <div class="stat-label">评级</div>
            <div style="margin-top:6px">${App.badge(latest.grade)}</div>
          </div>
          <div>
            <div class="stat-label">绝对分</div>
            <div class="stat-value">${App.num(latest.score_abs, 1)}</div>
          </div>
          <div>
            <div class="stat-label">交易日</div>
            <div class="stat-value" style="font-size:16px">${App.shortDate(latest.trade_date)}</div>
          </div>
        </div>
        <h3 class="drawer-h3">六维雷达</h3>
        <div id="drawer-radar" class="chart radar"></div>
        <h3 class="drawer-h3">维度得分</h3>
        <div class="dim-list">${dimRows(latest) || '<div class="faint small">该版本仅输出综合分</div>'}</div>
        <h3 class="drawer-h3">关键指标</h3>
        <div class="grid grid-3" style="gap:10px">${indicatorRows(ind)}</div>
        <h3 class="drawer-h3">K线（近 120 个交易日，前复权）</h3>
        <div id="drawer-kline" class="chart"></div>
        <h3 class="drawer-h3">评分历史（近 250 个交易日）</h3>
        <div id="drawer-chart" class="chart sm"></div>
        <h3 class="drawer-h3">基础信息</h3>
        <table class="kv">
          <tr><th>代码</th><td class="mono">${App.esc(code)}</td></tr>
          <tr><th>名称</th><td>${App.esc(meta.name || '—')}</td></tr>
          <tr><th>行业</th><td>${App.esc(meta.industry_l1 || '—')}</td></tr>
          <tr><th>板块</th><td>${App.esc(meta.board || '—')}</td></tr>
          <tr><th>上市日</th><td class="mono">${App.shortDate(meta.list_date)}</td></tr>
          <tr><th>版本</th><td class="mono">${App.esc(data.version || '—')}</td></tr>
        </table>`;

      drawRadar(latest);
      drawKline(data.kline || []);
      drawHistory(data.history || []);
    } catch (err) {
      document.getElementById('drawer-body').innerHTML = App.empty('加载失败', err.message);
    }
  }

  function drawRadar(latest) {
    const node = document.getElementById('drawer-radar');
    if (!node || typeof echarts === 'undefined') return;
    const present = DIMS.filter(
      ([key]) => latest['dim_' + key] !== undefined && latest['dim_' + key] !== null
    );
    if (!present.length) {
      node.innerHTML = '<div class="faint small" style="padding:16px">该版本未输出维度得分</div>';
      return;
    }
    const colors = App.chartColors();
    const chart = echarts.init(node);
    chart.setOption({
      tooltip: {},
      radar: {
        center: ['50%', '52%'],
        radius: '66%',
        indicator: present.map(([, label]) => ({ name: label, max: 100 })),
        axisName: { color: colors.text, fontSize: 11 },
        splitLine: { lineStyle: { color: colors.split } },
        splitArea: { areaStyle: { color: ['#ffffff', '#fafbfc'] } },
        axisLine: { lineStyle: { color: colors.split } },
      },
      series: [
        {
          type: 'radar',
          data: [
            {
              value: present.map(([key]) => Math.max(0, Math.min(100, Number(latest['dim_' + key]) || 0))),
              name: '维度得分',
              areaStyle: { color: 'rgba(24,95,165,0.18)' },
              lineStyle: { color: colors.primary, width: 1.6 },
              itemStyle: { color: colors.primary },
            },
          ],
        },
      ],
    });
    window.addEventListener('resize', () => chart.resize());
  }

  function drawKline(bars) {
    const node = document.getElementById('drawer-kline');
    if (!node || typeof echarts === 'undefined') return;
    if (!bars.length) {
      node.innerHTML = '<div class="faint small" style="padding:16px">暂无行情数据</div>';
      return;
    }
    const colors = App.chartColors();
    // 红涨绿跌
    const upColor = '#d64545';
    const downColor = '#2e9e6b';
    const chart = echarts.init(node);
    chart.setOption({
      grid: [
        { left: 48, right: 12, top: 16, height: 150 },
        { left: 48, right: 12, top: 180, height: 46 },
      ],
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        valueFormatter: (v) => (v == null ? '—' : Number(v).toFixed(2)),
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      xAxis: [
        {
          type: 'category',
          data: bars.map((b) => b.date),
          boundaryGap: true,
          axisLine: { lineStyle: { color: colors.split } },
          axisLabel: { show: false },
          axisTick: { show: false },
        },
        {
          type: 'category',
          gridIndex: 1,
          data: bars.map((b) => b.date),
          boundaryGap: true,
          axisLine: { lineStyle: { color: colors.split } },
          axisLabel: { color: colors.text, fontSize: 10 },
          axisTick: { show: false },
        },
      ],
      yAxis: [
        {
          scale: true,
          splitLine: { lineStyle: { color: colors.split } },
          axisLabel: { color: colors.text, fontSize: 11 },
        },
        {
          gridIndex: 1,
          splitNumber: 2,
          splitLine: { show: false },
          axisLabel: { show: false },
          axisLine: { show: false },
          axisTick: { show: false },
        },
      ],
      dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 }],
      series: [
        {
          name: 'K线',
          type: 'candlestick',
          data: bars.map((b) => [b.open, b.close, b.low, b.high]),
          itemStyle: {
            color: upColor,
            color0: downColor,
            borderColor: upColor,
            borderColor0: downColor,
          },
        },
        {
          name: '成交量',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: bars.map((b) => ({
            value: b.volume,
            itemStyle: { color: b.close >= b.open ? 'rgba(214,69,69,0.5)' : 'rgba(46,158,107,0.5)' },
          })),
        },
      ],
    });
    window.addEventListener('resize', () => chart.resize());
  }

  function drawHistory(history) {
    const node = document.getElementById('drawer-chart');
    if (!node) return;
    if (!history.length || typeof echarts === 'undefined') {
      if (node) node.innerHTML = '<div class="faint small" style="padding:16px">暂无历史</div>';
      return;
    }
    const colors = App.chartColors();
    const chart = echarts.init(node);
    chart.setOption({
      grid: { left: 40, right: 16, top: 16, bottom: 28 },
      tooltip: { trigger: 'axis' },
      xAxis: {
        type: 'category',
        data: history.map((h) => h.date),
        axisLine: { lineStyle: { color: colors.split } },
        axisLabel: { color: colors.text, fontSize: 11 },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: 100,
        splitLine: { lineStyle: { color: colors.split } },
        axisLabel: { color: colors.text, fontSize: 11 },
      },
      series: [
        {
          type: 'line',
          data: history.map((h) => Number(h.score_pct)),
          showSymbol: false,
          lineStyle: { width: 1.6, color: colors.primary },
          areaStyle: { color: 'rgba(24,95,165,0.08)' },
        },
      ],
    });
    window.addEventListener('resize', () => chart.resize());
  }

  document.addEventListener('DOMContentLoaded', reload);

  return { reload, applyFilter, openDetail };
})();
