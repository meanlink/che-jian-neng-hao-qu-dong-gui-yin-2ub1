import {datasets, meters} from '../api/entities.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc, fmt, notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
import {parseCSV} from '../utils/csv.js';

const STATUS_LABEL = {draft: '待发布', published: '已发布', superseded: '已替代'};
const DAY_LABEL = {complete: '可发布完整日', gap: '跨日缺测（不均摊）', isolated: '隔离（回零/换表缺交接）', pending: '待补齐'};
const SEG_LABEL = {complete: '完整区间', gap: '缺测区间', isolated: '隔离区间'};
const canWrite = () => session.user.role !== 'viewer';

export async function meterPage() {
  const [status, batches, daily, datasetList] = await Promise.all([
    meters.status(), meters.batches(), meters.daily(), datasets.list()]);
  workspace_cache.status = status;
  workspace_cache.batches = batches;
  workspace_cache.daily = daily;
  workspace_cache.datasets = datasetList;
  return heading('02B / METER LEDGER', '电表累计读数核算与日耗追溯',
    '原始读数只追加不修改；跨日缺测不均摊，回零/换表缺交接即隔离。发布后整组受影响日一次性切换，每笔日耗都可追查两个读数依据。')
    + renderStatus(status)
    + '<div class="dataset-grid">'
    + '<section class="panel"><div class="section-title"><h2>原始读数批次</h2><span>' + batches.length + ' 批</span></div>'
    + '<div class="batch-list">' + batches.map(renderBatchItem).join('') + '</div>'
    + renderIngestForm() + '</section>'
    + '<section class="panel"><div class="section-title"><h2 id="detail-title">已发布日耗与读数依据</h2><span>' + daily.length + ' 笔</span></div>'
    + renderDaily(daily) + renderMerge(datasetList) + '</section>'
    + '</div>';
}

const workspace_cache = {status: null, batches: [], daily: [], datasets: []};

function renderStatus(status) {
  const draftCards = (status.drafts || []).map(d => {
    const dc = d.day_counts || {};
    return '<div class="meter-card ' + (d.publishable ? 'is-ok' : 'is-hold') + '">'
      + '<strong>' + esc(d.name) + '</strong>'
      + '<span class="approval">' + (d.publishable ? '可发布' : '对账异常·暂缓') + '</span>'
      + Object.entries(DAY_LABEL).map(([k, label]) =>
        '<small>' + esc(label) + '：<b>' + (dc[k] || 0) + '</b></small>').join('')
      + '<div class="meter-summary">' + (d.meter_summary || []).map(s =>
        '<small>' + esc(s.meter_id) + ' / ' + esc(s.line) + ' · 守恒 ' + (s.balanced ? '✓' : '✗')
        + (s.gap_days.length ? ' · 缺测 ' + s.gap_days.length + ' 天' : '')
        + (s.isolated_days.length ? ' · 隔离 ' + s.isolated_days.length + ' 天' : '')
        + (s.pending_days.length ? ' · 待补齐 ' + s.pending_days.length + ' 天' : '') + '</small>').join('')
      + '</div></div>';
  }).join('');
  return '<section class="panel"><div class="section-title"><h2>状态追查</h2><span>待补齐 / 隔离 / 可发布</span></div>'
    + '<div class="metrics">'
    + metric('已发布日耗', status.published_daily_rows, '笔')
    + metric('缺测区间', status.gap_segments, '段')
    + metric('隔离区间', status.isolated_segments, '段')
    + metric('电表代次', (status.meter_states || []).length, '块')
    + '</div>'
    + (status.meter_states || []).map(s => '<div class="meter-state-row"><span>' + esc(s.meter_id) + ' / ' + esc(s.line)
      + '</span><small>第 ' + s.generation + ' 代 · 批次 #' + (s.batch_id ?? '—') + ' · ' + esc(s.first_time || '') + ' → ' + esc(s.last_time || '') + '</small></div>').join('')
    + (draftCards ? '<div class="section-title"><h2>草稿批次预览</h2></div><div class="meter-card-grid">' + draftCards + '</div>' : '<p class="hint">没有待发布草稿。</p>')
    + '</section>';
}

function metric(label, value, unit) {
  return '<div><span>' + esc(label) + '</span><strong>' + esc(fmt(value)) + '<small>' + esc(unit) + '</small></strong></div>';
}

