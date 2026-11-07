const $ = id => document.getElementById(id);
let paused = false, total = 0, blocked = 0, deviceSet = new Set(), queued = [], voiceReady = false, speaking = false, closeTimer;
let is_eddie_active = false, exitTimer, confirming = false, confirmTimer;
const dialog = $('assistant');
function updateCounts() { $('allowed').textContent = allowedCount.toLocaleString(); $('blocked-count').textContent = blocked.toLocaleString(); }
let allowedCount = 0;
function notice(message) { $('notice').textContent = message; $('notice').hidden = false; clearTimeout(notice.timer); notice.timer = setTimeout(() => $('notice').hidden = true, 7000); }
function addEvent(event) {
  if (!event || typeof event !== 'object' || !event.domain) return;
  total++; const action = String(event.action || event.decision || 'allow').toLowerCase(); if (action === 'block' || action === 'blocked') blocked++; if(['allow','allowed'].includes(action)) allowedCount++; updateCounts();
  const device = typeof event.device === 'object' ? event.device.name || event.device.ip || 'Unknown device' : event.device || event.device_name || event.client_ip || 'Unknown device';
  deviceSet.add(String(device)); $('requests').textContent = total.toLocaleString(); $('blocked').textContent = blocked.toLocaleString(); $('devices').textContent = deviceSet.size;
  if (paused) { queued.push(event); if (queued.length > 100) queued.shift(); return; }
  renderEvent(event, device, action);
}
function renderEvent(event, device, action) {
  if (!device) device = typeof event.device === 'object' ? event.device.name || event.device.ip : event.device || event.device_name || event.client_ip || 'Unknown device';
  action ||= String(event.action || event.decision || 'allow').toLowerCase();
  $('feed').querySelector('.empty')?.remove(); const row = document.createElement('div'); row.className = 'event';
  const time = document.createElement('time'); const date = (event.timestamp || event.ts) ? new Date(event.ts || (typeof event.timestamp === 'number' && event.timestamp < 1e12 ? event.timestamp * 1000 : event.timestamp)) : new Date(); time.textContent = Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString([], {hour12:false});
  const line = document.createElement('span');
  const verdict = ['block','blocked'].includes(action) ? 'blocked' : action === 'flag' ? 'flagged' : 'allowed';
  line.textContent = `${device} → ${event.domain} — ${verdict}${event.reason ? ' / ' + event.reason : ''}`;
  row.append(time, line); $('feed').append(row);
  if (!$('assistant-logs').hidden) { $('voice-logs').append(row.cloneNode(true)); while ($('voice-logs').children.length > 150) $('voice-logs').firstChild.remove(); $('voice-logs').scrollTop = $('voice-logs').scrollHeight; }
  while ($('feed').children.length > 150) $('feed').firstChild.remove();
  $('feed').scrollTop = $('feed').scrollHeight;

}
function setConfirming(on, ms = 0) { confirming = on; clearTimeout(confirmTimer); if (on) confirmTimer = setTimeout(() => { confirming = false; setAssistantState('listening'); }, ms); }
function consumeEvent(event) {
  if (event.type === 'ai_verdict') {
    const row = document.createElement('div'); row.className = 'event';
    row.textContent = `[ai] ${event.domain} → ${event.label} / ${event.reason}`;
    $('feed').querySelector('.empty')?.remove(); $('feed').append(row);
    while ($('feed').children.length > 150) $('feed').firstChild.remove();
    if (!$('assistant-logs').hidden) $('voice-logs').append(row.cloneNode(true));
    $('feed').scrollTop = $('feed').scrollHeight;
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
function wake(preview = false) { clearTimeout(exitTimer); dialog.classList.remove('closing'); is_eddie_active = true; clearTimeout(closeTimer); if (preview || !voiceReady) closeTimer = setTimeout(dismiss, 4000); if (!dialog.open) dialog.showModal(); $('assistant-title').textContent = "Hey, what's up?"; $('assistant-description').textContent = 'Ask me what your devices have been up to.'; setAssistantState('listening'); }
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
function toggleLogs(open) {
  $('assistant-logs').hidden = !open;
  $('open-logs').setAttribute('aria-expanded', String(open));
  $('open-logs').textContent = open ? 'Close logs ×' : 'Open logs ↗';
  if (open) { $('voice-logs').replaceChildren(...Array.from($('feed').children, row => row.cloneNode(true))); $('voice-logs').scrollTop = $('voice-logs').scrollHeight; $('close-logs').focus(); }
}
function voiceEvent(event) {
  if (event.type === 'voice_state') { if (typeof event.is_eddie_active === 'boolean') is_eddie_active = event.is_eddie_active; if (event.state === 'confirming') setConfirming(true, Number(event.expires_in_ms) || 15000); else if (['thinking','dismissed'].includes(event.state)) setConfirming(false); if (event.state === 'dismissed') return dismiss(); if (['thinking','speaking','listening'].includes(event.state)) clearTimeout(closeTimer); if (event.state === 'listening' && (!dialog.open || dialog.classList.contains('closing'))) wake(); setAssistantState(event.state); }
  if (event.type === 'agent_reply') { if (!dialog.open) wake(); $('assistant-description').textContent = event.text; }
  if (event.type === 'voice_status') { if (['off','error'].includes(event.status)) { $('character').classList.remove('talking'); if(dialog.open) dismiss(); } voiceReady = event.status === 'ready'; $('mic').textContent = voiceReady ? (event.engine === 'vosk' ? 'Stop mic · offline prototype ●' : 'Stop microphone ●') : 'Enable “Hey Eddie” ○'; if (event.message && !voiceReady) notice(event.message); else if (voiceReady) $('notice').hidden = true; }
  if (event.type === 'wake') wake();
  if (event.type === 'transcript' && event.text) { if (!dialog.open) wake(); $('assistant-title').textContent = event.text; }
  if (event.type === 'speaking') { if (!dialog.open) wake(); $('assistant-title').textContent = event.title || 'Here’s what I’m seeing.'; $('assistant-description').textContent = event.text || ''; setAssistantState('speaking'); speaking = true; }
  if (event.type === 'speech_end') { setAssistantState('ready'); speaking = false; }
}
window.eddie = { pushEvent: addEvent, wake, voiceEvent };
$('open-logs').onclick = () => toggleLogs($('assistant-logs').hidden);
$('close-logs').onclick = () => { toggleLogs(false); $('open-logs').focus(); };
dialog.addEventListener('cancel', event => { event.preventDefault(); dismiss(); });
dialog.addEventListener('close', () => { clearTimeout(exitTimer); dialog.classList.remove('closing'); toggleLogs(false); });
$('wake').onclick = () => wake(true); $('close').onclick = dismiss;
dialog.addEventListener('click', e => { if (e.target === dialog) { const r=dialog.getBoundingClientRect(); if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom) dismiss(); } });
document.addEventListener('keydown', e => { if (e.code === 'Space' && !['INPUT','TEXTAREA','BUTTON'].includes(document.activeElement.tagName)) { e.preventDefault(); wake(true); } });
$('pause').onclick = () => { paused = !paused; $('pause').textContent = paused ? 'resume' : 'pause'; if (!paused) { queued.forEach(e=>renderEvent(e)); queued=[]; } };
$('mic').onclick = async () => { try { const r = await fetch(voiceReady ? '/api/voice/stop' : '/api/voice/start',{method:'POST'}); const data=await r.json(); if (!r.ok) throw Error(data.message || 'Could not start microphone.'); notice(data.message); } catch(e) { notice(e.message); } };
const stream = new EventSource('/events'); stream.onopen = () => { $('feed-status').textContent='Connected to local event bridge'; }; stream.onerror=()=>{ $('feed-status').textContent='Reconnecting to local event bridge…'; if(dialog.open) dismiss(); }; stream.onmessage=e=>{ try { const event=JSON.parse(e.data); if(event.type==='traffic_status'){ $('connection').firstChild.textContent=event.connected?'Connected':'Waiting'; $('source-label').textContent=event.connected?'LIVE TRAFFIC':'WAITING FOR TRAFFIC'; } else consumeEvent(event); } catch {} };
// The traffic API runs on the same machine as this page (localhost on the Mac, its LAN IP from the iPad).
const trafficUrl = new URLSearchParams(location.search).get('traffic') || `ws://${location.hostname || 'localhost'}:8000/ws/events`;
function connectTraffic() { let ws; try { ws=new WebSocket(trafficUrl); } catch { return; } ws.onopen=()=>{ $('connection').firstChild.textContent='Connected'; $('source-label').textContent='LIVE TRAFFIC'; }; ws.onmessage=e=>{ try { const message=JSON.parse(e.data); if(message.type==='snapshot'){ total=0; blocked=0; allowedCount=0; deviceSet.clear(); $('feed').replaceChildren(); } const events=Array.isArray(message)?message:Array.isArray(message.events)?message.events:[message]; events.forEach(consumeEvent); const totals=message.totals || (message.type==='totals'?message:null); if(totals){ total=totals.seen; blocked=totals.blocked; $('requests').textContent=total.toLocaleString(); $('blocked').textContent=blocked.toLocaleString(); updateCounts(); } } catch {} }; ws.onclose=()=>{ $('connection').firstChild.textContent='Waiting'; $('source-label').textContent='WAITING FOR TRAFFIC'; setTimeout(connectTraffic,2500); }; ws.onerror=()=>ws.close(); }
connectTraffic();
