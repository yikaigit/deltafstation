/* 数据管理页：数据集覆盖、标的明细、任务记录 */
const DataPage = (() => {
  const DATASET_LABEL = {
    bars: '日线行情',
    valuation: '估值指标',
    adj_factor: '复权因子',
    fundamental: '财务报表',
    universe: '标的清单',
    calendar: '交易日历',
  };

  const state = {
    symbols: [],
    datasets: {},
    coverage: null,
    keyword: '',
    industry: '',
    loaded: false,
  };

  const el = (id) => document.getElementById(id);

  function datasetChip(name, info) {
    const label = DATASET_LABEL[name] || name;
    const ready = info && info.exists && (info.rows === undefined || info.rows > 0);
    const cls = ready ? 'chip ok' : info && info.exists ? 'chip warn' : 'chip';
    const rows = info && info.rows !== undefined ? `${App.num(info.rows, 0)} 行` : '未建立';
    const range =
      info && info.first_date && info.last_date
        ? `<span class="chip-sub">${App.esc(info.first_date)} ~ ${App.esc(info.last_date)}</span>`
        : '';
    return `<span class="${cls}" title="${App.esc(name)}">
      <span class="chip-dot"></span>${App.esc(label)}
      <span class="chip-sub">${App.esc(rows)}</span>${range}
    </span>`;
  }

  function renderDatasets(datasets) {
    const node = el('dataset-chips');
    if (!node) return;
    const names = Object.keys(DATASET_LABEL).filter((n) => datasets[n] !== undefined);
    const extra = Object.keys(datasets).filter((n) => !DATASET_LABEL[n]);
    const all = names.concat(extra);
    if (!all.length) {
      node.innerHTML = '<span class="faint small">尚无数据集</span>';
      return;
    }
    node.innerHTML = all.map((n) => datasetChip(n, datasets[n])).join('');
  }

  function renderStats(payload) {
    const symbols = payload.symbols || [];
    const datasets = payload.datasets || {};
    const bars = datasets.bars || {};

    el('d-symbols').textContent = App.num(payload.symbol_count || symbols.length, 0);
    el('d-rows').textContent = bars.rows !== undefined ? App.num(bars.rows, 0) : '—';
    el('d-range').textContent =
      bars.first_date && bars.last_date ? `${bars.first_date} ~ ${bars.last_date}` : '—';

    const total = Object.keys(datasets).length;
    const ready = Object.values(datasets).filter(
      (d) => d && d.exists && (d.rows === undefined || d.rows > 0)
    ).length;
    el('d-ready').textContent = total ? `${ready} / ${total}` : '—';
    el('d-ready-hint').textContent = `共 ${total} 个数据集`;

    const stamp = new Date();
    el('d-updated').textContent = `刷新于 ${stamp.toLocaleTimeString('zh-CN', { hour12: false })}`;
  }

  function statusOf(row) {
    if (!row.last_date) return { text: '无数据', cls: 'badge neutral' };
    if (!row.rows) return { text: '无数据', cls: 'badge neutral' };
    const today = new Date();
    const last = new Date(row.last_date + 'T00:00:00');
    const lagDays = Math.round((today - last) / 86400000);
    if (lagDays <= 4) return { text: '最新', cls: 'badge strong_buy' };
    if (lagDays <= 15) return { text: `滞后 ${lagDays} 天`, cls: 'badge buy' };
    if (lagDays <= 60) return { text: `滞后 ${lagDays} 天`, cls: 'badge neutral' };
    return { text: '需更新', cls: 'badge sell' };
  }

  function filteredSymbols() {
    const kw = state.keyword.trim().toLowerCase();
    return state.symbols.filter((row) => {
      if (state.industry && row.industry !== state.industry) return false;
      if (!kw) return true;
      const code = String(row.code || '').toLowerCase();
      const name = String(row.name || '').toLowerCase();
      return code.includes(kw) || name.includes(kw);
    });
  }

  function renderSymbols() {
    const body = el('symbol-body');
    if (!body) return;
    const rows = filteredSymbols();
    const total = state.symbols.length;

    const counter = el('symbol-count');
    if (counter) {
      counter.textContent =
        rows.length === total ? `共 ${total} 只` : `筛选 ${rows.length} / ${total} 只`;
    }

    if (!total) {
      body.innerHTML = `<tr><td colspan="8" style="padding:0">${App.empty(
        '尚无标的行情',
        '请先执行数据回填（P1）：拉取沪深300 成分与历史日线'
      )}</td></tr>`;
      return;
    }
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="8" class="muted" style="padding:24px;text-align:center">没有匹配的标的</td></tr>`;
      return;
    }

    body.innerHTML = rows
      .map((row) => {
        const st = statusOf(row);
        return `<tr>
          <td class="mono">${App.esc(row.code)}</td>
          <td>${App.esc(row.name || '—')}</td>
          <td>${App.esc(row.board || '—')}</td>
          <td>${App.esc(row.industry || '—')}</td>
          <td class="mono">${App.shortDate(row.first_date)}</td>
          <td class="mono">${App.shortDate(row.last_date)}</td>
          <td class="right mono">${App.num(row.rows, 0)}</td>
          <td><span class="${st.cls}">${App.esc(st.text)}</span></td>
        </tr>`;
      })
      .join('');
  }

  function renderIndustryOptions() {
    const select = el('symbol-industry');
    if (!select) return;
    const current = select.value;
    const industries = Array.from(
      new Set(state.symbols.map((r) => r.industry).filter(Boolean))
    ).sort((a, b) => a.localeCompare(b, 'zh-CN'));
    select.innerHTML =
      '<option value="">全部行业</option>' +
      industries.map((n) => `<option value="${App.esc(n)}">${App.esc(n)}</option>`).join('');
    if (industries.includes(current)) select.value = current;
  }

  const JOB_STATUS = {
    pending: ['待执行', 'badge neutral'],
    running: ['执行中', 'badge buy'],
    success: ['成功', 'badge strong_buy'],
    failed: ['失败', 'badge sell'],
    cancelled: ['已取消', 'badge neutral'],
  };

  function renderJobs(items) {
    const body = el('job-body');
    if (!body) return;
    if (!items || !items.length) {
      body.innerHTML = `<tr><td colspan="6" class="muted" style="padding:24px;text-align:center">暂无任务记录</td></tr>`;
      return;
    }
    body.innerHTML = items
      .map((job) => {
        const st = JOB_STATUS[job.status] || [job.status || '—', 'badge neutral'];
        const progress =
          job.progress !== null && job.progress !== undefined
            ? App.pct(Number(job.progress) * 100, 0)
            : '—';
        return `<tr>
          <td class="mono small">${App.esc(job.job_id)}</td>
          <td>${App.esc(job.kind)}</td>
          <td><span class="${st[1]}">${App.esc(st[0])}</span></td>
          <td class="mono">${progress}</td>
          <td class="small">${App.esc(job.message || '—')}</td>
          <td class="mono small">${App.esc(job.created_at || '—')}</td>
        </tr>`;
      })
      .join('');
  }

  function renderQuality(quality) {
    const panel = el('quality-panel');
    const dateNode = el('q-date');
    if (!panel) return;
    if (!quality || !quality.available) {
      if (dateNode) dateNode.textContent = '—';
      panel.innerHTML = `<div class="faint small">尚未生成校验报告（回填或每日更新后自动产出）</div>`;
      return;
    }
    if (dateNode) dateNode.textContent = quality.trade_date || '';

    const degraded = Boolean(quality.degraded);
    const errorSymbols = Number(quality.error_symbols || 0);
    const warnSymbols = Number(quality.warn_symbols || 0);
    const ratio = Number(quality.error_ratio || 0) * 100;
    const tone = degraded ? 'sell' : errorSymbols ? 'neutral' : 'strong_buy';
    const label = degraded ? '已降级' : errorSymbols ? '有告警' : '正常';

    panel.innerHTML = `
      <div class="row" style="justify-content:space-between;align-items:center">
        <span class="badge ${tone}">${label}</span>
        <span class="faint small">ERROR 占比 ${ratio.toFixed(2)}%（门禁 2%）</span>
      </div>
      <table class="kv mt-8">
        <tr><th>校验标的</th><td class="mono">${App.num(quality.symbols, 0)} 只</td></tr>
        <tr><th>ERROR</th><td class="mono">${App.num(errorSymbols, 0)} 只</td></tr>
        <tr><th>WARN</th><td class="mono">${App.num(warnSymbols, 0)} 只</td></tr>
        <tr><th>报告文件</th><td class="mono small">${App.esc(quality.file || '—')}</td></tr>
      </table>`;
  }

  function renderCalendar(calendar) {
    const panel = el('calendar-panel');
    const rangeNode = el('cal-range');
    if (!panel) return;
    if (!calendar || !calendar.exists) {
      if (rangeNode) rangeNode.textContent = '—';
      panel.innerHTML = `<div class="faint small">尚未建立交易日历，请执行全量回填</div>`;
      return;
    }
    if (rangeNode) rangeNode.textContent = `${calendar.first_date || '—'} ~ ${calendar.last_date || '—'}`;
    panel.innerHTML = `
      <table class="kv">
        <tr><th>交易日</th><td class="mono">${App.num(calendar.trading_days, 0)} 天</td></tr>
        <tr><th>日历行数</th><td class="mono">${App.num(calendar.rows, 0)} 行</td></tr>
        <tr><th>起点</th><td class="mono">${App.esc(calendar.first_date || '—')}</td></tr>
        <tr><th>终点</th><td class="mono">${App.esc(calendar.last_date || '—')}</td></tr>
      </table>`;
  }

  /* ---------------- 覆盖矩阵 ---------------- */

  function coverColor(cell, capacity) {
    if (!cell) return '#f3f4f6';
    const t = Math.max(0, Math.min(1, capacity ? cell / capacity : 0));
    // 0 → 浅灰，1 → 深绿（数据全）
    const lo = [243, 244, 246];
    const hi = [46, 138, 96];
    const rgb = lo.map((l, i) => Math.round(l + (hi[i] - l) * t));
    return `rgb(${rgb.join(',')})`;
  }

  function renderCoverage() {
    const body = el('cover-body');
    const head = el('cover-table') ? el('cover-table').querySelector('thead tr') : null;
    if (!body || !state.coverage) return;
    const cov = state.coverage;
    const years = cov.years || [];
    if (!years.length) {
      body.innerHTML = `<tr><td class="muted" style="padding:24px;text-align:center">尚无覆盖数据</td></tr>`;
      return;
    }
    if (head) {
      head.innerHTML =
        '<th style="min-width:170px">标的</th>' +
        years.map((y) => `<th class="right">${App.esc(y)}</th>`).join('') +
        '<th class="right">合计</th>';
    }

    const kw = (el('cover-search') ? el('cover-search').value : '').trim().toLowerCase();
    const sort = el('cover-sort') ? el('cover-sort').value : 'default';
    let rows = (cov.symbols || []).filter((s) => {
      if (!kw) return true;
      return (
        String(s.code).toLowerCase().includes(kw) ||
        String(s.name || '').toLowerCase().includes(kw)
      );
    });

    const gapCount = (code) =>
      years.filter((y) => {
        const cell = ((cov.matrix || {})[code] || {})[y] || 0;
        const cap = (cov.year_days || {})[y] || 0;
        return cap > 0 && cell < cap * 0.95;
      }).length;

    if (sort === 'code') rows = rows.slice().sort((a, b) => a.code.localeCompare(b.code));
    else if (sort === 'gap') rows = rows.slice().sort((a, b) => gapCount(b.code) - gapCount(a.code));

    rows = rows.slice(0, 300);
    body.innerHTML = rows
      .map((sym) => {
        const map = (cov.matrix || {})[sym.code] || {};
        const cells = years
          .map((y) => {
            const cell = map[y] || 0;
            const cap = (cov.year_days || {})[y] || 0;
            const text = cell ? String(cell) : '·';
            return `<td style="background:${coverColor(cell, cap)};color:${
              cell && cap && cell / cap > 0.6 ? '#fff' : 'var(--text)'
            }" title="${App.esc(sym.code)} ${App.esc(y)}：${cell} 行 / 交易日 ${cap}">${text}</td>`;
          })
          .join('');
        return `<tr>
          <td class="nowrap small"><span class="mono">${App.esc(sym.code)}</span>
            <span class="muted">${App.esc(sym.name || '')}</span></td>
          ${cells}
          <td class="right mono small">${App.num(sym.total_days, 0)}</td>
        </tr>`;
      })
      .join('');
  }

  async function loadCoverage() {
    try {
      state.coverage = await App.get('/api/data/coverage');
      renderCoverage();
    } catch (err) {
      const body = el('cover-body');
      if (body) {
        body.innerHTML = `<tr><td class="muted" style="padding:24px;text-align:center">${App.esc(
          err.message
        )}</td></tr>`;
      }
    }
  }

  async function fillOne() {
    const codeInput = el('fill-code');
    const startInput = el('fill-start');
    const hint = el('fill-hint');
    const btn = el('btn-fill');
    const code = (codeInput ? codeInput.value : '').trim();
    if (!code) {
      App.toast('请先填写标的代码，如 sh.600000', 'error');
      return;
    }
    if (btn) btn.disabled = true;
    try {
      const payload = { code };
      if (startInput && startInput.value) payload.start = startInput.value;
      const res = await App.post('/api/data/fill', payload);
      App.toast('已提交补拉任务 ' + res.job_id);
      if (hint) hint.textContent = `任务 ${res.job_id} 已排队，进度见下方任务记录。`;
      const jobs = await App.get('/api/jobs', { limit: 20 }).catch(() => ({ items: [] }));
      renderJobs(jobs.items);
      schedulePoll(jobs.items);
    } catch (err) {
      App.toast('补拉失败：' + err.message, 'error');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function reload() {
    const btn = el('btn-refresh');
    if (btn) btn.disabled = true;
    try {
      const [status, jobs] = await Promise.all([
        App.get('/api/data/status'),
        App.get('/api/jobs', { limit: 20 }).catch(() => ({ items: [] })),
      ]);
      state.datasets = status.datasets || {};
      state.symbols = status.symbols || [];
      renderStats(status);
      renderDatasets(state.datasets);
      renderQuality(status.quality);
      renderCalendar(status.calendar);
      renderIndustryOptions();
      renderSymbols();
      renderJobs(jobs.items);
      schedulePoll(jobs.items);
      loadCoverage();
      state.loaded = true;
    } catch (err) {
      App.toast('数据状态加载失败：' + err.message, 'error');
      const body = el('symbol-body');
      if (body) {
        body.innerHTML = `<tr><td colspan="8" style="padding:0">${App.empty(
          '加载失败',
          err.message
        )}</td></tr>`;
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  /** 有任务在跑时自动轮询，让进度条动起来 */
  let pollTimer = null;
  function schedulePoll(items) {
    const active = (items || []).some((j) => j.status === 'pending' || j.status === 'running');
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
    if (!active) return;
    pollTimer = setTimeout(async () => {
      const jobs = await App.get('/api/jobs', { limit: 20 }).catch(() => null);
      if (jobs) {
        renderJobs(jobs.items);
        schedulePoll(jobs.items);
      }
    }, 3000);
  }

  function filter() {
    const search = el('symbol-search');
    const industry = el('symbol-industry');
    state.keyword = search ? search.value : '';
    state.industry = industry ? industry.value : '';
    renderSymbols();
  }

  async function submitJob(kind, params) {
    try {
      const res = await App.post('/api/jobs', { kind, params });
      App.toast(`已创建任务 ${res.job_id}`);
      const jobs = await App.get('/api/jobs', { limit: 20 }).catch(() => ({ items: [] }));
      renderJobs(jobs.items);
      schedulePoll(jobs.items);
      return res;
    } catch (err) {
      App.toast('创建任务失败：' + err.message, 'error');
      return null;
    }
  }

  async function triggerUpdate() {
    const btn = el('btn-update');
    if (btn) btn.disabled = true;
    try {
      await submitJob('update', {});
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function triggerBackfill() {
    const ok = window.confirm(
      '全量回填会拉取沪深300 全部历史行情、复权因子、近 5 年估值与财务数据。\n\n' +
        '首次运行约需 20~40 分钟（取决于网络），期间可继续使用其他页面。\n\n确认开始？'
    );
    if (!ok) return;
    const btn = el('btn-backfill');
    if (btn) btn.disabled = true;
    try {
      await submitJob('backfill', { start: '2008-01-01', valuation_years: 5 });
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  document.addEventListener('DOMContentLoaded', reload);

  return { reload, filter, triggerUpdate, triggerBackfill, renderCoverage, fillOne };
})();
