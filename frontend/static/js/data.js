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
      renderIndustryOptions();
      renderSymbols();
      renderJobs(jobs.items);
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

  function filter() {
    const search = el('symbol-search');
    const industry = el('symbol-industry');
    state.keyword = search ? search.value : '';
    state.industry = industry ? industry.value : '';
    renderSymbols();
  }

  async function triggerUpdate() {
    const btn = el('btn-update');
    if (btn) btn.disabled = true;
    try {
      const res = await App.post('/api/jobs', { kind: 'update', params: {} });
      App.toast(`已创建更新任务 ${res.job_id}（执行器将在 P1 接入）`);
      const jobs = await App.get('/api/jobs', { limit: 20 }).catch(() => ({ items: [] }));
      renderJobs(jobs.items);
    } catch (err) {
      App.toast('创建任务失败：' + err.message, 'error');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  document.addEventListener('DOMContentLoaded', reload);

  return { reload, filter, triggerUpdate };
})();
