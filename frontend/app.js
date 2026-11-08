const $ = id => document.getElementById(id);
let total = 0, blocked = 0, deviceSet = new Set(), voiceReady = false, speaking = false, closeTimer;
let is_eddie_active = false, exitTimer, confirming = false, confirmTimer, userClosed = false;
const dialog = $('assistant');
// The same Eddie moves between the dashboard and focused voice view; it is never cloned.
const dashboardEddieSlot = document.createElement('aside');
dashboardEddieSlot.id = 'dashboard-eddie'; dashboardEddieSlot.className = 'dashboard-eddie';
dashboardEddieSlot.setAttribute('aria-label', 'Eddie');
const dashboard = document.querySelector?.('main');
if (dashboard) { dashboard.append(dashboardEddieSlot); dashboardEddieSlot.append($('character')); }
function transitionEddie(update) {
  if (document.startViewTransition && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) return document.startViewTransition(update);
  update(); return null;
}
function updateCounts() { $('allowed').textContent = allowedCount.toLocaleString(); $('blocked-count').textContent = blocked.toLocaleString(); $('broker-count').textContent = brokerCount.toLocaleString(); }
let allowedCount = 0, brokerCount = 0;
function notice(message) { $('notice').textContent = message; $('notice').hidden = false; clearTimeout(notice.timer); notice.timer = setTimeout(() => $('notice').hidden = true, 7000); }
function addEvent(event) {
  if (!event || typeof event !== 'object' || !event.domain) return;
  total++; const action = String(event.action || event.decision || 'allow').toLowerCase(); if (action === 'block' || action === 'blocked') blocked++; if(['allow','allowed'].includes(action)) allowedCount++; if (event.broker) brokerCount++; updateCounts();
  const device = typeof event.device === 'object' ? event.device.name || event.device.ip || 'Unknown device' : event.device || event.device_name || event.client_ip || 'Unknown device';
  deviceSet.add(String(device)); $('requests').textContent = total.toLocaleString(); $('blocked').textContent = blocked.toLocaleString(); $('devices').textContent = deviceSet.size;
  renderEvent(event, device, action); flashDevice(event.device_mac);
}
// A feed line: a coloured status tag ([approved], [blocked], [flagged], [approved (ai)]) then the text.
const TAGS = { allow: 'approved', block: 'blocked', flag: 'flagged', spoof: 'spoofed', ai: 'ai', rule: 'rule', bypass: 'bypass?', geo: 'new country?', rollback: 'rolled back?', acr: 'acr blocked' };
// What the DNS engine does with each AI label (decision.py), coloured like that result. The AI never
// blocks: a tracker is flagged, and so is an unsure domain, which says unsure to tell them apart.
const AI_RESULTS = { needed: ['approved', 'allow'], unsure: ['unsure', 'flag'], tracker: ['flagged', 'flag'] };
function taggedLine(kind, text, result) {
  const line = document.createElement('span'), tag = document.createElement('span'), body = document.createElement('span');
  tag.className = 'tag tag-' + (result ? result[1] : kind); tag.textContent = result ? `[${result[0]} (ai)]` : `[${TAGS[kind]}]`; body.textContent = ' ' + text;
  line.append(tag, body); return line;
}
// Every feed row starts with its time, so DNS lines and notes line up.
const dateOf = event => (event.timestamp || event.ts) ? new Date(event.ts || (typeof event.timestamp === 'number' && event.timestamp < 1e12 ? event.timestamp * 1000 : event.timestamp)) : new Date();
function timeOf(event = {}) {
  const time = document.createElement('time'); const date = dateOf(event); time.textContent = Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString([], {hour12:false});
  return time;
}
function renderEvent(event, device, action) {
  if (!device) device = typeof event.device === 'object' ? event.device.name || event.device.ip : event.device || event.device_name || event.client_ip || 'Unknown device';
  action ||= String(event.action || event.decision || 'allow').toLowerCase();
  $('feed').querySelector('.empty')?.remove(); const row = document.createElement('div'); row.className = 'event';
  const time = timeOf(event);
  const kind = ['block','blocked'].includes(action) ? 'block' : ['flag', 'spoof'].includes(action) ? action : 'allow';
  const line = taggedLine(kind, `${device} → ${event.domain}${event.reason ? ' — ' + event.reason : ''}`);
  row.append(time, line);
  // Known data broker or ad exchange (the API's label): where data gets sold on, so it stands out.
  if (event.broker) { row.className = 'event broker'; const tag = document.createElement('span'); tag.className = 'tag tag-broker'; tag.textContent = ` [${event.broker}]`; line.append(tag); }
  $('feed').append(row);
  if (!$('assistant-logs').hidden) { $('voice-logs').append(row.cloneNode(true)); while ($('voice-logs').children.length > 150) $('voice-logs').firstChild.remove(); $('voice-logs').scrollTop = $('voice-logs').scrollHeight; }
  while ($('feed').children.length > 150) $('feed').firstChild.remove();
  $('feed').scrollTop = $('feed').scrollHeight;

}
// The pending action's read-back stays on screen until it is answered, dismissed or expires.
function setConfirming(on, ms = 0, prompt = '') {
  confirming = on; clearTimeout(confirmTimer);
  $('assistant-pending').textContent = on ? prompt : ''; $('assistant-pending').hidden = !(on && prompt);
  if (on) confirmTimer = setTimeout(() => { setConfirming(false); setAssistantState('listening'); }, ms);
}
function feedNote(kind, text, event, result) {
  const row = document.createElement('div'); row.className = 'event'; row.append(timeOf(event), taggedLine(kind, text, result));
  $('feed').querySelector('.empty')?.remove(); $('feed').append(row);
  while ($('feed').children.length > 150) $('feed').firstChild.remove();
  if (!$('assistant-logs').hidden) $('voice-logs').append(row.cloneNode(true));
  $('feed').scrollTop = $('feed').scrollHeight;
}
const ruleLine = rule => `${rule.action} ${rule.pattern} · ${rule.device || 'all devices'} · by ${rule.created_by}`;
function renderRules(list) {
  $('rules-count').textContent = list.length;
  modes.spoofing = list.filter(rule => rule.action === 'spoof').length; renderModes();
  lockedMacs = new Set(list.filter(rule => rule.pattern === '*' && rule.action === 'block' && rule.device_mac).map(rule => rule.device_mac)); if (deviceList.length) renderDevices(deviceList);
  // Every rule, in the rules sheet the footer's "rules (n)" opens; newest first, as the API lists them.
  const items = list.map(rule => { const li = document.createElement('li'); li.textContent = ruleLine(rule); return li; });
  if (!items.length) { const none = document.createElement('li'); none.textContent = 'no rules yet'; items.push(none); }
  $('rules-list').replaceChildren(...items);
}
async function loadRules() {
  try { const r = await fetch('/api/rules'); if (!r.ok) throw Error(); renderRules(await r.json()); }
  catch { $('rules-count').textContent = 'unavailable'; $('rules-list').replaceChildren(); modes.spoofing = 0; renderModes(); }
}
// Devices (#35, #119): the "devices" link bottom right opens one row per device: its name, editable,
// "---" until someone adds one; child or adult; then its IP, maker and what it looked up lately, so people
// can tell which is which. A row lights up while its device looks something up.
// New (pending) and denied devices (#98) come first, with approve / deny buttons that ask before acting.
const STATUS_TEXT = { pending: 'new', denied: 'denied' };
let deviceList = [], lockedMacs = new Set(), editingName = false, kidPending = null;
const deviceLabel = d => d.label || `--- (${d.ip || d.mac})`;
const listNames = list => list.map(deviceLabel).join(', ');
function deviceRow(d) {
  const li = document.createElement('li'); li.className = `device-row${STATUS_TEXT[d.status] ? ' ' + d.status : ''}`; li.dataset.mac = d.mac;
  const name = document.createElement('input'); name.value = d.label || ''; name.placeholder = '---'; name.maxLength = 40;
  name.title = 'add a title'; name.setAttribute('aria-label', `name for ${d.ip || d.mac}: add a title`);
  name.onfocus = () => { editingName = true; name.placeholder = 'add a title'; };
  name.onblur = () => { editingName = false; name.placeholder = '---'; saveName(d, name.value); };
  name.onkeydown = e => { if (e.key === 'Enter') name.blur(); if (e.key === 'Escape') { name.value = d.label || ''; name.blur(); } };
  const kid = document.createElement('button'); kid.className = 'kid-toggle'; kid.textContent = d.kid ? 'child' : 'adult';
  kid.setAttribute('aria-pressed', String(!!d.kid)); kid.title = d.kid ? "A child's device: kid mode locks it down. Click to mark it as an adult's." : "Click to mark it as a child's device for kid mode.";
  kid.onclick = () => setKid(d, !d.kid);
  const where = [d.ip || d.mac, d.vendor, d.hostname].filter(Boolean).join(' · ');
  const info = document.createElement('small');
  info.textContent = `${where}${lockedMacs.has(d.mac) ? ' · locked down' : ''} · ${d.trackers} tracker${d.trackers === 1 ? '' : 's'} blocked`;
  li.append(name, kid, info);
  if ((d.recent_domains || []).length) { const seen = document.createElement('small'); seen.textContent = `looked up: ${d.recent_domains.join(', ')}`; li.append(seen); }
  if (STATUS_TEXT[d.status]) {
    const decide = document.createElement('div'), tag = document.createElement('span'); decide.className = 'decide'; tag.textContent = `[${STATUS_TEXT[d.status]}]`; decide.append(tag);
    for (const decision of d.status === 'pending' ? ['approve', 'deny'] : ['approve']) {
      const button = document.createElement('button'); button.textContent = decision; button.onclick = () => decideDevice(d, decision); decide.append(button);
    }
    li.append(decide);
  }
  return li;
}
function renderDevices(list) {
  deviceList = list; $('device-count').textContent = list.length; renderKidMode();
  if (editingName) return;  // don't wipe a name being typed; it re-renders once the name is saved
  list = [...list.filter(d => STATUS_TEXT[d.status]), ...list.filter(d => !STATUS_TEXT[d.status])];
  $('device-list').replaceChildren(...list.map(deviceRow));
}
// Light up a device's row while it looks something up: the easiest way to tell which device is which.
function flashDevice(key) {
  if ($('devices-sheet').hidden || !key) return; key = String(key).replace(/^ip:/, '');
  const row = $('device-list').children && [...$('device-list').children].find(li => li.dataset.mac === key); if (!row) return;
  row.classList.add('seen'); clearTimeout(row.seenTimer); row.seenTimer = setTimeout(() => row.classList.remove('seen'), 900);
}
async function updateDevice(d, change) {
  const r = await control('/api/devices/update', {mac: d.mac, ...change}); const data = await r.json(); if (!r.ok) throw Error(data.message); return data;
}
async function saveName(d, value) {
  value = value.trim(); if (value === (d.label || '')) return renderDevices(deviceList);
  try { await updateDevice(d, {label: value || null}); notice(value ? `Named ${value}. Voice can find it as "${value}".` : `Name cleared; it shows as ---.`); }
  catch (e) { notice(`Name not saved: ${e.message || 'the traffic API did not answer.'}`); }
  loadDevices();
}
async function setKid(d, kid) {
  if (!kid && lockedMacs.has(d.mac) && !confirm(`${deviceLabel(d)} is locked down. Marking it as an adult's doesn't unlock it, and kid mode won't cover it any more. Mark it as an adult's anyway?`)) return;
  try {
    await updateDevice(d, {kid});
    const kids = deviceList.filter(x => x.kid && x.mac !== d.mac);
    notice(kid ? (kids.some(x => lockedMacs.has(x.mac)) && !lockedMacs.has(d.mac)
      ? `${deviceLabel(d)} is a child's device. Kid mode doesn't cover it until you turn kid mode on again.` : `${deviceLabel(d)} is a child's device.`)
      : `${deviceLabel(d)} is an adult's device.`);
  } catch (e) { notice(`Not changed: ${e.message || 'the traffic API did not answer.'}`); }
  loadDevices();
}
// Kid mode: on when every child's device is locked down (a device "*" block rule), shown only from stored rules.
function renderKidMode() {
  const kids = deviceList.filter(d => d.kid), locked = kids.filter(d => lockedMacs.has(d.mac)), button = $('kid-mode');
  const on = kids.length > 0 && locked.length === kids.length;
  button.textContent = kidPending !== null ? `kid mode: turning ${kidPending ? 'on' : 'off'}…`
    : `kid mode: ${!kids.length ? 'off' : on ? 'on' : locked.length ? `on for ${locked.length} of ${kids.length}` : 'off'}`;
  button.disabled = kidPending !== null || !kids.length; button.setAttribute('aria-pressed', String(on)); button.setAttribute('aria-busy', String(kidPending !== null));
  $('kid-note').textContent = !kids.length ? "set a device to child to use kid mode" : `children's devices: ${listNames(kids)}`;
  // The heading's kid flag, like the house for home mode: shown while any child's device is locked down.
  const flag = $('kid-flag'), label = on ? 'kid mode on' : `kid mode on for ${locked.length} of ${kids.length}`;
  flag.hidden = !locked.length; flag.setAttribute('aria-label', label); flag.setAttribute('title', label);
  // No backend reports kid mode, so Eddie dresses for it from the same stored rules, whenever the flag shows.
  modes.kid_mode = locked.length > 0; renderEddieVisual();
}
async function toggleKidMode() {
  const kids = deviceList.filter(d => d.kid), locked = kids.filter(d => lockedMacs.has(d.mac)), on = locked.length < kids.length;
  if (!kids.length || kidPending !== null) return;
  kidPending = on; renderKidMode();
  try { const r = await control('/api/kid-mode', {on}); const data = await r.json(); if (!r.ok) throw Error(data.message); notice(data.message); }
  catch (e) { notice(`Kid mode not confirmed: ${e.message || 'the bridge did not answer.'}`); }
  finally { kidPending = null; loadRules(); loadDevices(); }
}
function toggleDevices(open) {
  if (open) toggleRules(false);
  $('devices-sheet').hidden = !open; $('open-devices').setAttribute('aria-expanded', String(open));
  if (open) { loadRules(); loadDevices(); }
}
// Rules sit beside devices in the footer; one sheet is open at a time.
function toggleRules(open) {
  if (open) toggleDevices(false);
  $('rules-sheet').hidden = !open; $('open-rules').setAttribute('aria-expanded', String(open));
  if (open) loadRules();
}
// The answer shown is the API's: stored, and whether DNS has loaded it.
async function decideDevice(d, decision) {
  const ask = decision === 'approve' ? `Approve ${d.name}? It gets full internet through Eddie.`
    : `Deny ${d.name}? Eddie blocks everything it looks up except the essentials, even with quarantine off.`;
  if (!confirm(ask)) return;
  try {
    const r = await control('/api/devices/decide', {mac: d.mac, decision}); const data = await r.json(); if (!r.ok) throw Error(data.message || data.detail);
    notice(`${data.name} ${data.status}${data.applied ? '' : ', not yet loaded by DNS'}.`);
  } catch (e) { notice(`Device change not confirmed: ${e.message || 'the traffic API did not answer.'}`); }
  loadDevices();
}
async function loadDevices() {
  try { const r = await fetch('/api/devices'); if (!r.ok) throw Error(); const data = await r.json(); renderDevices(data.devices || []); applyModes(data); }
  catch { $('device-count').textContent = 'unavailable'; if (!editingName) $('device-list').replaceChildren(); }
}
// Mode toggles show only state the backend acknowledged: a click shows "turning on…" until the API
// answers, and offline and home mode say when DNS hasn't loaded the change.
// Away mode (voice or the API) is shown only once DNS has loaded it, and drops at its end time.
// spoofing counts the stored spoof rules (#99); the house icon turns into Pinocchio while there are any.
const modes = {home_mode: null, home_manual: false, home_applied: null, offline: null, offline_applied: false, spoofing: 0, away: null, kid_mode: null, session_role: null, quarantine: null, quarantine_applied: false}, modePending = {};
const EDDIE_VISUALS = Object.freeze({
  regular: Object.freeze({accessory: 'none', stance: 'upright', motion: 'attentive'}),
  home: Object.freeze({accessory: 'roof', stance: 'settled', motion: 'breathe'}),
  kid: Object.freeze({accessory: 'propeller', stance: 'playful', motion: 'bounce'}),
  concierge: Object.freeze({accessory: 'bellhop', stance: 'formal', motion: 'hat-tip'}),
  away: Object.freeze({accessory: 'suitcase', stance: 'ready', motion: 'travel'})
});
// guest is Guest Mode as GET /api/guest last answered (#163), null while unknown. hatTipped: this guest session's
// one hat-tip (#162) has played; re-renders and reconnects keep it, and only a confirmed checkout re-arms it.
let visualConnected = true, guest = null, hatTipped = false;
function acknowledgedRole(state = modes) {
  if (!visualConnected) return 'regular';
  const explicit = state.session_role || state.role;
  if (['regular', 'home', 'kid', 'concierge', 'away'].includes(explicit)) return explicit;
  // Outfit order: kid, then guest, away and home; guest and home mode can both be on.
  if (state.kid_mode === true) return 'kid';
  if (guest?.active === true) return 'concierge';
  if (state.away?.on === true || state.away === true) return 'away';
  if (state.home_mode === true) return 'home';
  return 'regular';
}
function renderEddieVisual(state = modes) {
  const role = acknowledgedRole(state), visual = EDDIE_VISUALS[role], character = $('character');
  character.dataset.role = role; character.dataset.accessory = visual.accessory;
  character.dataset.stance = visual.stance; character.dataset.motion = visual.motion;
  if (role === 'concierge' && !hatTipped) { hatTipped = true; character.dataset.hatTip = 'once'; }
  else if (role !== 'concierge') { delete character.dataset.hatTip; if (visualConnected && guest?.active === false) hatTipped = false; }
}
renderEddieVisual();
function applyModes(state) {
  if (typeof state.home_mode === 'boolean') modes.home_mode = state.home_mode;
  if (typeof state.home_applied === 'boolean') { modes.home_applied = state.home_applied; modes.home_manual = state.home_manual === true; }
  if (typeof state.offline === 'boolean') { modes.offline = state.offline; modes.offline_applied = state.offline_applied === true; }
  if (typeof state.quarantine === 'boolean') { modes.quarantine = state.quarantine; modes.quarantine_applied = state.quarantine_applied === true; }
  if (typeof state.away === 'boolean') modes.away = {on: state.away, until: state.away_until || null, applied: state.away_applied === true,
    devices: state.away_devices || [], exempt: state.away_exempt || []};
  const hasRole = ['session_role', 'role', 'session'].some(key => Object.prototype.hasOwnProperty.call(state, key));
  const role = state.session_role || state.session?.role || state.role;
  if (hasRole && state.pending !== true && state.acknowledged !== false) modes.session_role = role === 'regular' ? null : ['home', 'kid', 'concierge', 'away'].includes(role) ? role : null;
  renderModes();
}
// [note, is it an alert]: away mode's state once DNS confirms it, else that DNS hasn't loaded the change.
function awayNote(away = modes.away) {
  if (!away) return ['', false];
  if (!away.applied) return [`away mode ${away.on ? 'on' : 'off'} not yet applied by DNS`, true];
  if (!away.on || (away.until && Date.now() >= away.until)) return ['', false];
  const names = list => list.map(d => d.name).join(', ');
  const until = away.until ? ` until ${new Date(away.until).toLocaleString([], {weekday: 'short', hour: 'numeric', minute: '2-digit'})}` : '';
  const cut = away.devices.length ? `cutting telemetry from ${names(away.devices)}` : 'no device seen showing someone home, nothing blocked';
  const kept = away.exempt.length ? ` · ${names(away.exempt)} stay${away.exempt.length === 1 ? 's' : ''} up` : '';
  return [`away mode: on${until} · ${cut}${kept}`, false];
}
// [note, is it an alert]: home mode (#20) once DNS confirms it, and whether phones or a manual change set it.
function homeNote() {
  if (modes.home_mode === null || modes.home_applied === null) return ['', false];
  if (!modes.home_applied) return [`home mode ${modes.home_mode ? 'on' : 'off'} not yet applied by DNS`, true];
  if (modes.home_mode) return [`home mode: blocking camera uploads · ${modes.home_manual ? 'turned on by hand' : 'a household phone is home'}`, false];
  return ['', false];  // off says nothing: the hollow house beside the heading already shows it
}
function renderModes() {
  for (const [id, label, key] of [['home-mode', 'home mode', 'home_mode'], ['offline-mode', 'offline mode', 'offline'], ['quarantine-mode', 'quarantine new devices', 'quarantine']]) {
    const button = $(id), pending = modePending[key], on = modes[key];
    // The footer's offline mode reads just "online" or "offline".
    const text = key === 'offline'
      ? (pending !== undefined ? `going ${pending ? 'offline' : 'online'}…` : on === null ? 'offline mode unavailable' : on ? 'offline' : 'online')
      : pending !== undefined ? `${label}: turning ${pending ? 'on' : 'off'}…` : `${label}: ${on === null ? 'unavailable' : on ? 'on' : 'off'}`;
    button.disabled = pending !== undefined || on === null;
    button.setAttribute('aria-busy', String(pending !== undefined)); button.setAttribute('aria-pressed', String(on === true));
    // Home mode is the house icon beside the heading, so its state is in the label, not the text.
    if (key === 'home_mode') {
      // A flag, not a switch: the house shows only while home mode is on (or the Pinocchio nose while spoof rules exist).
      button.hidden = !(on === true || pending === true || modes.spoofing > 0);
      const label = modes.spoofing ? `${text} · spoofing ${modes.spoofing} rule${modes.spoofing === 1 ? '' : 's'}` : text;
      button.setAttribute('aria-label', label); button.setAttribute('title', label); button.setAttribute('data-spoofing', String(modes.spoofing > 0));
    } else button.textContent = text;
  }
  renderMic();
  const waiting = modes.offline !== null && modePending.offline === undefined && !modes.offline_applied;
  $('offline-note').textContent = waiting ? `offline mode ${modes.offline ? 'on' : 'off'} not yet applied by DNS` : '';
  const qWaiting = modes.quarantine !== null && modePending.quarantine === undefined && !modes.quarantine_applied;
  $('quarantine-note').textContent = qWaiting ? 'quarantine change not yet applied by DNS'
    : 'when on, a device Eddie hasn\'t seen before gets only the essentials until you approve it here';
  $('quarantine-note').classList.toggle('alert', qWaiting);
  const [home, homeAlert] = homeNote();
  $('home-note').textContent = home; $('home-note').classList.toggle('alert', homeAlert);
  // Pending controls never enter modes; this renders only the last backend acknowledgement.
  renderEddieVisual();
  const [away, alert] = awayNote();
  $('away-note').textContent = away; $('away-note').classList.toggle('alert', alert);
}
// While the mic is on, the heading shows the mic icon and the footer button is gone: it's turned off
// by saying "stop recording". While off, the button reads "start mic", prefixed "offline •" once offline mode is confirmed on.
function renderMic() { $('mic-on').hidden = !voiceReady; $('mic').hidden = voiceReady; $('mic').textContent = 'start mic'; }
async function loadModes() {
  try { const r = await fetch('/api/mode'); if (!r.ok) throw Error(); applyModes(await r.json()); }
  catch { modes.home_mode = modes.offline = modes.away = modes.session_role = null; renderModes(); }
}
async function setMode(key, on) {
  if (modePending[key] !== undefined) return;
  modePending[key] = on; renderModes();
  const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 10000);
  try {
    const r = await control({home_mode: '/api/mode', offline: '/api/offline-mode', quarantine: '/api/quarantine'}[key], {[key]: on}, abort.signal);
    const data = await r.json(); if (!r.ok) throw Error(data.message);
    applyModes({...data, [`${key}_applied`]: data.applied});
  } catch (e) {
    notice(`${{home_mode: 'Home mode', offline: 'Offline mode', quarantine: 'Quarantine'}[key]} change not confirmed: ${e.name === 'AbortError' || !e.message ? 'the traffic API did not answer.' : e.message}`);
    loadModes();
  } finally { clearTimeout(timer); delete modePending[key]; renderModes(); }
}
// Ask before blocking (#79): the API's open question as a Block / Allow / Ignore card. The result line
// says "blocked" only when the API reports DNS applied the rule; Undo removes the rules this answer wrote.
let question = null, questionBusy = false, answered = null;
function renderQuestion() {
  const shown = question || answered; $('question').hidden = !shown; if (!shown) return;
  $('question-text').textContent = question ? question.prompt : 'answered';
  $('question-domains').replaceChildren(...(question ? question.domains : []).map(d => {
    const li = document.createElement('li'); li.textContent = d.domain; const small = document.createElement('small'); small.textContent = ` — ${d.reason}`; li.append(small); return li;
  }));
  $('question-actions').hidden = !question; for (const b of $('question-actions').children) b.disabled = questionBusy;
  $('question-result').textContent = answered ? answered.text : ''; $('question-result').classList.toggle('alert', !!answered?.alert);
  $('question-undo').hidden = !answered?.undo; $('question-undo').disabled = questionBusy;
}
function showQuestions(list) { question = Array.isArray(list) && list[0] || null; renderQuestion(); }
async function loadQuestions() {
  try { const r = await fetch('/api/questions'); if (!r.ok) throw Error(); showQuestions(await r.json()); } catch { /* keep what is shown */ }
}
function answerText(answer, result) {
  if (answer === 'ignore') return {text: 'Ignored. It stays flagged; no rule was added.'};
  const names = result.rules.map(r => r.pattern).join(', ');
  if (result.applied === true) return {text: `${answer === 'block' ? 'Blocked' : 'Allowed'} ${names} on every device. Eddie's DNS has loaded it.`, undo: true};
  return {text: `${answer === 'block' ? 'Block' : 'Allow'} saved for ${names}, but Eddie's DNS hasn't loaded it, so it isn't confirmed.`, alert: true, undo: true};
}
async function answerQuestion(answer) {
  if (!question || questionBusy) return; const id = question.id; questionBusy = true; renderQuestion();
  try {
    const r = await control('/api/questions/answer', {id, answer}); const data = await r.json(); if (!r.ok) throw Error(data.message);
    answered = {id, ...answerText(answer, data)}; if (question?.id === id) question = null;
  } catch (e) { notice(`Answer not confirmed: ${e.message || 'the bridge did not answer.'}`); loadQuestions(); }
  finally { questionBusy = false; renderQuestion(); }
}
async function undoAnswer() {
  if (!answered?.undo || questionBusy) return; questionBusy = true; renderQuestion();
  try {
    const r = await control('/api/questions/undo', {id: answered.id}); const data = await r.json(); if (!r.ok) throw Error(data.message);
    answered = {text: data.applied === false ? "Undone, but Eddie's DNS hasn't loaded the change yet." : 'Undone. The rule is removed.', alert: data.applied === false};
  } catch (e) { notice(`Undo not confirmed: ${e.message || 'the bridge did not answer.'}`); }
  finally { questionBusy = false; renderQuestion(); }
}
for (const b of $('question-actions').children) b.onclick = () => answerQuestion(b.dataset.answer);
$('question-undo').onclick = undoAnswer;
// Guest Mode (#163): the owner's switch and what guests may use, shown only as the bridge saved it.
// The Eddie computer or the paired iPad changes it, with no dialog; a refusal is the notice.
const GUEST_LABELS = {house_guide: 'house guide', timers: 'timers', tv: 'tv', lights: 'lights', thermostat: 'thermostat'};
function renderGuest() {
  renderEddieVisual();
  const button = $('guest-mode');
  button.disabled = !guest; button.setAttribute('aria-pressed', String(!!guest?.active));
  button.textContent = `guest mode: ${!guest ? '…' : guest.active ? 'on' : 'off'}`;
  $('guest-note').textContent = !guest ? '' : `${guest.active ? 'every voice is a guest. ' : ''}guests may use:`;
  $('guest-allowed').replaceChildren(...(guest ? guest.approvable.map(c => {
    const label = document.createElement('label'), box = document.createElement('input'), name = document.createElement('span');
    box.type = 'checkbox'; box.checked = guest.allowed.includes(c); name.textContent = GUEST_LABELS[c] || c;
    box.onchange = () => saveGuest({allowed: box.checked ? [...guest.allowed, c] : guest.allowed.filter(x => x !== c)});
    label.append(box, name); return label;
  }) : []));
  renderGuestCapabilities();
}
// Guest mode's own pane (#162/#165): the person flag next to home (filled while on; tapping it is the guest switch), and capabilities swapped for what guests may ask.
// Topic names only, never the guide's answers. It opens by itself when a guest session starts.
let guestShown = false;
function guestTopics() { return guest?.active && guest.allowed.includes('house_guide') ? guest.topics || [] : []; }
function renderGuestCapabilities() {
  const active = guest?.active === true, topics = guestTopics(), item = text => { const li = document.createElement('li'); li.textContent = text; return li; };
  const flag = $('guest-flag'), label = `guest mode: ${!guest ? 'unavailable' : active ? 'on' : 'off'}`;
  flag.disabled = !guest; flag.setAttribute('aria-pressed', String(active)); flag.setAttribute('aria-label', label); flag.title = label;
  $('capabilities-title').textContent = active ? 'guest mode · ask eddie' : 'what to ask eddie';
  $('capabilities-owner').hidden = active; $('guest-capabilities').hidden = !active;
  $('guest-intro').textContent = topics.length ? 'Say “Hey Eddie”, then ask about:' : guest?.allowed.includes('timers') ? 'Say “Hey Eddie”, then:' : 'Your host hasn’t turned anything on for guests yet.';
  $('guest-topics').replaceChildren(...topics.map(item), ...(active && guest.allowed.includes('timers') ? [item('“Set a timer for 10 minutes.”')] : []), ...(active ? [item('“Stop recording.”')] : []));
  if (active && !guestShown) showCapabilities(true);
  guestShown = active || (guestShown && !guest);
}
async function loadGuest() { try { const r = await fetch('/api/guest'); if (!r.ok) throw Error(); guest = await r.json(); } catch { guest = null; } renderGuest(); }
async function saveGuest(change) {
  try { const r = await control('/api/guest', change); const data = await r.json(); if (!r.ok) throw Error(data.message); guest = data; if ('active' in change) notice(`Guest mode is ${data.active ? 'on' : 'off'}.`); }
  catch (e) { notice(`Guest mode not saved: ${e.message || 'the bridge did not answer.'}`); }
  renderGuest();
}
$('guest-flag').onclick = $('guest-mode').onclick = () => guest && saveGuest({active: !guest.active});
// Counts move with traffic, so the panels refresh every 30 s as well as on (re)connect.
function refreshPanels() { clearTimeout(refreshPanels.timer); refreshPanels.timer = setTimeout(refreshPanels, 30000); loadDevices(); loadModes(); loadQuestions(); loadGuest(); }
// ACR blocker (#104): the latest acr_detected per device and domain, newest first.
const acrSeen = new Map();
function renderAcr() {
  const list = [...acrSeen.values()].sort((a, b) => b.ts - a.ts);
  $('acr').hidden = !list.length; $('acr-count').textContent = list.length;
  $('acr-list').replaceChildren(...list.slice(0, 3).map(e => { const li = document.createElement('li'); li.textContent = e.reason; return li; }));
}
// EDDIE_SPEAKER=ipad|both: Eddie's reply arrives as a WAV clip to play here. Safari plays sound only after a
// tap, so the first tap anywhere unlocks it; a reply that comes before then shows "tap to hear Eddie".
let voiceContext = null, pendingVoice = '';
function unlockVoice() {
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!Context) return;
  // Web Audio follows the iPad's Silent mode unless the page asks for playback, like a music app (Safari 16.4+).
  if (typeof navigator !== 'undefined' && navigator.audioSession) navigator.audioSession.type = 'playback';
  if (!voiceContext) voiceContext = new Context();
  if (voiceContext.state !== 'running') {
    voiceContext.resume();
    // Older iPads also want a sound started inside the tap itself.
    const silence = voiceContext.createBufferSource();
    silence.buffer = voiceContext.createBuffer(1, 1, 22050);
    silence.connect(voiceContext.destination);
    silence.start();
  }
  $('voice-unlock').hidden = true;
  if (pendingVoice) { const id = pendingVoice; pendingVoice = ''; playVoice(id); }
}
async function playVoice(id) {
  if (!id) return;
  if (!voiceContext || voiceContext.state !== 'running') { pendingVoice = id; $('voice-unlock').hidden = false; return; }
  try {
    const response = await fetch('/api/voice/audio/' + encodeURIComponent(id));
    if (!response.ok) return;
    const source = voiceContext.createBufferSource();
    source.buffer = await voiceContext.decodeAudioData(await response.arrayBuffer());
    source.connect(voiceContext.destination);
    source.start();
  } catch {}
}
// Safari counts the end of a tap (touchend, click) as permission to play, not pointerdown.
document.addEventListener('touchend', unlockVoice);
document.addEventListener('click', unlockVoice);
$('voice-unlock').onclick = unlockVoice;

