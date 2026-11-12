
const $ = (id) => document.getElementById(id);
let rulesData = null, skillData = null, decData = null, counter = 0, toastTimer = null;
const ICON_FLAME = '<svg viewBox="0 0 24 24"><path d="M12 2c1 4-3 5-3 9 0 1.5.8 2.6 2 3.2-.3-2 .7-3.4 2-4.6.4 2.4 2.6 3 2.6 5.4A5.6 5.6 0 0 1 12 21a5.6 5.6 0 0 1-5.6-5.6C6.4 10 10 8.6 12 2z"/></svg>';

function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]); }
function toast(msg) {
  const el = $('toast'); el.textContent = msg; el.classList.add('on');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('on'), 2800);
}
function floatXp(n) {
  const el = $('floatxp'); el.textContent = '+' + n;
  el.classList.remove('go'); void el.offsetWidth; el.classList.add('go');
}
async function api(url, body) {
  const opt = body === undefined ? {} : { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const res = await fetch(url, opt);
  const data = await res.json();
  if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}
function table(headers, rows) {
  return '<table><thead><tr>' + headers.map(h => '<th>' + esc(h) + '</th>').join('') +
    '</tr></thead><tbody>' + rows.map(r => '<tr>' + r.map(c => '<td>' + c + '</td>').join('') +
    '</tr>').join('') + '</tbody></table>';
}
function fold(title, body, open) {
  return '<details' + (open ? ' open' : '') + '><summary>' + esc(title) + '</summary>' +
    '<div class="dbody">' + body + '</div></details>';
}
function ts(unix) {
  const d = new Date(unix * 1000);
  return (d.getUTCMonth() + 1) + '/' + d.getUTCDate() + ' ' +
    String(d.getUTCHours()).padStart(2, '0') + ':' + String(d.getUTCMinutes()).padStart(2, '0') + ' UTC';
}

/* ---------------- 页签 ---------------- */
function switchTab(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + name));
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  $('screen').scrollTop = 0;
  if (name === 'rules' && rulesData) renderRules(rulesData);
  if (name === 'settings' && decData) renderDecisions(decData);
}
document.querySelectorAll('.tab').forEach(b => b.onclick = () => switchTab(b.dataset.tab));

/* ---------------- 诊断修正后的呈现 ---------------- */
function playChime() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    const ctx = new AC();
    const osc = ctx.createOscillator(), g = ctx.createGain();
    osc.type = 'sine'; osc.frequency.value = 880;
    g.gain.setValueAtTime(0.0001, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.05, ctx.currentTime + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.42);
    osc.connect(g); g.connect(ctx.destination);
    osc.start(); osc.stop(ctx.currentTime + 0.45);
    osc.onended = () => ctx.close();
  } catch (e) { /* 音频不可用时静默，不影响任何判定 */ }
}
function showLevelUp(lu) {
  const nums = ['', '壹', '贰', '叁', '肆', '伍', '陆', '柒', '捌'];
  $('lvup-num').textContent = nums[lu.to] || lu.to;
  $('lvup-k').textContent = 'L V ' + lu.from + ' → L V ' + lu.to;
  $('lvup-name').textContent = (lu.realm || '') + ' · ' + (lu.name || '');
  $('lvup-can').innerHTML = '<b style="color:#e8c86a">你 现 在 能 做 到</b><br>' +
    esc(lu.observable || '') + '<br><span style="color:#8a8070">' +
    esc(lu.skill_id || '') + '</span>';
  $('lvup').classList.add('on');
  playChime();
}
$('lvup').onclick = () => $('lvup').classList.remove('on');

function renderRecommendation(d) {
  const g = d.guidance && d.guidance.recommendation;
  if (!g) {
    $('reco-title').textContent = '先完成一次修行，系统才好给你指路';
    $('reco-gain').textContent = ''; $('reco-missing').textContent = '';
    return;
  }
  $('reco-title').textContent = g.skill_id + ' · ' + g.action;
  $('reco-gain').textContent = '预计 +' + g.estimated_gain + ' → 够到 LV' +
    g.target_level + ' · 还差 ' + g.remaining_exp + ' 点（' + g.estimate_note + '）';
  const miss = (g.missing || []).map(x => esc(x)).join('<br>· ');
  const stag = g.stagnation ? '<br><span style="color:var(--bad)">停滞：' +
    esc(g.stagnation.hint) + '</span>' : '';
  $('reco-missing').innerHTML = (miss ? '待补：<br>· ' + miss : '') + stag;
  $('reco-card').onclick = () => openSheet(g.skill_id);
}
function renderFactors(data) {
  const v = data.verdict, f = v && v.factors, box = $('factors-card');
  if (!v || !v.valid || !f) { box.style.display = 'none'; return; }
  box.style.display = 'block';
  const num = (x, dflt) => Number(x == null ? dflt : x) / 10000;
  const rows = [
    ['难度匹配', num(f.difficulty_bps, f.challenge_bps || 10000)],
    ['完成质量', num(f.quality_bps, 10000)],
    ['反馈强度', num(f.feedback_bps, 10000)],
    ['重复折扣', num(f.repeat_bps, 10000)],
    ['同日递减', num(f.same_day_bps, 10000)],
    ['间隔加成', num(f.spacing_bps, 10000)]
  ];
  if (f.survey_bps && Number(f.survey_bps) !== 10000) {
    rows.push(['当次状态', num(f.survey_bps, 10000)]);
  }
  let hot = -1, low = 2;
  rows.forEach((r, i) => { if (i < 4 && r[1] < low) { low = r[1]; hot = i; } });
  $('factors-rows').innerHTML = rows.map((r, i) =>
    '<span class="' + (i === hot ? 'hot' : '') + '">' + r[0] + ' <b>×' +
    r[1].toFixed(2) + '</b></span>').join('') +
    '<span>技能系数 <b>×' + num(v.skill_factor_bps, 10000).toFixed(2) + '</b></span>' +
    (hot >= 0 ? '<span style="width:100%;color:var(--warn)">最高杠杆：' +
      rows[hot][0] + '，把它补上收益最大</span>' : '') +
    (!data.survey ? '<span style="width:100%;color:var(--muted)">' +
      '这次没填「当次状态」，按 ×1.00 计；填了会按专注/疲劳/新鲜度/完成度微调。</span>' : '');
}
function renderTaskTimeline(d) {
  const rows = d.task_timeline || [];
  $('pb-count').textContent = rows.length + ' 组';
  $('task-groups').innerHTML = rows.length ? rows.map(g =>
    '<div class="pbcard"><div class="t">' + esc(g.task_key) + ' · ' +
      esc(g.skill_id) + '</div><div class="s">练了 ' + g.count + ' 次 · ' +
      esc(g.metric_label) + ' 从 ' + g.first_value + ' 到最好 ' + g.best_value + ' ' +
      esc(g.metric_unit) + (g.is_pb ? ' · <b style="color:var(--primary)">刚破个人最好</b>' : '') +
      '</div><ul>' + g.items.slice().reverse().map(x =>
        '<li>' + ts(x.at) + ' · <b>' + x.value + '</b> ' + esc(g.metric_unit) +
        ' · +' + x.delta + (x.note ? '<br><em>' + esc(x.note) + '</em>' : '') +
        '</li>').join('') + '</ul></div>').join('')
    : '<div class="kv">同一个任务练过两次以上，这里会出现纵向对比与个人最好成绩。</div>';
}

