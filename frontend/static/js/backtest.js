/* 回测中心：参数表单、记录列表、指标展示 */
const BacktestPage = (() => {
  const el = (id) => document.getElementById(id);

  function fmtSigned(value, digits = 2) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
    const n = Number(value);
    return (n > 0 ? '+' : '') + n.toFixed(digits) + '%';
  }

  function toneOf(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return 'faint';
    return Number(value) >= 0 ? 'up' : 'down';
  }

  function renderRows(items) {
    const body = el('bt-body');
    const counter = el('bt-count');
    if (counter) counter.textContent = items.length ? `共 ${items.length} 条` : '暂无记录';
    if (!body) return;
    if (!items.length) {
      body.innerHTML = `<tr><td colspan="9" style="padding:0">${App.empty(
        '尚无回测记录',
        '回测引擎在 P3 阶段接入，跑通后会在此列出'
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
        return `<tr>
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
          <td class="right">
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
        body.innerHTML = `<tr><td colspan="9" style="padding:0">${App.empty(
          '加载失败',
          err.message
        )}</td></tr>`;
      }
    }
  }

  async function submit(event) {
    event.preventDefault();
    const btn = el('bt-run');
    const form = el('bt-form');
    const payload = {};
    new FormData(form).forEach((value, key) => {
      payload[key] = value === '' ? null : value;
    });
    for (const key of ['top_n', 'rebal_days', 'initial_cash']) {
      if (payload[key] !== null) payload[key] = Number(payload[key]);
    }
    for (const key of ['max_weight']) {
      if (payload[key] !== null) payload[key] = Number(payload[key]);
    }

    if (btn) btn.disabled = true;
    try {
      const res = await App.post('/api/backtests', payload);
      App.toast('回测已提交：' + (res.id || ''));
      await reload();
    } catch (err) {
      App.toast(err.message, 'error');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function remove(id) {
    if (!window.confirm(`确认删除回测 ${id}？该操作不可恢复。`)) return;
    try {
      await App.del('/api/backtests/' + encodeURIComponent(id));
      App.toast('已删除 ' + id);
      await reload();
    } catch (err) {
      App.toast('删除失败：' + err.message, 'error');
    }
  }

  document.addEventListener('DOMContentLoaded', reload);

  return { reload, submit, remove };
})();