function consumeEvent(event) {
  if (event.type === 'role_state' || event.type === 'session_state') { if (event.pending !== true && event.acknowledged === true) applyModes(event); return; }
  if (event.type === 'ai_verdict') return feedNote('ai', `${event.domain}${event.reason ? ' — ' + event.reason : ''}`, event, AI_RESULTS[event.label] || [String(event.label), 'flag']);
  if (event.type === 'mode') return applyModes(event);
  if (event.type === 'guest') { guest = event; return renderGuest(); }
  if (event.type === 'question') { question = event; return renderQuestion(); }
  if (event.type === 'question_resolved') { if (question?.id === event.id) { question = null; renderQuestion(); } return; }
  if (event.type === 'offline_mode') return applyModes({offline: event.offline, offline_applied: event.applied});
  if (event.type === 'quarantine') return applyModes({quarantine: event.quarantine, quarantine_applied: event.applied});
  if (event.type === 'new_device') {
    // Stored as new; "quarantine on" is the setting, not proof DNS is blocking it yet.
    const seen = timeOf(event).textContent, extra = [event.vendor, event.hostname].filter(Boolean).join(', ');
    const text = `A device I've never seen joined at ${seen}: ${event.device}${extra && extra !== event.device ? ` (${extra})` : ''}. ${event.quarantine ? 'Quarantine is on until you approve it.' : 'Approve or deny it under devices.'}`;
    feedNote('rule', text, event); notice(text); loadDevices();
    return;
  }
  if (event.type === 'device_status') { loadDevices(); return; }
  if (event.type === 'away_mode') return applyModes(event);
  // A suspicion, never proof: the reason says what was looked up and never that a device is covered.
  if (event.type === 'bypass_suspected') { feedNote('bypass', event.reason, event); notice(event.reason); return; }
  if (event.type === 'geo_alert') { feedNote('geo', event.reason, event); notice(event.reason); return; }
  // Only sent for a lookup the DNS server blocked; the interval is the API's measurement, never a guess.
  if (event.type === 'acr_detected') { feedNote('acr', event.reason, event); notice(event.reason); acrSeen.set(`${event.device_mac} ${event.domain}`, event); renderAcr(); return; }
  if (event.type === 'rule_rolled_back') {
    // A guess from traffic, never proof: the reason says so; applied false means DNS hasn't confirmed the block is gone.
    const text = event.applied ? event.reason : `${event.reason} Eddie's DNS hasn't confirmed the block is gone yet.`;
    feedNote('rollback', text, event); notice(text); loadRules();
    return;
  }
  if (event.type === 'rule_change') {
    // Stored, not enforced: the event is sent before DNS acknowledges it, so never say "blocked".
    const text = `Rule ${event.change === 'removed' ? 'removed' : 'added'}: ${ruleLine(event)}`;
    feedNote('rule', text, event); notice(text); loadRules();
    return;
  }
  if (event.domain) addEvent(event); else voiceEvent(event);
}
function setAssistantState(state) {
  // A pending risky action keeps asking for yes or no until it is answered or expires.
  if (confirming && ['idle','ready','listening'].includes(state)) state = 'confirming';
  const display = state === 'idle' ? 'ready' : state;
  $('character').dataset.state = display;
  $('character').classList.toggle('talking', state === 'speaking');
  $('assistant-state').textContent = display === 'ready' ? 'WAITING FOR YOUR NEXT QUESTION' : display === 'confirming' ? 'SAY YES TO CONFIRM OR NO TO CANCEL' : 'EDDIE IS ' + display.toUpperCase();
}
function wake(preview = false) { userClosed = false; clearTimeout(exitTimer); dialog.classList.remove('closing'); is_eddie_active = true; clearTimeout(closeTimer); if (preview || !voiceReady) closeTimer = setTimeout(dismiss, 4000); if (!dialog.open) transitionEddie(() => { dialog.prepend($('character')); dialog.showModal(); }); const topics = guestTopics(); $('assistant-title').textContent = guest?.active ? 'Welcome! How can I help?' : "Hey, what's up?"; $('assistant-description').textContent = !guest?.active ? 'Ask me what your devices have been up to.' : topics.length ? `Ask me about ${topics.slice(0, 3).join(', ')} and more.` : 'I’m in guest mode.'; setAssistantState('listening'); }
// Closing Eddie returns to the live feed, scrolled to the newest activity.
function dismiss() {
  is_eddie_active = false;
  setConfirming(false);
  clearTimeout(closeTimer);
  if (!dialog.open || dialog.classList.contains('closing')) return;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const sharedTransition = Boolean(document.startViewTransition) && !reduced;
  if (!sharedTransition) dialog.classList.add('closing');
  const duration = sharedTransition || reduced ? 0 : 320;
  exitTimer = setTimeout(() => {
    transitionEddie(() => {
      dialog.close(); dialog.classList.remove('closing'); dashboardEddieSlot.append($('character'));
      toggleLogs(false); $('feed').scrollTop = $('feed').scrollHeight;
    });
  }, duration);
}
// The user closed Eddie: end the voice conversation too, so the next request needs the wake phrase,
// and don't reopen for events the worker sent before it heard about it.
function closeByUser() { userClosed = true; dismiss(); control('/api/voice/dismiss').catch(() => {}); }
function reopen() { if (!userClosed && (!dialog.open || dialog.classList.contains('closing'))) wake(); }
function toggleLogs(open) {
  $('assistant-logs').hidden = !open;
  $('open-logs').setAttribute('aria-expanded', String(open));
  $('open-logs').textContent = open ? 'Close logs ×' : 'Open logs ↗';
  if (open) { $('voice-logs').replaceChildren(...Array.from($('feed').children, row => row.cloneNode(true))); $('voice-logs').scrollTop = $('voice-logs').scrollHeight; $('close-logs').focus(); }
}
function voiceEvent(event) {
  if (event.type === 'voice_state') { if (typeof event.is_eddie_active === 'boolean') is_eddie_active = event.is_eddie_active; if (event.state === 'confirming') setConfirming(true, Number(event.expires_in_ms) || 15000, String(event.prompt || '')); else if (['thinking','dismissed'].includes(event.state)) setConfirming(false); if (event.state === 'dismissed') { userClosed = false; return dismiss(); } if (['thinking','speaking','listening'].includes(event.state)) clearTimeout(closeTimer); if (event.state === 'listening') reopen(); setAssistantState(event.state); }
  if (event.type === 'agent_reply') { reopen(); $('assistant-description').textContent = event.text; }
  if (event.type === 'voice_status') { if (['off','error'].includes(event.status)) { $('character').classList.remove('talking'); if(dialog.open) dismiss(); } voiceReady = event.status === 'ready'; renderMic(); if (event.message && !voiceReady) notice(event.message); else if (voiceReady) $('notice').hidden = true; }
  if (event.type === 'wake') wake();
  if (event.type === 'transcript' && event.text) { reopen(); $('assistant-title').textContent = event.text; }
  if (event.type === 'speaking') { reopen(); $('assistant-title').textContent = event.title || 'Here’s what I’m seeing.'; $('assistant-description').textContent = event.text || ''; setAssistantState('speaking'); speaking = true; }
  if (event.type === 'speech_audio') playVoice(String(event.id || ''));
  // A spoken change (home mode, a rule) may have landed while this screen missed its event: reread them.
  if (event.type === 'speech_end') { loadModes(); loadRules(); }
  if (event.type === 'speech_end') { setAssistantState('ready'); speaking = false; }
}
// "What they know" report: real facts, or the seeded demo week under a demo banner.
function renderReport(report) {
  const demo = report.demo === true;
  $('report-title').textContent = demo ? `what they know · DEMO WEEK` : `what they know · last ${report.hours}h${report.source === 'eddie.db' ? ' · from eddie.db' : ''}`;
  $('report-demo').textContent = demo ? `DEMO DATA · ${report.label}` : ''; $('report-demo').hidden = !demo;
  const items = (report.facts || []).map(f => { const li = document.createElement('li'); li.textContent = `${f.category ? `${f.category}${f.sensitive ? ' · sensitive' : ''}: ` : ''}${f.fact} — ${f.evidence} (confidence ${Math.round(f.confidence * 100)}%)`; return li; });
  if (!items.length) { const li = document.createElement('li'); li.textContent = 'Not enough traffic yet to say what anyone could guess.'; items.push(li); }
  $('report-facts').replaceChildren(...items); $('report').hidden = false;
}
// Privacy settings coach (#112): the hand-written guide for each recognised device, with its sources.
function showSuggestions(open) { $('suggestions').hidden = !open; $('suggestions-open').setAttribute('aria-expanded', String(open)); if (open) loadSuggestions(); }
function renderSuggestions(data) {
  const blocks = (data.devices || []).map(({name, guide: d}) => {
    const div = document.createElement('div'); div.className = 'suggestion';
    const head = document.createElement('div'); head.className = 'suggestion-head'; head.textContent = `${name} · ${d.name}`;
    const steps = document.createElement('ol'); steps.append(...d.steps.map(s => { const li = document.createElement('li'); li.textContent = `${s.do}: ${s.setting} — ${s.why}`; return li; }));
    const sources = document.createElement('p'); sources.className = 'modes-note'; sources.append('sources: ');
    d.sources.forEach((s, i) => { const a = document.createElement('a'); a.href = s.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; a.textContent = s.title; sources.append(...(i ? ['; ', a] : [a])); });
    const where = document.createElement('p'); where.textContent = d.where; const note = document.createElement('p'); note.className = 'modes-note'; note.textContent = d.note;
    div.append(head, where, steps, note, sources); return div;
  });
  if (!blocks.length) { const p = document.createElement('p'); p.textContent = data.text || 'No recognised devices yet.'; blocks.push(p); }
  $('suggestions-list').replaceChildren(...blocks);
}
async function loadSuggestions() {
  try { const r = await fetch('/api/suggestions'); const data = await r.json(); if (!r.ok) throw Error(data.message || data.detail); renderSuggestions(data); }
  catch (e) { const p = document.createElement('p'); p.textContent = `unavailable${e.message ? `: ${e.message}` : ''}`; $('suggestions-list').replaceChildren(p); }
}
$('suggestions-open').onclick = () => showSuggestions($('suggestions').hidden);
// capabilities, bottom right: what to ask Eddie, in a small pane over the corner. Eddie points here
// instead of reading the whole list aloud.
function showCapabilities(open) { $('capabilities').hidden = !open; $('capabilities-open').setAttribute('aria-expanded', String(open)); }
$('capabilities-open').onclick = () => showCapabilities($('capabilities').hidden);
$('capabilities-close').onclick = () => { showCapabilities(false); $('capabilities-open').focus(); };
$('suggestions-close').onclick = () => { showSuggestions(false); $('suggestions-open').focus(); };
// Cross-device tracker map (#107): which tracking companies each device looked up, from GET /api/tracker-map.
function showTrackerMap(open) { $('tracker-map').hidden = !open; $('tracker-map-open').setAttribute('aria-expanded', String(open)); if (open) loadTrackerMap(); }
function el(tag, text, className) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (className) e.className = className; return e; }
function renderTrackerMap(data) {
  const companies = data.companies || [], devices = data.devices || [];
  $('tracker-map-text').textContent = data.text || '';
  const head = el('tr'); head.append(el('th', 'company'), ...devices.map(d => el('th', d)));
  const thead = el('thead'); thead.append(head);
  const tbody = el('tbody');
  for (const c of companies) {
    const seen = new Map((c.devices || []).map(d => [d.device, d.requests]));
    const tr = el('tr', undefined, c.id_sync && c.id_sync.length ? 'sync' : '');
    tr.append(el('th', `${c.company} · ${c.device_count}/${devices.length}`));
    for (const d of devices) { const n = seen.get(d); const td = el('td', n ? '■' : '·', n ? 'seen' : 'unseen'); td.setAttribute('title', n ? `${d}: ${n} lookups` : `${d}: none`); tr.append(td); }
    tbody.append(tr);
  }
  $('tracker-map-grid').replaceChildren(...(companies.length ? [thead, tbody] : []));
  const syncs = companies.flatMap(c => (c.id_sync || []).map(s => ({...s, company: c.company})));
  $('tracker-map-sync').replaceChildren(...syncs.map(s => {
    const li = el('li'); const n = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
    li.append(el('span', `[id sync] ${s.company} · ${s.domain} · ${n(s.devices.length, 'device')}, ${n(s.requests, 'lookup')}, ${s.blocked} blocked`));
    const b = el('button', s.rule_id ? 'rule in place' : `block ${s.domain}`, 'toggle'); b.disabled = Boolean(s.rule_id);
    b.onclick = () => blockSync(s.domain, b); li.append(b); return li;
  }));
}
async function blockSync(domain, button) {
  button.disabled = true; button.setAttribute('aria-busy', 'true');
  try {
    const r = await control('/api/tracker-map/block', {domain}); const data = await r.json(); if (!r.ok) throw Error(data.message || data.detail);
    notice(data.existing ? `A rule already blocks ${data.pattern}.` : data.applied ? `Blocked ${data.pattern} on every device. DNS confirmed.` : `Saved a block on ${data.pattern}, but DNS hasn't confirmed it yet.`);
    loadTrackerMap();
  } catch (e) { button.disabled = false; notice(`Couldn't block ${domain}${e.message ? `: ${e.message}` : ''}`); }
  finally { button.removeAttribute('aria-busy'); }
}
async function loadTrackerMap() {
  try { const r = await fetch('/api/tracker-map'); const data = await r.json(); if (!r.ok) throw Error(data.message || data.detail); renderTrackerMap(data); }
  catch (e) { $('tracker-map-text').textContent = `unavailable${e.message ? `: ${e.message}` : ''}`; $('tracker-map-grid').replaceChildren(); $('tracker-map-sync').replaceChildren(); }
}
$('tracker-map-open').onclick = () => showTrackerMap($('tracker-map').hidden);
$('tracker-map-close').onclick = () => { showTrackerMap(false); $('tracker-map-open').focus(); };
window.eddie = { pushEvent: addEvent, wake, voiceEvent, renderReport, renderSuggestions, renderTrackerMap, visualFor: acknowledgedRole, ask: text => control('/api/voice/ask', {text}) };
$('report-close').onclick = () => { $('report').hidden = true; };
$('open-logs').onclick = () => toggleLogs($('assistant-logs').hidden);
$('close-logs').onclick = () => { toggleLogs(false); $('open-logs').focus(); };
dialog.addEventListener('cancel', event => { event.preventDefault(); closeByUser(); });
dialog.addEventListener('close', () => { clearTimeout(exitTimer); dialog.classList.remove('closing'); toggleLogs(false); });
$('close').onclick = closeByUser;
dialog.addEventListener('click', e => { if (e.target === dialog) { const r=dialog.getBoundingClientRect(); if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom) closeByUser(); } });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('devices-sheet').hidden && !dialog.open && e.target.tagName !== 'INPUT') { toggleDevices(false); return; } if (e.key === 'Escape' && !$('rules-sheet').hidden && !dialog.open) { toggleRules(false); return; } if (e.key === 'Escape' && !$('capabilities').hidden && !dialog.open) { showCapabilities(false); return; } if (e.code === 'Space' && !['INPUT','TEXTAREA','BUTTON'].includes(document.activeElement.tagName)) { e.preventDefault(); wake(true); } });
// A paired device (the iPad) opens the link the Eddie computer prints; the token stays in the
// URL fragment, which is never sent in a request line and is not written to browser storage.
const pairToken = new URLSearchParams(String(location.hash || '').slice(1)).get('pair');
const control = (path, body, signal) => fetch(path, {method:'POST', signal, headers: {'Content-Type':'application/json', ...(pairToken ? {Authorization:`Bearer ${pairToken}`} : {})}, body: JSON.stringify(body || {})});
$('mic').onclick = async () => { try { const r = await control('/api/voice/start'); const data=await r.json(); if (!r.ok) throw Error(data.message || 'Could not start microphone.'); notice(data.message); } catch(e) { notice(e.message); } };
const stream = new EventSource('/events'); stream.onopen = () => { $('feed-status').textContent='Connected to local event bridge'; }; stream.onerror=()=>{ $('feed-status').textContent='Reconnecting to local event bridge…'; if(dialog.open) dismiss(); }; stream.onmessage=e=>{ try { const event=JSON.parse(e.data); if(event.type==='traffic_status'){ $('connection').firstChild.textContent=event.connected?'Connected':'Waiting'; $('source-label').textContent=event.connected?'LIVE TRAFFIC':'WAITING FOR TRAFFIC'; } else consumeEvent(event); } catch {} };
// The traffic API runs on the same machine as this page (localhost on the Mac, its LAN IP from the iPad).
const trafficUrl = new URLSearchParams(location.search).get('traffic') || `ws://${location.hostname || 'localhost'}:8000/ws/events`;
function connectTraffic() { let ws; try { ws=new WebSocket(trafficUrl); } catch { visualConnected=false; renderEddieVisual(); return; } ws.onopen=()=>{ visualConnected=true; renderEddieVisual(); $('connection').firstChild.textContent='Connected'; $('source-label').textContent='LIVE TRAFFIC'; }; ws.onmessage=e=>{ try { visualConnected=true; const message=JSON.parse(e.data); if(message.type==='snapshot'){ total=0; blocked=0; allowedCount=0; brokerCount=0; deviceSet.clear(); acrSeen.clear(); renderAcr(); $('feed').replaceChildren(); } let events=Array.isArray(message)?message:Array.isArray(message.events)?message.events:[message]; if(message.type==='snapshot'){ events=[...events, ...(message.ai_verdicts || []), ...(message.bypass || []), ...(message.geo || []), ...(message.acr || [])].sort((a,b)=>(dateOf(a).getTime()||0)-(dateOf(b).getTime()||0)); } events.forEach(consumeEvent); if(message.type==='snapshot'){ showQuestions(message.questions); loadRules(); refreshPanels(); } const totals=message.totals || (message.type==='totals'?message:null); if(totals){ total=totals.seen; blocked=totals.blocked; $('requests').textContent=total.toLocaleString(); $('blocked').textContent=blocked.toLocaleString(); updateCounts(); } } catch {} }; ws.onclose=()=>{ visualConnected=false; renderEddieVisual(); $('connection').firstChild.textContent='Waiting'; $('source-label').textContent='WAITING FOR TRAFFIC'; setTimeout(connectTraffic,2500); }; ws.onerror=()=>ws.close(); }
$('home-mode').onclick = () => setMode('home_mode', !modes.home_mode);
$('offline-mode').onclick = () => setMode('offline', !modes.offline);
$('quarantine-mode').onclick = () => setMode('quarantine', !modes.quarantine);
$('kid-mode').onclick = toggleKidMode;
$('open-devices').onclick = () => toggleDevices($('devices-sheet').hidden);
$('open-rules').onclick = () => toggleRules($('rules-sheet').hidden);
$('close-rules').onclick = () => { toggleRules(false); $('open-rules').focus(); };
$('close-devices').onclick = () => { toggleDevices(false); $('open-devices').focus(); };
connectTraffic();
loadRules();
loadGuest();
refreshPanels();