/* ---------------- 今日 ---------------- */
function renderToday(d) {
  skillData = d;
  $('top-realm').textContent = d.top_realm || '初窥门径';
  $('total-xp').textContent = d.achievement_context.total_xp;
  $('today-xp').textContent = d.today_xp;
  $('lv-max').textContent = 'LV' + d.achievement_context.max_level;
  const sk = d.streak || {};
  const days = sk.current_days || 0;
  $('streak-num').textContent = days;
  const flame = $('flame');
  flame.classList.toggle('cold', days === 0);
  flame.innerHTML = ICON_FLAME + '<span id="streak-num">' + days + '</span> 日';
  $('hero-sub').textContent = '七艺同修 · ' + (d.achievement_context.sessions || 0) + ' 次有效修行';
  $('status-right').textContent = '本地 · ' + (d.verify.length ? '自检异常' : '自检正常');
  // 一次都还没记过时，先把「第一步做什么」说清楚；记录一次后自动收起。
  $('onboard').style.display = (d.achievement_context.sessions || 0) ? 'none' : '';

  renderRecommendation(d);
  $('skills').innerHTML = d.skills.map(s => {
    const pct = s.is_max_level ? 100 : s.percent;
    // 卡级把所有未通过项一次说全，不再只显示第一条造成「只说一半」。
    const missing = s.missing || [];
    const gate = s.gate_blocked
      ? '<span class="tag-gate">经验够 LV' + s.exp_level + ' · ' +
          (missing.length ? esc(missing.join('；')) : '达标校验未放行') + '</span>'
      : (s.is_max_level ? '已至圆满' : '还差 ' + s.remaining_exp + ' 点');
    return '<div class="skill" data-skill="' + esc(s.skill_id) + '">' +
      '<div class="skill-top"><div><span class="skill-name">' + esc(s.skill_id) + '</span>' +
        '<span class="skill-realm">' + esc(s.realm) + '</span></div>' +
        '<div class="skill-lv">LV<b>' + s.level + '</b></div></div>' +
      '<div class="bar' + (s.gate_blocked ? ' gate' : '') + '"><i style="width:' + pct + '%"></i></div>' +
      '<div class="mile">' + [30, 60, 100].map(m =>
        '<i class="' + (pct >= m ? 'on' : '') + '"></i>').join('') + '</div>' +
      '<div class="skill-foot"><span>' + s.effective_exp + ' / ' +
        (s.is_max_level ? 'MAX' : (s.current_exp + s.remaining_exp)) + '</span><span>' + gate + '</span></div>' +
      '</div>';
  }).join('');
  const top = d.skills.reduce((a, b) => (b.percent > a.percent ? b : a), d.skills[0]);
  $('milestone-note').textContent = top
    ? ('本级里程碑 30% / 60% / 100% · ' + top.skill_id + ' 当前 ' +
       Math.round(top.percent) + '%' + (top.fast_track ? ' · 连续达标快通已激活' : ''))
    : '本级里程碑：30% / 60% / 100%';
  document.querySelectorAll('.skill').forEach(el => {
    el.onclick = () => openSheet(el.dataset.skill);
  });
  ['top-realm', 'today-xp', 'total-xp'].forEach(id => {
    $(id).classList.remove('pop'); void $(id).offsetWidth; $(id).classList.add('pop');
  });
}

/* ---------------- 江湖（利他层） ---------------- */
let practiceSeqs = new Set();
function renderWorld(w) {
  if (!w) return;
  const wallet = w.wallet || {}, rules = w.rules || {};
  const cap = rules.daily_reputation_cap || 100;
  const costs = rules.exchange_cost || {};
  practiceSeqs = new Set((w.practice_seqs || []).map(Number));

  $('rep-title').textContent = w.reputation_title || '无名行者';
  $('rep-line').textContent = '声望 ' + w.reputation + ' 点';
  $('rep-day').textContent = '今日累计 ' + w.day_reputation + ' / ' + cap;
  $('hero-wallet').textContent = '声望 ' + w.reputation + ' · 连胜激冻 ' +
    (wallet.freeze_cards || 0) + ' 张' + (wallet.pending_insight ? ' · 悟道卡已装备' : '');
  $('wallet').innerHTML = [
    ['悟道卡', wallet.insight_cards || 0, '下一次有效练习翻倍'],
    ['连胜激冻', wallet.freeze_cards || 0, '断签时保一次连胜'],
    ['今日功德', w.day_reputation, '每日上限 ' + cap]
  ].map(x => '<div class="wallet-cell"><b>' + x[1] + '</b><span>' + esc(x[0]) +
    '<br>' + esc(x[2]) + '</span></div>').join('');
  $('wallet-hint').textContent = wallet.pending_insight
    ? '悟道卡已装备：下一次有效练习熟练度翻倍，完成后自动消耗。'
    : '声望只能靠护道、题签、传承获得，不能购买。兑换：悟道卡 ' +
      (costs.insight_card || 0) + ' 点 / 连胜激冻 ' + (costs.freeze_card || 0) + ' 点。';
  $('guild-qi').textContent = '宗门气运 ' + (w.guild_qi || 0);
  $('peers').innerHTML = (w.peers || []).map(p =>
    '<div class="peer' + (p.at_risk ? ' risk' : '') + '"><div>' +
    '<div class="pn">' + esc(p.name) + ' · 连胜 ' + p.streak_days + ' 日</div>' +
    '<div class="pd">' + (p.at_risk
      ? '已 ' + p.days_since_last + ' 天未修行，连胜将断'
      : '今日已修行，连胜安稳') +
    ' · 被护道 ' + (p.protected_days || 0) + ' 次</div></div>' +
    '<button class="mini' + (p.at_risk ? ' pri' : '') + '" data-guard="' + esc(p.id) + '"' +
    (p.at_risk ? '' : ' disabled') + '>护 道</button></div>').join('');
  document.querySelectorAll('[data-guard]').forEach(b => {
    b.onclick = () => altruism('guard', { target: b.dataset.guard });
  });

  const seqs = (w.event_seqs || []).slice().reverse();
  $('deed-event').innerHTML = seqs.length
    ? seqs.map(s => '<option value="' + s + '">#' + s + '</option>').join('')
    : '<option value="">暂无记录</option>';
  const names = { guard: '护道', encourage: '题签', share: '传承' };
  $('deeds').innerHTML = (w.deeds || []).length
    ? w.deeds.map(d => '<div class="deed-row">#' + d.seq + ' · ' +
        esc(names[d.kind] || d.kind) + ' · ' + esc(d.target) +
        ' <span class="note-ok">+' + d.reputation + '</span> · ' + ts(d.at) + '</div>').join('')
    : '<div class="kv">还没有功德记录。先去护道，或给一条修行记录留句鼓励。</div>';
  $('deed-count').textContent = (w.deeds || []).length + ' 条';
  $('scope-note').textContent = (w.scope_note || '') +
    ' 声望数值、称号与兑换门槛均为本机自定参数，不是任何学术框架的实现。';
}
$('ex-insight').onclick = () => altruism('exchange', { item: 'insight_card' });
$('ex-freeze').onclick = () => altruism('exchange', { item: 'freeze_card' });
$('use-insight').onclick = () => altruism('use_insight', {});
$('use-freeze').onclick = () => altruism('use_freeze', {});
$('do-encourage').onclick = () => {
  const v = $('deed-event').value;
  if (!v) { toast('先完成一次修行'); return; }
  altruism('encourage', { target: Number(v) });
};
$('do-share').onclick = () => {
  const v = $('deed-event').value;
  if (!v) { toast('先完成一次修行'); return; }
  if (!practiceSeqs.has(Number(v))) {
    toast('这条不是有效练习，不能公开为范例'); return;
  }
  altruism('share', { target: Number(v) });
};

