'use strict';
const el = id => document.getElementById(id);
const methods = ['B0', 'B1', 'B2', 'A0'];
const statusNames = {baseline_survives:'原测试未检出',verified:'技术验证通过',generation_failed:'生成失败',invalid_test_oracle:'参考版本未通过',generation_execution_failed:'生成测试执行失败',repair_not_reproducible:'复现门槛未通过'};
const semanticNames = {confirmed:'确认正确',rejected:'不符合语义',uncertain:'尚不确定',not_verified:'未获技术验证'};
const defectNames = {boundary:'边界条件',null_type:'空值 / 类型',exception_handling:'异常处理'};
let offset = 0, total = 0, listVersion = 0, detailVersion = 0;
const pageSize = 20;
function node(tag, text) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; return n; }
function row(values) { const tr = node('tr'); for (const value of values) tr.appendChild(node('td', value)); return tr; }
async function api(path) {
  const response = await fetch(path);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '结果暂不可用');
  return data;
}
function options(id, values, names = {}) {
  for (const value of values) { const option = node('option', names[value] || value); option.value = value; el(id).appendChild(option); }
}
function drawSummary(data) {
  el('experiment-notice').textContent = data.notice;
  for (const [value, label] of [[data.projects_count,'开源项目'],[data.tasks_count,'注入缺陷任务'],[data.records_count,'实验记录']]) {
    const card = node('div'); card.append(node('strong', value), node('span', label)); el('experiment-stats').appendChild(card);
  }
  for (const group of methods) {
    const m = data.methods[group], v = data.groups[group];
    const description = m.generation_rounds ? `${m.generation_rounds}轮生成 · ${m.coverage_feedback ? '带' : '无'}覆盖率反馈 · 最多${m.repair_attempts}次修复` : '仅运行原测试';
    el('method-rows').appendChild(row([group,description,`${v.detected}/${v.planned}`,`${v.technical_verified}/${v.planned}`,`${v.semantic_confirmed}/${v.planned}`,`${(v.semantic_rate*100).toFixed(2)}%`]));
  }
  for (const p of data.projects) el('project-rows').appendChild(row([`${p.id} · ${p.tag}`,...methods.map(g => `${data.by_project[p.id][g].semantic_confirmed}/18`)]));
  for (const text of data.limits) el('experiment-limits').appendChild(node('li', text));
  options('filter-project', data.projects.map(p=>p.id)); options('filter-method', methods);
  options('filter-defect', data.defect_types, defectNames); options('filter-status', data.statuses, statusNames);
}
function hideDetail() {
  detailVersion++;
  el('detail-content').hidden = true; el('detail-download').hidden = true;
  el('detail-summary').textContent = '点击记录右侧“查看”，检查测试、补丁与审查依据。';
}
async function loadRecords() {
  const version = ++listVersion;
  el('records-prev').disabled = true; el('records-next').disabled = true;
  el('record-count').textContent = '正在载入记录…'; el('record-rows').replaceChildren();
  const query = new URLSearchParams(new FormData(el('experiment-filters')));
  for (const [key,value] of [...query]) if (!value) query.delete(key);
  query.set('limit',pageSize); query.set('offset',offset);
  try {
    const data = await api(`/api/experiments/records?${query}`);
    if (version !== listVersion) return;
    total = data.total;
    for (const record of data.records) {
      const tr = row([`${record.project_id}\n${record.task_id}`,`${record.group} · 第${record.repeat}次`,defectNames[record.defect_type]||record.defect_type,statusNames[record.status]||record.status,semanticNames[record.semantic],`${record.elapsed_seconds.toFixed(1)} 秒`]);
      const cell = node('td'), button = node('button','查看');
      button.setAttribute('aria-label',`查看 ${record.task_id} ${record.group} 第${record.repeat}次`);
      button.onclick = () => showDetail(record.slot_id); cell.appendChild(button); tr.appendChild(cell); el('record-rows').appendChild(tr);
    }
    el('record-count').textContent = total ? `筛选匹配 ${total} 条；完整实验仍为360条，主表分母不变。` : '没有匹配的记录，可调整筛选条件。';
    el('records-page').textContent = total ? `第 ${Math.floor(offset/pageSize)+1} / ${Math.ceil(total/pageSize)} 页` : '0 条';
    el('records-prev').disabled = offset === 0; el('records-next').disabled = offset+pageSize >= total;
  } catch (error) { if(version === listVersion) el('record-count').textContent = error.message; }
}
function codeBlock(host, title, content) { const d = node('details'); d.append(node('summary',title), node('pre',content)); host.appendChild(d); }
function coverageText(value) { return value && value.total > 0 ? `${value.covered}/${value.total} · ${(100*value.covered/value.total).toFixed(2)}%` : 'N/A'; }
async function showDetail(slot) {
  const version = ++detailVersion;
  el('detail-content').hidden = true; el('detail-download').hidden = true;
  el('detail-summary').textContent = '正在读取本条实验…';
  try {
    const data = await api(`/api/experiments/records/${slot}`);
    if(version !== detailVersion) return;
    const r = data.record;
    el('detail-summary').textContent = `${r.project_id} · ${r.group} · 第${r.repeat}次 · ${statusNames[r.status]} · ${semanticNames[r.semantic]}\n记录 ${slot}`;
    el('detail-download').href = `/api/experiments/records/${slot}/download`; el('detail-download').hidden = false;
    el('detail-requirements').textContent = `${data.project.id} ${data.project.tag}\n提交 ${data.project.commit}\n${data.target.path} :: ${data.target.qualified_name}\n\n${data.requirements.join('\n')}\n\n需求出处\n${data.requirement_sources.map(s=>`${s.path}:${s.start_line}-${s.end_line}\n${s.excerpt}`).join('\n\n')}`;
    el('detail-tests').replaceChildren(); el('detail-patches').replaceChildren(); el('detail-coverage').replaceChildren();
    for(const test of data.tests) codeBlock(el('detail-tests'),`第${test.round}轮 · ${test.path}`,test.content);
    if(!data.tests.length) el('detail-tests').appendChild(node('p','本条没有已接受的生成测试输出。B0不生成测试。'));
    for(const patch of data.patches) {
      const review = patch.review;
      const box = node('div');
      box.append(node('h4',`第${patch.attempt}次补丁 · ${review.is_last_attempt?'最后一次提案':'中间提案'} · ${semanticNames[review.status]}`),node('p',`审查：${review.reviewer}\n${review.notes}`));
      codeBlock(box,'查看源码差异',patch.diff); el('detail-patches').appendChild(box);
    }
    if(!data.patches.length) el('detail-patches').appendChild(node('p','本条没有实际应用的修复补丁。'));
    for(const [stage,value] of Object.entries(data.coverage)) el('detail-coverage').appendChild(row([stage,coverageText(value.lines),coverageText(value.branches)]));
    const u = data.usage;
    el('detail-usage').textContent = `请求次数：${u.requests}\n原包装层已知 tokens：${u.known_tokens}\n未知用量请求：${u.unknown_requests}\n总 tokens：${u.total_tokens===null?'未知':u.total_tokens}\n\n生成轮次记录\n${JSON.stringify(data.generation_rounds,null,2)}`;
    el('detail-log-notice').textContent = data.log_notice;
    el('detail-raw').textContent = JSON.stringify(data,null,2);
    el('detail-content').hidden = false;
    el('experiment-detail').scrollIntoView({behavior:'smooth',block:'start'});
  } catch(error) { if(version===detailVersion) el('detail-summary').textContent=error.message; }
}
el('experiment-filters').addEventListener('submit',event=>event.preventDefault());
el('experiment-filters').addEventListener('change',()=>{offset=0;hideDetail();loadRecords();});
el('experiment-filters').addEventListener('reset',()=>setTimeout(()=>{offset=0;hideDetail();loadRecords();},0));
el('records-prev').onclick=()=>{offset=Math.max(0,offset-pageSize);hideDetail();loadRecords();};
el('records-next').onclick=()=>{offset+=pageSize;hideDetail();loadRecords();};
(async()=>{try{drawSummary(await api('/api/experiments/summary'));el('experiment-message').textContent='结果已载入，证据摘要与统计复算校验通过。';await loadRecords();}catch(error){el('experiment-message').textContent=error.message;}})();
