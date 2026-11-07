const $ = id => document.getElementById(id);
let total = 0, blocked = 0, deviceSet = new Set(), voiceReady = false, speaking = false, closeTimer;
let is_eddie_active = false, exitTimer, confirming = false, confirmTimer, userClosed = false;
const dialog = $('assistant');
function updateCounts() { $('allowed').textContent = allowedCount.toLocaleString(); $('blocked-count').textContent = blocked.toLocaleString(); }
let allowedCount = 0;
function notice(message) { $('notice').textContent = message; $('notice').hidden = false; clearTimeout(notice.timer); notice.timer = setTimeout(() => $('notice').hidden = true, 7000); }
function addEvent(event) {
  if (!event || typeof event !== 'object' || !event.domain) return;
  total++; const action = String(event.action || event.decision || 'allow').toLowerCase(); if (action === 'block' || action === 'blocked') blocked++; if(['allow','allowed'].includes(action)) allowedCount++; updateCounts();
  const device = typeof event.device === 'object' ? event.device.name || event.device.ip || 'Unknown device' : event.device || event.device_name || event.client_ip || 'Unknown device';
  deviceSet.add(String(device)); $('requests').textContent = total.toLocaleString(); $('blocked').textContent = blocked.toLocaleString(); $('devices').textContent = deviceSet.size;
  renderEvent(event, device, action);
}
// A feed line: a coloured status tag ([approved], [blocked], [flagged], [ai]) then the text.
const TAGS = { allow: 'approved', block: 'blocked', flag: 'flagged', ai: 'ai', rule: 'rule' };
function taggedLine(kind, text) {
  const line = document.createElement('span'), tag = document.createElement('span'), body = document.createElement('span');
  tag.className = 'tag tag-' + kind; tag.textContent = `[${TAGS[kind]}]`; body.textContent = ' ' + text;
  line.append(tag, body); return line;
}
function renderEvent(event, device, action) {
  if (!device) device = typeof event.device === 'object' ? event.device.name || event.device.ip : event.device || event.device_name || event.client_ip || 'Unknown device';
  action ||= String(event.action || event.decision || 'allow').toLowerCase();
  $('feed').querySelector('.empty')?.remove(); const row = document.createElement('div'); row.className = 'event';
  const time = document.createElement('time'); const date = (event.timestamp || event.ts) ? new Date(event.ts || (typeof event.timestamp === 'number' && event.timestamp < 1e12 ? event.timestamp * 1000 : event.timestamp)) : new Date(); time.textContent = Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString([], {hour12:false});
  const kind = ['block','blocked'].includes(action) ? 'block' : action === 'flag' ? 'flag' : 'allow';
  const line = taggedLine(kind, `${device} → ${event.domain}${event.reason ? ' — ' + event.reason : ''}`);
  row.append(time, line); $('feed').append(row);
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
function feedNote(kind, text) {
  const row = document.createElement('div'); row.className = 'event'; row.append(taggedLine(kind, text));
  $('feed').querySelector('.empty')?.remove(); $('feed').append(row);
  while ($('feed').children.length > 150) $('feed').firstChild.remove();
  if (!$('assistant-logs').hidden) $('voice-logs').append(row.cloneNode(true));
  $('feed').scrollTop = $('feed').scrollHeight;
}
const ruleLine = rule => `${rule.action} ${rule.pattern} · ${rule.device || 'all devices'} · by ${rule.created_by}`;
function renderRules(list) {
  $('rules-count').textContent = list.length;
  const items = list.slice(0, 5).map(rule => { const li = document.createElement('li'); li.textContent = ruleLine(rule); return li; });
  if (list.length > 5) { const more = document.createElement('li'); more.textContent = `+${list.length - 5} more`; items.push(more); }
  $('rules-list').replaceChildren(...items);
}
async function loadRules() {
  try { const r = await fetch('/api/rules'); if (!r.ok) throw Error(); renderRules(await r.json()); }
  catch { $('rules-count').textContent = 'unavailable'; $('rules-list').replaceChildren(); }
}
// Device panel (#35): each device's name, distinct domains blocked and the latest of them, from eddie.db.
function renderDevices(list) {
  $('device-count').textContent = list.length;
  const items = list.slice(0, 5).map(d => {
    const li = document.createElement('li'); li.textContent = `${d.name} · ${d.trackers} tracker${d.trackers === 1 ? '' : 's'} blocked`;
    const recent = (d.recent_blocks || []).map(b => b.domain);
    if (recent.length) { const small = document.createElement('small'); small.textContent = `recent: ${recent.join(', ')}`; li.append(small); }
    return li;
  });
  if (list.length > 5) { const more = document.createElement('li'); more.textContent = `+${list.length - 5} more`; items.push(more); }
  $('device-list').replaceChildren(...items);
}
async function loadDevices() {
  try { const r = await fetch('/api/devices'); if (!r.ok) throw Error(); renderDevices((await r.json()).devices || []); }
  catch { $('device-count').textContent = 'unavailable'; $('device-list').replaceChildren(); }
}
// Mode toggles show only state the backend acknowledged: a click shows "turning on…" until the API
// answers, and offline mode says when DNS hasn't loaded the change. Home mode is a backend flag only.
const modes = {home_mode: null, offline: null, offline_applied: false}, modePending = {};
function applyModes(state) {
  if (typeof state.home_mode === 'boolean') modes.home_mode = state.home_mode;
  if (typeof state.offline === 'boolean') { modes.offline = state.offline; modes.offline_applied = state.offline_applied === true; }
  renderModes();
}
function renderModes() {
  for (const [id, label, key] of [['home-mode', 'home mode', 'home_mode'], ['offline-mode', 'offline mode', 'offline']]) {
    const button = $(id), pending = modePending[key], on = modes[key];
    const text = pending !== undefined ? `${label}: turning ${pending ? 'on' : 'off'}…` : `${label}: ${on === null ? 'unavailable' : on ? 'on' : 'off'}`;
    button.disabled = pending !== undefined || on === null;
    button.setAttribute('aria-busy', String(pending !== undefined)); button.setAttribute('aria-pressed', String(on === true));
    // Home mode is the house icon beside the heading, so its state is in the label, not the text.
    if (key === 'home_mode') { button.setAttribute('aria-label', text); button.setAttribute('title', text); } else button.textContent = text;
  }
  renderMic();
  const waiting = modes.offline !== null && modePending.offline === undefined && !modes.offline_applied;
  $('offline-note').textContent = waiting ? `offline mode ${modes.offline ? 'on' : 'off'} not yet applied by DNS` : '';
  $('home-note').textContent = modes.home_mode ? 'home mode is a backend flag; DNS does not enforce it yet' : '';
}
// One mic button: "start mic" or "stop mic", prefixed "offline •" while offline mode is confirmed on.
function renderMic() { $('mic').textContent = `${modes.offline ? 'offline • ' : ''}${voiceReady ? 'stop mic' : 'start mic'}`; }
async function loadModes() {
  try { const r = await fetch('/api/mode'); if (!r.ok) throw Error(); applyModes(await r.json()); }
  catch { modes.home_mode = modes.offline = null; renderModes(); }
}
async function setMode(key, on) {
  if (modePending[key] !== undefined) return;
  modePending[key] = on; renderModes();
  const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 10000);
  try {
    const r = await control(key === 'home_mode' ? '/api/mode' : '/api/offline-mode', {[key]: on}, abort.signal);
    const data = await r.json(); if (!r.ok) throw Error(data.message);
    applyModes({...data, offline_applied: data.applied});
  } catch (e) {
    notice(`${key === 'home_mode' ? 'Home' : 'Offline'} mode change not confirmed: ${e.name === 'AbortError' || !e.message ? 'the traffic API did not answer.' : e.message}`);
    loadModes();
  } finally { clearTimeout(timer); delete modePending[key]; renderModes(); }
}
// Counts move with traffic, so the panels refresh every 30 s as well as on (re)connect.
function refreshPanels() { clearTimeout(refreshPanels.timer); refreshPanels.timer = setTimeout(refreshPanels, 30000); loadDevices(); loadModes(); }
function consumeEvent(event) {
  if (event.type === 'ai_verdict') return feedNote('ai', `${event.domain} → ${event.label} / ${event.reason}`);
  if (event.type === 'mode') return applyModes({home_mode: event.home_mode});
  if (event.type === 'offline_mode') return applyModes({offline: event.offline, offline_applied: event.applied});
  if (event.type === 'rule_change') {
    // Stored, not enforced: the event is sent before DNS acknowledges it, so never say "blocked".
    const text = `Rule ${event.change === 'removed' ? 'removed' : 'added'}: ${ruleLine(event)}`;
    feedNote('rule', text); notice(text); loadRules();
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
function wake(preview = false) { userClosed = false; clearTimeout(exitTimer); dialog.classList.remove('closing'); is_eddie_active = true; clearTimeout(closeTimer); if (preview || !voiceReady) closeTimer = setTimeout(dismiss, 4000); if (!dialog.open) dialog.showModal(); $('assistant-title').textContent = "Hey, what's up?"; $('assistant-description').textContent = 'Ask me what your devices have been up to.'; setAssistantState('listening'); }
// Closing Eddie returns to the live feed, scrolled to the newest activity.
function dismiss() {
  is_eddie_active = false;
  setConfirming(false);
  clearTimeout(closeTimer);
  if (!dialog.open || dialog.classList.contains('closing')) return;
  dialog.classList.add('closing');
  const duration = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 320;
  exitTimer = setTimeout(() => {
    dialog.close();
    dialog.classList.remove('closing');
    toggleLogs(false);
    $('feed').scrollTop = $('feed').scrollHeight;
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
  if (event.type === 'speech_end') { setAssistantState('ready'); speaking = false; }
}
// "What they know" report: real facts, or the seeded demo week under a demo banner.
function renderReport(report) {
  const demo = report.demo === true;
  $('report-title').textContent = demo ? `what they know · DEMO WEEK` : `what they know · last ${report.hours}h${report.source === 'eddie.db' ? ' · from eddie.db' : ''}`;
  $('report-demo').textContent = demo ? `DEMO DATA · ${report.label}` : ''; $('report-demo').hidden = !demo;
  const items = (report.facts || []).map(f => { const li = document.createElement('li'); li.textContent = `${f.fact} — ${f.evidence} (confidence ${Math.round(f.confidence * 100)}%)`; return li; });
  if (!items.length) { const li = document.createElement('li'); li.textContent = 'Not enough traffic yet to say what anyone could guess.'; items.push(li); }
  $('report-facts').replaceChildren(...items); $('report').hidden = false;
}
window.eddie = { pushEvent: addEvent, wake, voiceEvent, renderReport, ask: text => control('/api/voice/ask', {text}) };
$('report-close').onclick = () => { $('report').hidden = true; };
$('open-logs').onclick = () => toggleLogs($('assistant-logs').hidden);
$('close-logs').onclick = () => { toggleLogs(false); $('open-logs').focus(); };
dialog.addEventListener('cancel', event => { event.preventDefault(); closeByUser(); });
dialog.addEventListener('close', () => { clearTimeout(exitTimer); dialog.classList.remove('closing'); toggleLogs(false); });
$('close').onclick = closeByUser;
dialog.addEventListener('click', e => { if (e.target === dialog) { const r=dialog.getBoundingClientRect(); if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom) closeByUser(); } });
document.addEventListener('keydown', e => { if (e.code === 'Space' && !['INPUT','TEXTAREA','BUTTON'].includes(document.activeElement.tagName)) { e.preventDefault(); wake(true); } });
// A paired device (the iPad) opens the link the Eddie computer prints; the token stays in the
// URL fragment, which is never sent in a request line and is not written to browser storage.
const pairToken = new URLSearchParams(String(location.hash || '').slice(1)).get('pair');
const control = (path, body, signal) => fetch(path, {method:'POST', signal, headers: {'Content-Type':'application/json', ...(pairToken ? {Authorization:`Bearer ${pairToken}`} : {})}, body: JSON.stringify(body || {})});
$('mic').onclick = async () => { try { const r = await control(voiceReady ? '/api/voice/stop' : '/api/voice/start'); const data=await r.json(); if (!r.ok) throw Error(data.message || 'Could not start microphone.'); notice(data.message); } catch(e) { notice(e.message); } };
const stream = new EventSource('/events'); stream.onopen = () => { $('feed-status').textContent='Connected to local event bridge'; }; stream.onerror=()=>{ $('feed-status').textContent='Reconnecting to local event bridge…'; if(dialog.open) dismiss(); }; stream.onmessage=e=>{ try { const event=JSON.parse(e.data); if(event.type==='traffic_status'){ $('connection').firstChild.textContent=event.connected?'Connected':'Waiting'; $('source-label').textContent=event.connected?'LIVE TRAFFIC':'WAITING FOR TRAFFIC'; } else consumeEvent(event); } catch {} };
// The traffic API runs on the same machine as this page (localhost on the Mac, its LAN IP from the iPad).
const trafficUrl = new URLSearchParams(location.search).get('traffic') || `ws://${location.hostname || 'localhost'}:8000/ws/events`;
function connectTraffic() { let ws; try { ws=new WebSocket(trafficUrl); } catch { return; } ws.onopen=()=>{ $('connection').firstChild.textContent='Connected'; $('source-label').textContent='LIVE TRAFFIC'; }; ws.onmessage=e=>{ try { const message=JSON.parse(e.data); if(message.type==='snapshot'){ total=0; blocked=0; allowedCount=0; deviceSet.clear(); $('feed').replaceChildren(); } const events=Array.isArray(message)?message:Array.isArray(message.events)?message.events:[message]; events.forEach(consumeEvent); if(message.type==='snapshot'){ (message.ai_verdicts || []).forEach(consumeEvent); loadRules(); refreshPanels(); } const totals=message.totals || (message.type==='totals'?message:null); if(totals){ total=totals.seen; blocked=totals.blocked; $('requests').textContent=total.toLocaleString(); $('blocked').textContent=blocked.toLocaleString(); updateCounts(); } } catch {} }; ws.onclose=()=>{ $('connection').firstChild.textContent='Waiting'; $('source-label').textContent='WAITING FOR TRAFFIC'; setTimeout(connectTraffic,2500); }; ws.onerror=()=>ws.close(); }
$('home-mode').onclick = () => setMode('home_mode', !modes.home_mode);
$('offline-mode').onclick = () => setMode('offline', !modes.offline);
connectTraffic();
loadRules();
refreshPanels();