/* ---------------- 历程 ---------------- */
function renderJourney(d) {
  const series = (d.stats && d.stats.series) || [];
  const max = Math.max(1, ...series.map(x => x.xp));
  $('strip').innerHTML = series.map(x =>
    '<i class="' + (x.xp ? '' : 'empty') + '" style="height:' +
    Math.max(2, Math.round(x.xp / max * 100)) + '%" title="' + x.xp + ' 点"></i>').join('');
  $('stat-total').textContent = (d.stats ? d.stats.total_xp : 0) + ' 点';

  const byDay = {};
  (d.events || []).forEach(e => {
    const day = Math.floor(e.at / 86400);
    if (e.delta > 0) byDay[day] = (byDay[day] || 0) + e.delta;
  });
  const today = Math.floor((d.clock ? d.clock.unix : Date.now() / 1000) / 86400);
  $('days').innerHTML = Array.from({ length: 14 }, (_, i) => {
    const day = today - 13 + i;
    return '<i class="' + (byDay[day] ? 'hot' : '') + '">' + (day % 100) + '</i>';
  }).join('');
  const sk = d.streak || {};
  $('streak-note').textContent = '连续 ' + (sk.current_days || 0) + ' 日 · 补签剩 ' +
    (sk.grace_remaining != null ? sk.grace_remaining : 1) + ' 次';

  const ach = d.achievements || [];
  $('ach-count').textContent = ach.filter(a => a.unlocked).length + ' / ' + ach.length;
  $('achievements').innerHTML = ach.map(a =>
    '<div class="ach' + (a.unlocked ? ' got' : '') + '"><div class="medal">' + esc(a.icon) + '</div>' +
    '<b>' + esc(a.name) + '</b><span>' + esc(a.desc) + '</span>' +
    '<div class="mini"><i style="width:' + a.progress_percent + '%"></i></div>' +
    '<span>' + a.actual + ' / ' + a.target + '</span></div>').join('');

  const ev = (d.events || []).slice(0, 30);
  const names = { practice: '修行', rejected: '未计入', admin: '调整', reset: '重置' };
  $('timeline').innerHTML = ev.length ? ev.map(e =>
    '<li class="' + (e.kind === 'rejected' ? 'bad' : (e.kind === 'practice' ? '' : 'admin')) + '">' +
    '<b>' + esc(e.skill_id) + '</b> · ' + (names[e.kind] || e.kind) +
    (e.delta > 0 ? ' <span class="note-ok">+' + e.delta + '</span>' :
      (e.delta < 0 ? ' <span class="note-bad">' + e.delta + '</span>' : '')) +
    '<div class="t">' + ts(e.at) + (e.task_key && e.kind === 'practice' ? ' · ' + esc(e.task_key) : '') +
    (e.reason_code && e.kind !== 'practice' ? ' · ' + esc(e.reason_code) : '') + '</div></li>').join('')
    : '<li>还没有记录。完成第一次修行后会在这里出现。</li>';
  renderTaskTimeline(d);
}