function renderBatchItem(b) {
  return '<div class="batch-item meter-batch" data-batch="' + b.id + '"><strong>' + esc(b.name) + '</strong>'
    + '<span class="status-badge status-' + esc(b.status) + '">' + esc(STATUS_LABEL[b.status] || b.status) + '</span>'
    + '<span>' + (b.meters || []).map(esc).join('、') + ' · 影响 ' + (b.affected_days || []).length + ' 天 · ' + esc(b.source) + '</span>'
    + '<small>SHA256 ' + esc(b.checksum.slice(0, 18)) + '…</small>'
    + (b.status === 'draft' && canWrite() ? '<button class="button publish-btn" data-publish="' + b.id + '">整组发布 ↗</button>' : '')
    + '</div>';
}

function renderIngestForm() {
  if (!canWrite()) return '<p class="hint">观察员只读：可查看全部批次、状态与读数依据。</p>';
  return '<div class="section-title"><h2>接入原始读数</h2></div>'
    + '<form id="meter-form">'
    + '<label>批次名称<input name="name" required minlength="2" placeholder="例如：冲压P01 十一月抄表（含迟到补录）"></label>'
    + '<label>幂等令牌（可选，重试不重复接入）<input name="request_token" placeholder="留空则每次新建；补录重试建议填写同一令牌"></label>'
    + '<label>上传 CSV 或 JSON<input type="file" id="meter-file" accept=".json,.csv"></label>'
    + '<label>读数 JSON<textarea name="readings" rows="7" required placeholder="[\n  {&quot;meter_id&quot;:&quot;EM-P01-01&quot;,&quot;line&quot;:&quot;冲压P01&quot;,&quot;read_time&quot;:&quot;2026-05-01T08:00&quot;,&quot;reading&quot;:100},\n  {&quot;meter_id&quot;:&quot;EM-P01-01&quot;,&quot;line&quot;:&quot;冲压P01&quot;,&quot;read_time&quot;:&quot;2026-05-02T08:00&quot;,&quot;reading&quot;:130}\n]"></textarea></label>'
    + '<p class="hint">字段：meter_id 电表 / line 线体 / read_time 时刻(ISO) / reading 累计读数 / event 可选 reset|replace / handoff 换表交接读数。</p>'
    + '<button type="submit" class="primary">校验并生成草稿预览</button></form>';
}

function renderDaily(daily) {
  if (!daily.length) return '<div class="empty">尚无已发布日耗。先在左侧接入读数并发布草稿批次。</div>';
  return '<div class="table-wrap"><table><thead><tr>'
    + ['日耗#', '电表', '线体', '日期', '日耗 kWh', '起读数', '止读数', '代次', '读数依据'].map(h => '<th>' + h + '</th>').join('')
    + '</tr></thead><tbody>' + daily.map(d => '<tr>'
      + '<td>' + d.id + '</td><td>' + esc(d.meter_id) + '</td><td>' + esc(d.line) + '</td><td>' + esc(d.day) + '</td>'
      + '<td><b>' + fmt(d.kwh) + '</b></td><td>' + fmt(d.start_reading) + '<br><small>' + esc(d.start_time) + '</small></td>'
      + '<td>' + fmt(d.end_reading) + '<br><small>' + esc(d.end_time) + '</small></td><td>' + d.generation + '</td>'
      + '<td><button class="text-button basis-btn" data-basis="' + d.id + '">追查两笔读数 ↗</button></td>'
      + '</tr>').join('') + '</tbody></table></div>';
}

function renderMerge(datasetList) {
  if (!canWrite() || !datasetList.length) return '<p class="hint">合并归因仅取已发布完整批次；缺测、隔离、待补齐的日子不会进入分析样本。</p>';
  return '<div class="section-title"><h2>合入生产特征供归因</h2><span>仅已发布完整日</span></div>'
    + '<form id="merge-form" class="merge-form"><label>生产特征批次<select name="dataset_id">'
    + datasetList.map(d => '<option value="' + d.id + '">' + esc(d.name) + ' · ' + d.rows + ' 条</option>').join('')
    + '</select></label><label>合并批次名称<input name="merge_name" required minlength="2" value="电表口径合并批次"></label>'
    + '<button class="primary" type="submit">生成可归因批次 ↗</button>'
    + '<p class="hint">原生产批次保持不变；无已发布日耗的线体/日期会被跳过并在结果中列明。原有日样本导入仍可在“数据接入”页使用。</p></form>';
}

