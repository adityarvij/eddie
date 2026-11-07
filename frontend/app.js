const $ = id => document.getElementById(id);
let paused = false, total = 0, blocked = 0, deviceSet = new Set(), queued = [], voiceReady = false, speaking = false, closeTimer;
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
  while ($('feed').children.length > 150) $('feed').firstChild.remove();
  $('feed').scrollTop = $('feed').scrollHeight;

}
function wake() { clearTimeout(closeTimer); if (!dialog.open) dialog.showModal(); $('assistant-title').textContent = "Hey, what's up?"; $('assistant-description').textContent = 'Ask me what your devices have been up to.'; $('assistant-state').textContent = 'EDDIE IS LISTENING'; $('question').focus(); }
function dismiss() { dialog.close(); }
function voiceEvent(event) {
  if (event.type === 'voice_state') { if (event.state === 'listening') wake(); $('character').classList.toggle('talking', event.state === 'speaking'); $('assistant-state').textContent = 'EDDIE IS ' + event.state.toUpperCase(); }
  if (event.type === 'agent_reply') { if (!dialog.open) wake(); $('assistant-description').textContent = event.text; }
  if (event.type === 'voice_status') { voiceReady = event.status === 'ready'; $('mic').textContent = voiceReady ? '“Hey Eddie” is listening ●' : 'Enable “Hey Eddie” ○'; if (event.message) notice(event.message); }
  if (event.type === 'wake') wake();
  if (event.type === 'transcript' && event.text) { if (!dialog.open) wake(); $('question').value = event.text; }
  if (event.type === 'speaking') { if (!dialog.open) wake(); $('assistant-title').textContent = event.title || 'Here’s what I’m seeing.'; $('assistant-description').textContent = event.text || ''; $('assistant-state').textContent = 'EDDIE IS TALKING'; $('character').classList.add('talking'); speaking = true; }
  if (event.type === 'speech_end') { $('character').classList.remove('talking'); $('assistant-state').textContent = 'EDDIE IS LISTENING'; speaking = false; }
}
window.eddie = { pushEvent: addEvent, wake, voiceEvent };
$('wake').onclick = wake; $('close').onclick = dismiss;
dialog.addEventListener('click', e => { if (e.target === dialog) { const r=dialog.getBoundingClientRect(); if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom) dismiss(); } });
document.addEventListener('keydown', e => { if (e.code === 'Space' && !['INPUT','TEXTAREA','BUTTON'].includes(document.activeElement.tagName)) { e.preventDefault(); wake(); } });
$('pause').onclick = () => { paused = !paused; $('pause').textContent = paused ? 'resume' : 'pause'; if (!paused) { queued.forEach(e=>renderEvent(e)); queued=[]; } };
$('mic').onclick = async () => { try { const r = await fetch('/api/voice/start',{method:'POST'}); const data=await r.json(); if (!r.ok) throw Error(data.message || 'Could not start microphone.'); notice(data.message); } catch(e) { notice(e.message); } };
$('ask-form').onsubmit = async e => { e.preventDefault(); const text=$('question').value.trim(); if (!text) return; $('question').value=''; try { const r = await fetch('/api/voice/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})}); if(!r.ok) throw Error('Voice service unavailable.'); } catch(e) { notice(e.message); } };
const stream = new EventSource('/events'); stream.onopen = () => { $('feed-status').textContent='Connected to local event bridge'; }; stream.onerror=()=>{ $('feed-status').textContent='Reconnecting to local event bridge…'; }; stream.onmessage=e=>{ try { const event=JSON.parse(e.data); if(event.type==='traffic_status'){ $('connection').firstChild.textContent=event.connected?'Connected':'Waiting'; $('source-label').textContent=event.connected?'LIVE TRAFFIC':'WAITING FOR TRAFFIC'; } else if(event.domain) addEvent(event); else voiceEvent(event); } catch {} };
// Claude's fake traffic can stream JSON from ws://localhost:8000/ws/events.
const trafficUrl = new URLSearchParams(location.search).get('traffic') || 'ws://localhost:8000/ws/events';
function connectTraffic() { let ws; try { ws=new WebSocket(trafficUrl); } catch { return; } ws.onopen=()=>{ $('connection').firstChild.textContent='Connected'; $('source-label').textContent='LIVE TRAFFIC'; }; ws.onmessage=e=>{ try { const message=JSON.parse(e.data); if(message.type==='snapshot'){ total=0; blocked=0; allowedCount=0; deviceSet.clear(); $('feed').replaceChildren(); } const events=Array.isArray(message)?message:Array.isArray(message.events)?message.events:[message]; events.forEach(event=>event.domain?addEvent(event):voiceEvent(event)); const totals=message.totals || (message.type==='totals'?message:null); if(totals){ total=totals.seen; blocked=totals.blocked; $('requests').textContent=total.toLocaleString(); $('blocked').textContent=blocked.toLocaleString(); updateCounts(); } } catch {} }; ws.onclose=()=>{ $('connection').firstChild.textContent='Waiting'; $('source-label').textContent='WAITING FOR TRAFFIC'; setTimeout(connectTraffic,2500); }; ws.onerror=()=>ws.close(); }
connectTraffic();