/* ---------------- 规则 ---------------- */
function renderRules(r) {
  rulesData = r;
  $('disclaimer').textContent = r.threshold_disclaimer || '';
  const plain = (r.plain_rules || []).map(x =>
    '<div class="plain"><b>' + esc(x.title) + '</b><p>' + esc(x.text) + '</p>' +
    '<code>' + esc(x.tech) + '</code></div>').join('');
  const boundaries = table(['边界', '规则'], Object.keys(r.trigger_boundaries || {}).map(k =>
    ['<code>' + esc(k) + '</code>', esc(r.trigger_boundaries[k])]));
  const criteria = table(['等级', '名称', '次数', '均质', '反馈', '难度', '允许搁置', '可观测条件'],
    (r.level_criteria || []).map(c => ['LV' + c.level, esc(c.name), c.min_sessions,
      c.min_avg_quality, c.min_feedback, c.min_difficulty,
      c.max_idle_days == null ? '不限' : c.max_idle_days + ' 天', esc(c.observable)]));
  const skillTables = Object.keys(r.skill_rules || {}).map(sid => {
    const rule = r.skill_rules[sid];
    return '<h4 style="font-size:12px;margin:12px 0 3px">' + esc(sid) +
      ' <span class="badge2">' + esc(rule.standard || '') + '</span></h4>' +
      '<div class="hint">' + esc(rule.description) + '</div>' +
      table(['指标', '单位', '有效门槛', '加成线', '合法范围', 'LV1–LV8 均值要求'],
        rule.metrics.map(m => [esc(m.label), esc(m.unit), '≥ ' + m.min_valid, '≥ ' + m.good,
          m.range_low + '~' + m.range_high, m.level_requirements.join(' / ')])) +
      table(['等级', '可观测达标条件', '依据'], rule.level_observables.map((o, i) =>
        ['LV' + (i + 1), esc(o), esc((rule.mapping_kind_by_level || {})[String(i + 1)] || '')]));
  }).join('');
  const sources = table(['标识', '来源与要点'], Object.keys(r.standard_sources || {}).map(k => {
    const s = r.standard_sources[k];
    return ['<code>' + esc(k) + '</code>', '<b>' + esc(s.name || k) + '</b>' +
      ' <span class="badge2">' + esc(s.grade || '') + '</span>' +
      '<div class="kv">' + esc(s.note || '') + '</div>' +
      '<code>' + esc(s.url || '') + '</code>'];
  }));
  const reasons = table(['顺序', '原因码', '判定结果'], (r.reason_order || []).map((k, i) =>
    [i + 1, '<code>' + esc(k) + '</code>', esc((r.reason_codes || {})[k] || '')]));
  const gates = table(['顺序', '技能门'], (r.skill_metric_gate_order || []).map((k, i) =>
    [i + 1, '<code>' + esc(k) + '</code>']));
  const decay = table(['搁置天数', '保持度上限'], (r.decay_anchors || []).map(a =>
    [a.days + ' 天', a.retention_bps + ' bps']));
  const formula = table(['量', '公式'], Object.keys(r.formula || {}).map(k =>
    ['<code>' + esc(k) + '</code>', '<code>' + esc(r.formula[k]) + '</code>']));
  const constRows = [['质量门槛', 'Q < ' + r.constants.MIN_QUALITY + ' 判无效'],
    ['无反馈质量门槛', 'Q < ' + r.constants.NO_FEEDBACK_QUALITY_GATE],
    ['连续低难度', '第 ' + r.constants.LOW_STREAK_LIMIT + ' 次起判无效'],
    ['重复窗口', r.constants.REPEAT_WINDOW + ' 次'],
    ['宽限期', r.constants.DECAY_GRACE_SECONDS + ' 秒'],
    ['保持度下限', r.constants.RETENTION_FLOOR_BPS + ' bps'],
    ['优秀线加成', '每项 +' + (r.constants.GOOD_BONUS_BPS / 100) + '%']];
  const streak = table(['连胜规则', '值'], Object.keys(r.streak_rules || {}).map(k =>
    [esc(k), esc(String(r.streak_rules[k]))]));
  const achDef = table(['成就', '条件', '目标'], (r.achievements || []).map(a =>
    [esc(a.name) + '<br><span class="t">' + esc(a.desc) + '</span>',
      '<code>' + esc(a.metric) + '</code>', a.target]));
  const checks = (r.article_fact_checks || []).map(c =>
    '<div class="plain"><b>' + esc(c.claim) + '</b><p><span class="note-bad">' +
    esc(c.verdict) + '</span> · ' + esc(c.evidence) + '</p><p>处理：' + esc(c.action) + '</p></div>').join('');
  const u = r.ulives_reference || {};
  const ulives = '<div class="hint">' + esc(u.product || '') + '</div>' +
    table(['已借鉴', '本系统对应'], (u.adopted || []).map(a => [esc(a.mechanism), esc(a.here)])) +
    table(['未采纳', '理由'], (u.not_adopted || []).map(a => [esc(a.mechanism), esc(a.reason)])) +
    '<div class="hint">来源：' + (u.sources || []).map(s => '<code>' + esc(s) + '</code>').join(' ') + '</div>';
  const decided = table(['决策', '选定', '实施结果'], (r.open_decisions || []).map(d =>
    [esc(d.question), '<code>' + esc(d.selected) + '</code>', esc(d.implementation)]));
  const verification = (r.source_verification || []).map(c =>
    '<div class="plain"><b>[' + esc(c.layer) + '] ' + esc(c.claim) + '</b>' +
    '<p><span class="note-bad">' + esc(c.verdict) + '</span> · ' + esc(c.evidence) +
    '</p><p>处理：' + esc(c.action) + '</p></div>').join('');
  const t = r.trust_rules || {}, n = r.narrative_rules || {}, a = r.altruism_rules || {};
  const punish = n.punishment_policy || {};
  const threeLayers =
    '<div class="hint"><b>验证层</b> ' + esc(t.declaration || '') + '</div>' +
    table(['证明类型取值', '说明'], Object.keys(t.proof_type_labels || {}).map(k =>
      ['<code>' + esc(k || '（空）') + '</code>', esc(t.proof_type_labels[k])])) +
    '<div class="kv">已实现真实校验：<b>' + (t.verification_implemented ? '是' : '否') +
      '</b> · 本机不接收图片、不下载链接、不校验真伪。</div>' +
    '<div class="hint" style="margin-top:10px"><b>叙事层</b> ' +
      esc(n.push_capability || '') + '。' + esc(n.no_unverified_metrics || '') + '</div>' +
    table(['惩罚政策', '取值'], Object.keys(punish).map(k =>
      [esc(k), esc(Array.isArray(punish[k]) ? punish[k].join(' / ') : String(punish[k]))])) +
    '<div class="hint" style="margin-top:10px"><b>利他层</b> 声望汇率与门槛是本机自定参数，' +
      '「GP-AIR」查无此框架名；机制只借间接互惠的思路。</div>' +
    table(['利他行为', '声望'], Object.keys(a.reputation || {}).map(k =>
      [esc(k), a.reputation[k]])) +
    table(['兑换项', '所需声望'], Object.keys(a.exchange_cost || {}).map(k =>
      [esc(k), a.exchange_cost[k]])) +
    table(['声望称号', '门槛'], (r.reputation_titles || []).map(x =>
      [esc(x.name), x.min]));
  const diag = (r.diagnosis_verification || []).map(c =>
    '<div class="plain"><b>' + esc(c.claim) + '</b><p><span class="note-bad">' +
    esc(c.verdict) + '</span> · ' + esc(c.evidence) + '</p><p>处理：' +
    esc(c.action) + '</p></div>').join('');
  const sr = r.shape_rules || {};
  const shapeTable = table(['形状参数', '值'], Object.keys(sr)
    .filter(k => k !== 'disclaimer').map(k =>
      [esc(k), esc(typeof sr[k] === 'object' ? JSON.stringify(sr[k]) : String(sr[k]))])) +
    '<div class="hint">' + esc(sr.disclaimer || '') + '</div>';

  const lf = r.low_friction || {};
  const capVerify = (r.capability_verification || []).map(c =>
    '<div class="plain"><b>' + esc(c.claim) + '</b><p><span class="note-bad">' +
    esc(c.verdict) + '</span> · ' + esc(c.source) + '</p><p>' + esc(c.note) + '</p>' +
    (c.url ? '<code>' + esc(c.url) + '</code>' : '') + '</div>').join('');
  const lowFriction = (lf.plain || []).map(x =>
    '<div class="plain"><p>' + esc(x) + '</p></div>').join('') +
    table(['参数', '值'], Object.keys(lf)
      .filter(k => k !== 'plain' && k !== 'not_implemented').map(k =>
      ['<code>' + esc(k) + '</code>',
        esc(typeof lf[k] === 'object' ? JSON.stringify(lf[k]) : String(lf[k]))])) +
    '<div class="hint">明确未实现：' +
    (lf.not_implemented || []).map(x => esc(x)).join(' / ') + '</div>' +
    '<h4 style="font-size:12px;margin:12px 0 4px">能力边界 · 反向核实（' +
    (r.capability_verification || []).length + ' 条）</h4>' + capVerify;
  const fm = r.settlement_form_meta || {};
  const settleBook = r.settlement_forms || {};
  const settleTable = '<div class="hint">' + esc(fm.disclaimer || '') + '</div>' +
    table(['问卷里的项', '对应判定字段'], Object.keys(fm.field_map || {}).map(k =>
      [esc(k), esc(fm.field_map[k])])) +
    '<div class="hint">' + esc(fm.difficulty_hint || '') + '</div>' +
    Object.keys(settleBook).map(sid => {
      const f = settleBook[sid];
      return '<h4 style="font-size:12px;margin:13px 0 3px">' + esc(sid) +
        ' <span class="badge2">' + f.question_count + ' 题</span></h4>' +
        '<div class="hint">' + esc(f.intro || '') + '</div>' +
        table(['问题', '默认', '可选档位'], (f.questions || []).map(q =>
          [esc(q.label), '<code>' + esc(String(q.default)) + '</code>',
            (q.options || []).map(o => esc(o.label)).join(' / ') ||
            esc(q.placeholder || '自由填写')]));
    }).join('');

  const defs = r.proficiency_definition || {};
  const defTable = '<div class="hint">' + esc(defs.what_it_is || '') + '</div>' +
    table(['项', '定义'], Object.keys(defs.three_layers || {}).map(k =>
      ['<code>' + esc(k) + '</code>', esc(defs.three_layers[k])])) +
    table(['分级 / 成长', '规则'], [
      ['分级', esc((defs.grading || {}).range || '')],
      ['升级成本', esc((defs.grading || {}).cost || '')],
      ['达标门槛', esc((defs.grading || {}).gate || '')],
      ['退化', esc((defs.grading || {}).downgrade || '')],
      ['唯一入口', esc((defs.growth || {}).entry || '')],
      ['增益公式', '<code>' + esc((defs.growth || {}).gain || '') + '</code>'],
      ['判定顺序', esc(((defs.growth || {}).gate_order || []).join(' → '))],
      ['无效练习', esc((defs.growth || {}).invalid || '')],
      ['日历口径', esc((defs.growth || {}).daily_rule || '')]
    ]) +
    '<h4 style="font-size:12px;margin:12px 0 4px">熟练度不是什么</h4>' +
    (defs.what_it_is_not || []).map(x => '<div class="plain"><p>' + esc(x) + '</p></div>').join('') +
    '<h4 style="font-size:12px;margin:12px 0 4px">恒等式与自检</h4>' +
    (defs.invariants || []).map(x => '<div class="kv">· ' + esc(x) + '</div>').join('') +
    '<div class="hint">' + esc(defs.disclaimer || '') + '</div>';
  const reviewRows = (r.review_verification || []).map(c =>
    '<div class="plain"><b>' + esc(c.claim) + '</b><p><span class="' +
    (String(c.verdict).indexOf('不') === 0 ? 'note-bad' : 'note-ok') + '">' +
    esc(c.verdict) + '</span> · ' + esc(c.evidence) + '</p><p>处理：' +
    esc(c.action) + '</p></div>').join('');

  $('rule-box').innerHTML =
    fold('熟练度是什么 · 分级与成长规则', defTable, true) +
    fold('一句话读懂（大白话 ' + (r.plain_rules || []).length + ' 条）', plain) +
    fold('本轮审查的反向核实（' + (r.review_verification || []).length + ' 条）', reviewRows) +
    fold('低摩擦挂机 · 技能结算问卷（含 ' +
      (r.capability_verification || []).length + ' 条能力核实）', lowFriction) +
    fold('七个技能各自的结算问卷 · 与判定字段一一对应', settleTable) +
    fold('升级 · 退化 · 卡级边界', boundaries) +
    fold('每次熟练度增加的判定顺序', gates + reasons) +
    fold('系数与阈值', table(['项', '值'], constRows) + decay) +
    fold('LV1–LV8 通用达标条件', criteria) +
    fold('七个技能各自的评判标准', skillTables) +
    fold('连胜规则与自动成就', streak + achDef) +
    fold('权威标准来源', sources) +
    fold('已拍板并生效的 12 项决策', decided) +
    fold('诊断修正：反向核实（' + (r.diagnosis_verification || []).length + ' 条）', diag) +
    fold('形状修正参数（可回退 / 不含随机与惩罚）', shapeTable) +
    fold('本轮三层意见的反向核实（' + (r.source_verification || []).length + ' 条）', verification) +
    fold('验证 · 叙事 · 利他 三层规则', threeLayers) +
    fold('参考文章的反向核实（不照抄数字）', checks) +
    fold('参考 ulives 的机制对照', ulives) +
    fold('计算公式', formula);
}