export function bindMeter(refresh) {
  let source = 'manual.json';
  const fileInput = document.querySelector('#meter-file');
  fileInput?.addEventListener('change', async e => {
    try {
      const file = e.target.files[0];
      if (!file) return;
      source = file.name;
      const text = await file.text();
      const records = file.name.endsWith('.csv') ? parseCSV(text) : JSON.parse(text);
      document.querySelector('[name=readings]').value = JSON.stringify(records, null, 2);
    } catch (error) { notify(error.message, true); }
  });

  const form = document.querySelector('#meter-form');
  if (form) submit(form, async f => {
    const token = f.get('request_token')?.trim() || null;
    const payload = {name: f.get('name'), source, readings: JSON.parse(f.get('readings'))};
    if (token) payload.request_token = token;
    const batch = await meters.ingest(payload);
    notify('草稿已生成：完整 ' + (batch.preview?.day_counts?.complete || 0)
      + ' 天 / 缺测 ' + (batch.preview?.day_counts?.gap || 0)
      + ' 天 / 隔离 ' + (batch.preview?.day_counts?.isolated || 0) + ' 天');
    await refresh();
  });

  document.querySelectorAll('.publish-btn').forEach(btn => btn.addEventListener('click', async () => {
    try {
      const batch = await meters.publish(Number(btn.dataset.publish));
      notify('已整组发布 ' + (batch.affected_days || []).length + ' 个受影响日，旧跨段关系已撤销重建');
      await refresh();
    } catch (error) { notify(error.message, true); }
  }));

  document.querySelectorAll('.basis-btn').forEach(btn => btn.addEventListener('click', async () => {
    try {
      const detail = await meters.basis(Number(btn.dataset.basis));
      showBasisModal(detail);
    } catch (error) { notify(error.message, true); }
  }));

  const mergeForm = document.querySelector('#merge-form');
  if (mergeForm) submit(mergeForm, async f => {
    const result = await meters.merge({
      name: f.get('merge_name'), dataset_id: Number(f.get('dataset_id'))});
    const skipped = result.merge_meta?.skipped?.length || 0;
    notify('已生成「' + result.name + '」' + result.rows + ' 条；跳过缺测/隔离 ' + skipped + ' 天，可前往工作台归因');
    await refresh();
  });
}

function showBasisModal(detail) {
  const r = reading => reading ? esc(reading.read_time) + ' 表底 ' + fmt(reading.reading)
    + (reading.event ? '（事件 ' + esc(reading.event) + '）') : '—';
  const segments = detail.segments || [];
  const segmentText = segments.length
    ? segments.map(seg => esc(SEG_LABEL[seg.status] || seg.status) + '：' + esc(seg.reason || '')
      + '（差额 ' + fmt(seg.delta) + ' = ' + fmt(seg.end_reading) + ' − ' + fmt(seg.start_reading) + '）').join('<br>')
    : '—';
  const modal = document.createElement('div');
  modal.className = 'modal-backdrop';
  modal.innerHTML = '<div class="modal panel" role="dialog" aria-modal="true">'
    + '<div class="section-title"><h2>日耗 #' + detail.daily.id + ' 的读数依据</h2>'
    + '<button class="text-button" id="modal-close">关闭 ✕</button></div>'
    + '<div class="metrics"><div><span>线体 / 电表</span><strong>' + esc(detail.daily.line)
    + '<small>' + esc(detail.daily.meter_id) + '</small></strong></div>'
    + '<div><span>归属日期</span><strong>' + esc(detail.daily.day) + '<small>第 ' + detail.daily.generation + ' 代</small></strong></div>'
    + '<div><span>日耗</span><strong>' + fmt(detail.daily.kwh) + '<small>kWh</small></strong></div>'
    + '<div><span>恒等式校验</span><strong>' + (detail.identity_check ? '通过 ✓' : '不符 ✗') + '<small>止读数−起读数</small></strong></div></div>'
    + '<div class="basis-flow"><div><p class="eyebrow">起读数</p><p>' + r(detail.start_reading) + '</p><small>读数 #' + (detail.start_reading?.id ?? '—') + '</small></div>'
    + '<div class="flow-arrow">→</div><div><p class="eyebrow">止读数</p><p>' + r(detail.end_reading) + '</p><small>读数 #' + (detail.end_reading?.id ?? '—') + '</small></div></div>'
    + '<p class="hint">底层区间 ' + segments.length + ' 段：<br>' + segmentText + '。</p>'
    + '</div>';
  document.body.appendChild(modal);
  modal.addEventListener('click', e => { if (e.target === modal || e.target.id === 'modal-close') modal.remove(); });
}