/* ---------------- 拍板记录（设置页） ---------------- */
function renderDecisions(d) {
  decData = d;
  $('dec-count').textContent = '已拍板 ' + d.decided_count + ' / ' + d.total;
  $('dec-list').innerHTML = d.decisions.map(x => {
    const cur = d.selections[x.id];
    const bad = cur && cur.key !== x.selected;
    return '<details><summary>' + esc(x.group) + ' · ' + esc(x.id) + '</summary><div class="dbody">' +
      '<div class="hint">' + esc(x.question) + '</div>' +
      '<div class="kv">拍板：<code>' + esc(x.selected) + '</code>（原推荐 ' + esc(x.recommended) + '）</div>' +
      '<div class="kv" style="margin-top:5px">' + esc(x.implementation) + '</div>' +
      (bad ? '<div class="kv note-bad" style="margin-top:5px">记录与已实施规则不一致，点「恢复已拍板选择」修复</div>' : '') +
      '</div></details>';
  }).join('');
}

/* ---------------- 抽屉 ---------------- */
function openSheet(skillId) {
  if (!rulesData) { toast('规则尚未载入'); return; }
  if (skillId) $('p-skill').value = skillId;
  renderMetricInputs($('p-skill').value);
  renderSurveyBlock();
  $('mask').classList.add('on'); $('sheet').classList.add('on');
}
function closeSheet() { $('mask').classList.remove('on'); $('sheet').classList.remove('on'); }
$('mask').onclick = () => { closeSheet(); closeSettle(); closeBaseline(); };

function renderMetricInputs(skillId, boxId) {
  const rule = rulesData && rulesData.skill_rules[skillId];
  const box = $(boxId || 'p-metrics');
  if (!rule) { box.innerHTML = ''; return; }
  let h = '<div class="hint" style="margin:4px 0">本次观测值（缺一不可）</div>';
  rule.metrics.forEach(m => {
    h += '<div class="metric"><span>' + esc(m.label) + ' <small>≥' + m.min_valid +
      ' 有效 / ≥' + m.good + ' 加成</small></span>' +
      '<input type="number" data-metric="' + esc(m.key) + '" min="' + m.range_low +
      '" max="' + m.range_high + '" value="' + m.min_valid + '"></div>';
  });
  if (rule.medical_gate) {
    h += '<div class="metric"><span>疼痛/不适 <small>0 无 / 1 有</small></span>' +
      '<input type="number" data-metric="pain_flag" min="0" max="1" value="0"></div>' +
      '<div class="metric"><span>Borg RPE <small>6–20</small></span>' +
      '<input type="number" data-metric="rpe" min="6" max="20" value="12"></div>' +
      '<div class="metric"><span>停止信号 <small>0 无 / 1 有</small></span>' +
      '<input type="number" data-metric="stop_sign" min="0" max="1" value="0"></div>';
  }
  box.innerHTML = h;
}
$('p-skill').onchange = () => renderMetricInputs($('p-skill').value);

function collectMetrics(boxId) {
  const m = {};
  $(boxId || 'p-metrics').querySelectorAll('[data-metric]').forEach(el => { m[el.dataset.metric] = Number(el.value); });
  return m;
}
function defaultMetrics(skillId, mode) {
  const rule = rulesData.skill_rules[skillId]; const m = {};
  rule.metrics.forEach(x => { m[x.key] = mode === 'good' ? x.good
    : mode === 'fail' ? Math.max(x.range_low, x.min_valid - 1) : x.min_valid; });
  if (rule.medical_gate) {
    m.pain_flag = mode === 'pain' ? 1 : 0;
    m.rpe = mode === 'rpe_high' ? 19 : 12;
    m.stop_sign = mode === 'stop' ? 1 : 0;
  }
  return m;
}
function applyPreset(mode) {
  const sid = $('p-skill').value;
  const m = defaultMetrics(sid, mode);
  $('p-metrics').querySelectorAll('[data-metric]').forEach(el => {
    if (m[el.dataset.metric] != null) el.value = m[el.dataset.metric];
  });
  const p = { good: [5, 92], normal: [3, 80], sloppy: [2, 30] }[mode];
  if (p) { $('p-d').value = p[0]; $('p-q').value = p[1]; }
  toast('已填入「' + mode + '」预设');
}
$('quick').innerHTML = [['good', '高质量'], ['normal', '标准'], ['sloppy', '敷衍'],
  ['fail', '指标不达标'], ['pain', '报告疼痛'], ['stop', '停止信号'], ['rpe_high', 'RPE 过高']]
  .map(x => '<button class="mini" data-preset="' + x[0] + '">' + x[1] + '</button>').join('');
document.querySelectorAll('[data-preset]').forEach(b => {
  b.onclick = () => applyPreset(b.dataset.preset);
});

async function post(url, body, okMsg) {
  try {
    const data = await api(url, body);
    const v = data.verdict;
    if (v) {
      if (v.valid) floatXp(v.gain);
      toast((v.valid ? '有效修行 +' + v.gain : '未计入 · ' + v.reason_code) +
        ' · ' + (data.reason_text || ''));
      if (data.narrative) $('sys-whisper').textContent = data.narrative;
    } else if (okMsg) toast(okMsg);
    if (data.action_result) showAltruism(data);
    if (data.verdict) renderFactors(data);
    else $('factors-card').style.display = 'none';
    if (data.level_up) showLevelUp(data.level_up);
    render(data);
  } catch (e) { toast('操作失败：' + e.message); }
}
function showAltruism(data) {
  const ar = data.action_result || {};
  const text = data.reason_text || ar.reason_code || '';
  toast((ar.ok ? '已记入 · ' : '未计入 · ') + text);
  $('sys-whisper').textContent = ar.ok
    ? (ar.reputation ? '功德 +' + ar.reputation + '。' : '') + text
    : text;
}
async function altruism(action, extra) {
  try { post('/api/altruism', Object.assign({ action: action }, extra || {})); }
  catch (e) { toast('操作失败：' + e.message); }
}
$('p-submit').onclick = () => {
  const key = $('p-key').value.trim() || ('修行-' + (++counter));
  post('/api/practice', {
    skill_id: $('p-skill').value, difficulty: Number($('p-d').value),
    quality: Number($('p-q').value), feedback: $('p-f').value,
    task_key: key, metrics: collectMetrics(),
    proof_type: $('p-proof').value,
    proof_url: $('p-proof-url').value.trim().slice(0, 512),
    note: $('p-note').value.trim().slice(0, 500),
    survey: collectSurvey()
  });
  $('p-key').value = '';
  $('p-note').value = '';
  closeSheet();
};

/* ---------------- 问卷：首登起点 + 每次练习微调 ---------------- */
let surveyData = null;
let baseTargets = [];
let basePrompted = false;
function optionHtml(list, blank) {
  return (blank ? '<option value="">' + esc(blank) + '</option>' : '') +
    list.map(o => '<option value="' + esc(o.value) + '">' + esc(o.label) + '</option>').join('');
}
function renderSurveyBlock() {
  const box = $('p-survey');
  if (!surveyData || !surveyData.session) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="hint" style="margin:8px 0 4px">这一次的状态（可不填；只微调 ±20%，不改判定对错）</div>' +
    surveyData.session.map(q => '<div class="row"><label>' + esc(q.label) +
      '<select data-survey="' + esc(q.key) + '">' + optionHtml(q.options, '不填') +
      '</select></label></div>').join('');
}
function collectSurvey() {
  const box = $('p-survey');
  if (!box || !surveyData || !surveyData.session) return null;
  const out = {};
  for (const q of surveyData.session) {
    const el = box.querySelector('[data-survey="' + q.key + '"]');
    if (!el || !el.value) return null;
    out[q.key] = el.value;
  }
  return out;
}
async function ensureSurveys() {
  if (surveyData) return surveyData;
  try { surveyData = await api('/api/surveys'); } catch (e) { surveyData = null; }
  return surveyData;
}
function openBaseline() {
  const d = skillData;
  if (!surveyData || !d || !d.baseline || !d.baseline.pending.length) return;
  baseTargets = d.baseline.pending.slice();
  $('base-body').innerHTML =
    '<div class="row"><label>技能 <select id="base-skill">' +
    baseTargets.map(s => '<option value="' + esc(s) + '">' + esc(s) + '</option>').join('') +
    '</select></label></div>' +
    surveyData.baseline.map(q => '<div class="row"><label>' + esc(q.label) +
      '<select data-base="' + esc(q.key) + '">' + optionHtml(q.options) +
      '</select></label></div>').join('');
  $('base-hint').textContent = '还有 ' + baseTargets.length +
    ' 个技能没定起点；这四问只给起始等级（最高 LV3），不送经验。';
  $('mask').classList.add('on'); $('base-sheet').classList.add('on');
}
function closeBaseline() { $('base-sheet').classList.remove('on'); $('mask').classList.remove('on'); }
async function submitBaseline() {
  const skill = $('base-skill') ? $('base-skill').value : '';
  if (!skill) { closeBaseline(); return; }
  const answers = {};
  document.querySelectorAll('#base-body [data-base]').forEach(el => { answers[el.dataset.base] = el.value; });
  try {
    const res = await api('/api/baseline', { skill_id: skill, answers });
    const r = res.baseline_result || {};
    toast('已记下 ' + skill + ' 的起点 · LV' + (r.level || '?'));
    baseTargets = baseTargets.filter(x => x !== skill);
    if (baseTargets.length) { openBaseline(); } else { closeBaseline(); }
    render(res);
  } catch (e) { toast('起点未保存：' + e.message); }
}
$('base-submit').onclick = submitBaseline;
$('base-later').onclick = () => {
  try { sessionStorage.setItem('baseline-later', '1'); } catch (e) { /* 忽略 */ }
  closeBaseline();
};
$('dev-toggle').onchange = async () => {
  try {
    await api('/api/settings', { dev_tools: $('dev-toggle').checked });
    toast($('dev-toggle').checked ? '开发者工具已开启' : '开发者工具已关闭');
    render(await api('/api/skills'));
  } catch (e) { toast('切换失败：' + e.message); }
};
$('open-sheet').onclick = () => openSheet();

/* ---------------- 低摩擦挂机：候选时段 + 最多两问结算 ----------------
   能力边界：只观察本页自己的 visibilityState，不读手机屏幕状态、不读前台 App、
   不在后台常驻。页面隐藏期间的区间在「回到页面 / 重新打开」时按墙钟补齐。
   候选时长绝不自动折算熟练度，只有 settle 且用户点「算」才走原判定门。      */
const CAP_KEY = 'proficiency_capture_v1';
let captureData = null, settleSession = null, settleFields = null;

function capLocal() {
  try { return JSON.parse(localStorage.getItem(CAP_KEY) || 'null'); }
  catch (e) { return null; }
}
function capSave(v) {
  try {
    if (v) localStorage.setItem(CAP_KEY, JSON.stringify(v));
    else localStorage.removeItem(CAP_KEY);
  } catch (e) { /* 存储不可用时退化为当前页面内计时，不谎称后台监控 */ }
}
function capIntervals() {
  const st = capLocal();
  if (!st || !st.sessionId) return [];
  const list = (st.hidden || []).slice();
  if (st.hiddenFromMs != null) {
    list.push([Math.max(0, Math.round((st.hiddenFromMs - st.startedMs) / 1000)),
               Math.max(0, Math.round((Date.now() - st.startedMs) / 1000))]);
  }
  return list.filter(x => x[1] > x[0]);
}
document.addEventListener('visibilitychange', () => {
  const st = capLocal();
  if (!st || !st.sessionId) return;
  if (document.hidden) {
    if (st.hiddenFromMs == null) st.hiddenFromMs = Date.now();
  } else if (st.hiddenFromMs != null) {
    (st.hidden = st.hidden || []).push([
      Math.max(0, Math.round((st.hiddenFromMs - st.startedMs) / 1000)),
      Math.max(0, Math.round((Date.now() - st.startedMs) / 1000))]);
    st.hiddenFromMs = null;
  }
  capSave(st);
});

/* 启动自愈：上次没点收工，重开页面时自动转成「待结算」小票，不自动加分。 */
async function bootCapture() {
  const st = capLocal();
  if (!st || !st.sessionId) return;
  try {
    await api('/api/capture/stop', {
      session_id: st.sessionId, hidden_intervals: capIntervals(), note: st.note || '' });
    toast('上次挂机已收口，等你确认');
  } catch (e) { /* 服务端可能已收口或存档已清空，不补分 */ }
  capSave(null);
}
function renderCapture(d) {
  captureData = d.capture || captureData;
  if (!captureData) return;
  const active = (captureData.active || [])[0];
  const st = capLocal();
  if (st && st.sessionId && (!active || active.id !== st.sessionId)) capSave(null);
  const btn = $('cap-toggle');
  if (active) {
    $('cap-badge').innerHTML = '<span class="cap-pill">挂 机 中</span>';
    btn.textContent = '收 工'; btn.classList.add('stop');
    const mins = Math.max(0, Math.floor(
      (Date.now() - (st && st.sessionId === active.id ? st.startedMs : Date.now())) / 60000));
    $('cap-note').textContent = '本页在前台约 ' + mins +
      ' 分钟。重新打开页面会自动收口成待确认小票；时长不折算经验，不读手机屏幕、不知道前台 App。';
  } else {
    $('cap-badge').textContent = '未开工';
    btn.textContent = '开 工'; btn.classList.remove('stop');
    $('cap-note').textContent = '只记录「这个页面在前台多久」，不读手机屏幕、' +
      '不知道你开了哪个 App。时长不会自己变成熟练度；收工后按技能出一份结算问卷，' +
      '每题已默认选中，可一键确认。';
  }
  const pending = captureData.pending || [];
  $('cap-list').innerHTML = pending.map(s => {
    const canReuse = s.prefill && s.prefill.available;
    return '<div class="cap-item"><div class="t">' + esc(s.skill_id) + ' · 待结算 ' +
      Math.round(s.visible_seconds / 60) + ' 分钟</div><div class="s">' +
      (canReuse ? '可填技能问卷，也可一键沿用上次' : '按技能问卷填一次观测值') +
      ' · 跳过 / 丢弃 = 不计分、不扣分</div><div class="row">' +
      '<button class="mini pri" data-cap-settle="' + esc(s.id) + '">填 问 卷 结 算</button>' +
      '<button class="mini" data-cap-drop="' + esc(s.id) + '">丢 掉</button></div></div>';
  }).join('');
  document.querySelectorAll('[data-cap-settle]').forEach(b => {
    b.onclick = () => openSettle(b.dataset.capSettle);
  });
  document.querySelectorAll('[data-cap-drop]').forEach(b => {
    b.onclick = () => post('/api/capture/discard',
      { session_id: b.dataset.capDrop }, '小票已丢弃');
  });
}
setInterval(() => {
  if (captureData) renderCapture({ capture: captureData });
}, 30000);

async function capStart() {
  const sid = $('cap-skill').value;
  if (!sid) { toast('先选一个技能'); return; }
  try {
    const data = await api('/api/capture/start', { skill_id: sid });
    const row = data.capture_session;
    capSave({ sessionId: row.id, skillId: sid, startedMs: Date.now(),
              hidden: [], hiddenFromMs: document.hidden ? Date.now() : null,
              note: '' });
    render(data);
    toast('已开工。回来点一下就能结算');
  } catch (e) { toast('开工失败：' + e.message); }
}
async function capStop() {
  const st = capLocal();
  if (!st || !st.sessionId) { toast('没有进行中的挂机'); return; }
  try {
    const data = await api('/api/capture/stop', {
      session_id: st.sessionId, hidden_intervals: capIntervals(), note: st.note || '' });
    capSave(null);
    render(data);
    const r = data.capture_session;
    toast(r.status === 'pending' ? '已收口，点小票确认一下'
      : ('小票作废：' + (r.reason || r.status)));
  } catch (e) { toast('收口失败：' + e.message); }
}
$('cap-toggle').onclick = () => {
  const active = captureData && (captureData.active || [])[0];
  active ? capStop() : capStart();
};

function closeSettle() {
  $('settle-sheet').classList.remove('on');
  $('mask').classList.remove('on');
  settleSession = null; settleFields = null;
}
function openSettle(id) {
  const s = ((captureData && captureData.pending) || []).find(x => x.id === id);
  if (!s) { toast('这张小票已失效'); return; }
  settleSession = s;
  const form = s.form || { questions: s.questions || [], intro: '' };
  settleFields = {};
  (form.questions || []).forEach(q => { settleFields[q.key] = q.default; });
  $('settle-skill').textContent = s.skill_id + ' · ' +
    (form.question_count || (form.questions || []).length) + ' 题';
  $('settle-hint').textContent = s.skill_id + ' · 本页前台约 ' +
    Math.round(s.visible_seconds / 60) + ' 分钟。' + (form.intro || '') +
    ' 每题已默认选中，可一个字不填直接确认；跳过或关闭 = 不计入、不扣分。';
  $('s-reuse').style.display = form.reuse_available ? '' : 'none';
  drawSettle();
  $('mask').classList.add('on');
  $('settle-sheet').classList.add('on');
}
const SETTLE_GROUPS = [
  ['metric', '这 次 做 了 多 少'],
  ['safety', '身 体 有 没 有 报 警'],
  ['task', '做 的 是 什 么'],
  ['judgement', '难 度 · 反 馈 · 完 成 度'],
  ['note', '随 手 记 一 笔']
];
function drawSettle() {
  const s = settleSession, form = s.form || {}, qs = form.questions || [];
  let h = '';
  SETTLE_GROUPS.forEach(g => {
    const rows = qs.filter(q => q.group === g[0]);
    if (!rows.length) return;
    h += '<div class="sect">' + esc(g[1]) + '</div>';
    rows.forEach(q => {
      const cur = settleFields[q.key];
      h += '<div class="qbox"><div class="ql">' + esc(q.label) + '</div>' +
        '<div class="qw">' + esc(q.hint || '') + '</div>';
      if (q.kind === 'text') {
        h += '<input type="text" class="qtext" data-fkey="' + esc(q.key) +
          '" placeholder="' + esc(q.placeholder || '') +
          '" value="' + esc(cur == null ? '' : cur) + '">';
      } else {
        h += '<div class="qopts">' + (q.options || []).map(o =>
          '<button type="button" data-fkey="' + esc(q.key) + '" data-val="' +
          esc(o.value) + '" class="' +
          (String(cur) === String(o.value) ? 'on' : '') + '">' +
          esc(o.label) + '</button>').join('') + '</div>';
        if (q.kind === 'number_choice') {
          h += '<div class="qnum"><span>或手填 ' + esc(q.unit || '') +
            '（' + q.min + '–' + q.max + '，最低线 ' + q.min_valid +
            '，加成线 ' + q.good + '）</span>' +
            '<input type="number" data-nkey="' + esc(q.key) + '" min="' +
            q.min + '" max="' + q.max + '" value="' + esc(cur) + '"></div>';
        }
        if (q.kind === 'task_choice' && q.allow_custom) {
          h += '<div class="qnum"><input type="text" class="qtext" data-ckey="' +
            esc(q.key) + '" placeholder="' + esc(q.custom_placeholder || '自己写') +
            '" value="' + esc(settleFields.task_custom || '') + '"></div>';
        }
      }
      h += '</div>';
    });
  });
  h += '<div class="qfoot">' + esc(form.submit_hint || '') + '</div>' +
       '<div class="qfoot">' + esc(form.disclaimer || '') + '</div>';
  $('settle-body').innerHTML = h;
  bindSettleInputs();
}
function bindSettleInputs() {
  const qs = (settleSession.form && settleSession.form.questions) || [];
  document.querySelectorAll('#settle-body button[data-fkey]').forEach(b => {
    b.onclick = () => {
      const key = b.dataset.fkey;
      const q = qs.find(x => x.key === key) || {};
      let v = b.dataset.val;
      // 反馈与任务名是字符串；其余档位都是整数。
      if (key !== 'feedback' && key !== 'task') v = Number(v);
      settleFields[key] = v;
      if (key === 'task') delete settleFields.task_custom;
      drawSettle();
    };
  });
  document.querySelectorAll('#settle-body [data-nkey]').forEach(el => {
    el.onchange = () => {
      const v = Number(el.value);
      if (!Number.isFinite(v)) return;
      settleFields[el.dataset.nkey] = v;
      drawSettle();
    };
  });
  document.querySelectorAll('#settle-body [data-ckey]').forEach(el => {
    el.oninput = () => {
      const v = el.value.trim();
      if (v) settleFields.task_custom = v; else delete settleFields.task_custom;
    };
  });
  document.querySelectorAll('#settle-body input.qtext[data-fkey]').forEach(el => {
    el.oninput = () => { settleFields[el.dataset.fkey] = el.value; };
  });
}
$('s-later').onclick = () => {
  post('/api/capture/settle',
    { session_id: settleSession.id, answers: { mode: 'form', confirm: 'later' } },
    '小票已保留');
  closeSettle();
};
$('s-discard').onclick = () => {
  post('/api/capture/discard', { session_id: settleSession.id }, '小票已丢弃');
  closeSettle();
};
$('s-reuse').onclick = () => {
  post('/api/capture/settle', {
    session_id: settleSession.id,
    answers: { mode: 'reuse', confirm: 'yes', values: 'same' }
  }, '小票已处理');
  closeSettle();
};
$('s-submit').onclick = () => {
  post('/api/capture/settle', {
    session_id: settleSession.id,
    answers: { mode: 'form', confirm: 'yes',
               fields: Object.assign({}, settleFields) }
  }, '小票已处理');
  closeSettle();
};

/* ---------------- 基准复测 / 数据主权 / 时区 ---------------- */
function renderBenchmark(d) {
  const b = d.benchmark || {};
  $('bench-card').style.display = 'block';
  const cards = b.active_cards || [];
  $('bench-badge').textContent = cards.length
    ? (cards.length + ' 个基准 · ' + (b.pass_count || 0) + ' 次通过')
    : '未设基准';
  $('bench-list').innerHTML = cards.length ? cards.map(c => {
    const last = (c.results || []).slice(-1)[0];
    return '<div class="cap-item"><div class="t">' + esc(c.skill_id) +
      ' · ' + esc(c.task_key) + '</div><div class="s">基线 ' + c.baseline + ' ' +
      esc(c.unit) + '（' + esc(c.metric_label) + '）· ' +
      (c.days_until_retest > 0 ? '还需 ' + c.days_until_retest + ' 天可复测'
                               : '现在可以按同口径复测') +
      (last ? '<br>上次复测 ' + last.actual + ' ' + esc(c.unit) + '：' +
        (last.passed ? '超过基线 ' : '未超过基线 ') +
        (last.delta > 0 ? '+' : '') + last.delta : '') +
      '</div></div>';
  }).join('') : '<div class="kv">还没有基准。选一条「客观度量」的真实记录设为基准，' +
    '四周后用同一任务、同一口径重测。</div>';
  const opts = (b.candidates || (d.events || []).filter(e =>
    e.kind === 'practice' && e.feedback === 'measured'));
  const sel = $('bench-event'), prev = sel.value;
  sel.innerHTML = opts.length
    ? opts.map(e => '<option value="' + e.seq + '">#' + e.seq + ' · ' +
        esc(e.skill_id) + ' · ' + esc(e.task_key || '未命名') + '</option>').join('')
    : '<option value="">无可用记录</option>';
  if (opts.some(e => String(e.seq) === prev)) sel.value = prev;
  $('bench-create').disabled = !opts.length;
  $('bench-note').textContent = b.declaration ||
    '同一任务、同一口径，隔一段时间重测一次。';
}
function renderData(d) {
  const tz = d.timezone || {};
  if (tz.offset_seconds != null) {
    $('tz-line').textContent = '日历口径：' + esc(tz.label) +
      ' · 本地日序号 ' + tz.local_day + ' / UTC 日序号 ' + tz.utc_day;
    const sel = $('tz-select');
    if (!Array.from(sel.options).some(o => o.value === String(tz.offset_seconds))) {
      sel.innerHTML += '<option value="' + tz.offset_seconds + '">自定义 ' +
        esc(tz.label) + '</option>';
    }
    sel.value = String(tz.offset_seconds);
  }
}
$('bench-create').onclick = () => {
  const seq = Number($('bench-event').value);
  if (!seq) { toast('先选一条客观度量的记录'); return; }
  post('/api/benchmark/create', { event_seq: seq }, '基准已建立');
};
$('tz-apply').onclick = () => post('/api/settings',
  { tz_offset_seconds: Number($('tz-select').value) }, '时区已更新');
$('data-export').onclick = async () => {
  try {
    const b = await api('/api/export');
    const blob = new Blob([JSON.stringify(b, null, 2)],
                          { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'proficiency-backup-' + new Date().toISOString().slice(0, 10) + '.json';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 3000);
    toast('备份已导出');
  } catch (e) { toast('导出失败：' + e.message); }
};
$('data-import').onclick = () => $('data-file').click();
$('data-file').onchange = async (ev) => {
  const f = ev.target.files && ev.target.files[0];
  if (!f) return;
  try {
    const bundle = JSON.parse(await f.text());
    if (!window.confirm('导入会覆盖当前全部存档。系统会自动先留一份现场备份，确定继续？')) return;
    post('/api/import', { bundle: bundle }, '导入完成');
  } catch (e) { toast('导入失败：' + e.message); }
  ev.target.value = '';
};
$('data-backup').onclick = () => post('/api/backup', {}, '现场备份已生成');

/* ---------------- 我的自定义技能 ---------------- */
function csMetricRow() {
  const box = document.createElement('div');
  box.className = 'row cs-metric';
  box.innerHTML =
    '<label>英文标识 <input data-cs="key" type="text" placeholder="practice_minutes" style="width:110px"></label>' +
    '<label>名称 <input data-cs="label" type="text" placeholder="练习时长" style="width:90px"></label>' +
    '<label>单位 <input data-cs="unit" type="text" placeholder="分钟" style="width:60px"></label>' +
    '<label>下界 <input data-cs="low" type="number" min="0" value="0" style="width:70px"></label>' +
    '<label>上界 <input data-cs="high" type="number" min="1" value="120" style="width:70px"></label>' +
    '<button class="mini" data-cs-del="1">移除</button>';
  box.querySelector('[data-cs-del]').onclick = () => {
    box.remove();
    if (!$('cs-metrics').querySelector('.cs-metric')) csMetricRow();
  };
  $('cs-metrics').appendChild(box);
}
function renderCustomSkills(d) {
  const block = $('cs-block');
  if (!d || !d.ok) { block.style.display = 'none'; return; }
  block.style.display = '';
  const list = d.skills || [];
  $('cs-count').textContent = list.length + ' / ' + d.max_custom_skills;
  $('cs-ai').textContent = d.ai && d.ai.configured
    ? ('AI 已配置：' + d.ai.model + ' @ ' + d.ai.endpoint_host +
       '；不填网址也会自动检索维基百科等公开资料，再让模型折算成 LV1–LV8 要求。')
    : 'AI 未配置：新增技能使用本地保守模板（未联网、不冒充 AI 判定）。';
  $('cs-list').innerHTML = list.length
    ? list.map(x => '<div class="kv" style="margin:4px 0">' +
        esc(x.skill_id) + ' · ' + x.metric_count + ' 项指标 · ' +
        (x.ai_used ? 'AI 联网生成' : '本地模板') +
        (x.reason ? ' · ' + esc(x.reason) : '') +
        ' <button class="mini" data-cs-remove="' + esc(x.skill_id) + '">删除</button></div>').join('')
    : '<div class="kv">还没有自定义技能。</div>';
  document.querySelectorAll('[data-cs-remove]').forEach(b => {
    b.onclick = async () => {
      const sid = b.dataset.csRemove;
      if (!window.confirm('删除「' + sid + '」会连同它已有的练习记录一起移出存档，' +
        '等级与事件都会消失（导出包里的旧备份不受影响）。确定删除？')) return;
      try {
        await api('/api/skills/custom/remove', { skill_id: sid });
        toast('已删除技能'); await refresh();
      } catch (e) { toast('删除失败：' + e.message); }
    };
  });
}
$('cs-add-metric').onclick = () => {
  if ($('cs-metrics').querySelectorAll('.cs-metric').length >= 5) {
    toast('最多 5 个指标'); return;
  }
  csMetricRow();
};
$('cs-submit').onclick = async () => {
  const err = $('cs-error'); err.textContent = '';
  const name = $('cs-name').value.trim();
  const metrics = [];
  $('cs-metrics').querySelectorAll('.cs-metric').forEach(row => {
    const v = k => row.querySelector('[data-cs="' + k + '"]').value.trim();
    metrics.push({ key: v('key'), label: v('label'), unit: v('unit'),
      range_low: Number(v('low')), range_high: Number(v('high')) });
  });
  const urls = $('cs-urls').value.split('\n').map(x => x.trim())
    .filter(Boolean).slice(0, 3);
  try {
    const d = await api('/api/skills/custom/add',
      { name: name, metrics: metrics, source_urls: urls });
    toast(d.provenance && d.provenance.ai_used
      ? 'AI 依据资料生成并添加' : '已按本地保守模板添加');
    $('cs-name').value = ''; $('cs-urls').value = '';
    await refresh();
  } catch (e) { err.textContent = e.message; }
};
if (!$('cs-metrics').querySelector('.cs-metric')) csMetricRow();

/* ---------------- 设置页动作 ---------------- */
$('cfg-apply').onclick = () => post('/api/config', {
  skill_id: $('cfg-skill').value, kind: 'power',
  base: Number($('cfg-base').value), param: $('cfg-k').value,
  max_level: Number($('cfg-max').value) }, '曲线已更新');
$('advance').onclick = () => post('/api/advance', { days: Number($('adv-days').value) }, '已结算');
$('adv-7').onclick = () => post('/api/advance', { days: 7 }, '已快进 7 天');
$('adv-120').onclick = () => post('/api/advance', { days: 120 }, '已快进 120 天');
$('reset').onclick = () => {
  if (!window.confirm('确定清空全部修行记录？模拟时钟也会归零。')) return;
  post('/api/reset', {}, '已重置');
};
$('dec-restore').onclick = async () => {
  try { renderDecisions(await api('/api/decisions', { restore: true })); toast('已恢复为拍板时的选择'); }
  catch (e) { toast('恢复失败：' + e.message); }
};

/* ---------------- 总渲染 ---------------- */
function render(d) {
  renderToday(d);
  renderWorld(d.world);
  renderJourney(d);
  renderCapture(d);
  renderBenchmark(d);
  renderData(d);
  const sel = [$('p-skill'), $('cfg-skill'), $('cap-skill')];
  sel.forEach(node => {
    const prev = node.value;
    node.innerHTML = d.skills.map(s => '<option value="' + esc(s.skill_id) + '">' +
      esc(s.skill_id) + '</option>').join('');
    if (d.skills.some(s => s.skill_id === prev)) node.value = prev;
  });
  $('save-path').textContent = '存档：' + d.save_path;
  $('verify').textContent = '一致性自检：' + (d.verify.length ? d.verify.join('；') : '正常');
  $('clock').textContent = '模拟时钟偏移：' + d.clock.offset_days + ' 天';
  const dev = !!d.dev_tools;
  $('dev-cfg').style.display = dev ? '' : 'none';
  $('dev-time').style.display = dev ? '' : 'none';
  $('dev-toggle').checked = dev;
  if (rulesData) renderMetricInputs($('p-skill').value);
  if (!basePrompted && d.baseline && d.baseline.pending && d.baseline.pending.length) {
    let later = false;
    try { later = sessionStorage.getItem('baseline-later') === '1'; } catch (e) { /* 忽略 */ }
    if (!later && surveyData) { basePrompted = true; openBaseline(); }
  }
}
async function refresh() {
  try {
    rulesData = await api('/api/rules');
    renderRules(rulesData);
    decData = await api('/api/decisions');
    renderDecisions(decData);
    await bootCapture();
    await ensureSurveys();
    render(await api('/api/skills'));
    try { renderCustomSkills(await api('/api/skills/custom')); }
    catch (e) { renderCustomSkills(null); }
  } catch (e) { toast('读取失败：' + e.message); }
}
if (!window.__PRODUCT_AUTH__) { refresh(); }
