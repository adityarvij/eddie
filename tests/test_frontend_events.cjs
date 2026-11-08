const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
class Element {
  get textContent(){return this._text ?? this.children.map(node=>node.textContent).join('');}
  set textContent(value){this._text=value;}
  constructor(){this.children=[];this.hidden=true;this.dataset={};this.attrs={};this.firstChild={textContent:''};this.classList={add(){},remove(){},toggle(){}};}
  append(...nodes){for(const node of nodes){node.parent=this;this.children.push(node);}}
  prepend(...nodes){for(const node of nodes){node.parent=this;this.children.unshift(node);}}
  remove(){this.parent.children=this.parent.children.filter(node=>node!==this);}
  querySelector(){return null;} cloneNode(){return new Element();}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  addEventListener(){} setAttribute(k,v){this.attrs[k]=v;} focus(){}
}
const ids=new Map();
const get=id=>{if(!ids.has(id))ids.set(id,new Element());return ids.get(id);};
let socket;
const timers=[];
const context={document:{getElementById:get,createElement:()=>new Element(),addEventListener(){},activeElement:{tagName:'BODY'}},window:{},location:{search:'',hostname:'localhost'},URLSearchParams,Date,console,
  setTimeout(fn,ms){timers.push({fn,ms});return timers.length;},clearTimeout(){},fetch(){},AbortController,EventSource:class{},WebSocket:class{constructor(){socket=this;}}};
const source=fs.readFileSync('frontend/app.js','utf8');
for (const a of ['block','allow','ignore']) { const b = new Element(); b.dataset.answer = a; get('question-actions').append(b); }
vm.runInNewContext(source,context);
socket.onmessage({data:JSON.stringify({domain:'example.org',device:'TV',action:'allow',ts:Date.now()})});
assert.equal(get('requests').textContent,'1');
const tag=row=>row.children.at(-1).children[0];  // a row ends with the line: [tag, text]
const verdictTs=new Date(2026,0,1,9,5,7).getTime();
socket.onmessage({data:JSON.stringify({type:'ai_verdict',domain:'example.org',label:'needed',confidence:.9,reason:'App content',ts:verdictTs})});
assert.equal(get('requests').textContent,'1','AI events must not be counted as DNS requests');
assert.equal(get('allowed').textContent,'1');
assert.match(get('feed').children.at(-1).textContent,/\[approved \(ai\)\] example\.org — App content$/);
assert.equal(tag(get('feed').children.at(-1)).className,'tag tag-allow','an AI line is coloured like its result');
for (const [label, shown] of [['unsure','unsure (ai)'],['tracker','flagged (ai)']]) {
  socket.onmessage({data:JSON.stringify({type:'ai_verdict',domain:'t.example',label,confidence:.6,reason:'Why',ts:verdictTs})});
  assert.equal(tag(get('feed').children.at(-1)).textContent,`[${shown}]`,'the AI never blocks, so a tracker reads flagged');
  assert.equal(tag(get('feed').children.at(-1)).className,'tag tag-flag');
}
assert.equal(get('feed').children.at(-1).children[0].textContent,new Date(verdictTs).toLocaleTimeString([],{hour12:false}),'an AI line starts with the verdict time');
socket.onmessage({data:JSON.stringify({domain:'ads.example',device:'TV',action:'block',reason:'On a list',ts:Date.now()})});
assert.match(get('feed').children.at(-1).textContent,/\[blocked\] TV → ads\.example — On a list$/);
assert.equal(tag(get('feed').children.at(-1)).className,'tag tag-block');
socket.onmessage({data:JSON.stringify({domain:'t.example',device:'TV',action:'spoof',reason:'global rule t.example',ts:Date.now()})});
assert.match(get('feed').children.at(-1).textContent,/\[spoofed\] TV → t\.example — global rule t\.example$/);
assert.equal(tag(get('feed').children.at(-1)).className,'tag tag-spoof');
socket.onmessage({data:JSON.stringify({domain:'new.example',device:'TV',action:'flag',ts:Date.now()})});
assert.match(get('feed').children.at(-1).textContent,/\[flagged\] TV → new\.example$/);
assert.match(get('feed').children[0].textContent,/\[approved\] TV → example\.org$/);

// Rule changes: a [rule] feed row and a toast that say stored, never enforced.
socket.onmessage({data:JSON.stringify({type:'rule_change',change:'added',id:7,pattern:'device-metrics-us.amazon.com',action:'block',device_mac:'f0:4f:7c:11:22:33',device:'Fire Stick',created_by:'voice'})});
assert.equal(tag(get('feed').children.at(-1)).parent.textContent,'[rule] Rule added: block device-metrics-us.amazon.com · Fire Stick · by voice');
assert.match(get('feed').children.at(-1).children[0].textContent,/^\d{2}:\d{2}:\d{2}$/,'a rule line starts with its time');
assert.equal(tag(get('feed').children.at(-1)).className,'tag tag-rule');
assert.equal(get('notice').textContent,'Rule added: block device-metrics-us.amazon.com · Fire Stick · by voice');
socket.onmessage({data:JSON.stringify({type:'rule_change',change:'removed',id:8,pattern:'a.example',action:'allow',device_mac:null,device:null,created_by:'api'})});
assert.equal(get('notice').textContent,'Rule removed: allow a.example · all devices · by api');
assert.equal(get('requests').textContent,'4','rule changes are not DNS requests');

// Bypass suspicions: a [bypass?] row and a toast with the API's reason; not a DNS request.
const bypassReason='TV looked up dns.google (Google Public DNS), an encrypted DNS resolver. It may be sending some lookups there.';
socket.onmessage({data:JSON.stringify({type:'bypass_suspected',ts:Date.now(),device_mac:'f0:4f:7c:11:22:33',device:'TV',domain:'dns.google',resolver:'Google Public DNS',blocked:false,reason:bypassReason})});
assert.equal(tag(get('feed').children.at(-1)).parent.textContent,`[bypass?] ${bypassReason}`);
assert.equal(tag(get('feed').children.at(-1)).className,'tag tag-bypass');
assert.equal(get('notice').textContent,bypassReason);
assert.equal(get('requests').textContent,'4','bypass suspicions are not DNS requests');

// Geo alerts (#102): a [new country?] row and a toast with the caveat the API sent; not a DNS request.
const geoReason='Doorbell started talking to a server in Russia (example.com, 5.6.7.8). Until now its servers were in the United States. IP location is approximate.';
socket.onmessage({data:JSON.stringify({type:'geo_alert',ts:Date.now(),device_mac:'f0:4f:7c:11:22:34',device:'Doorbell',domain:'example.com',ip:'5.6.7.8',country:'RU',country_name:'Russia',previous:['US'],approximate:true,reason:geoReason})});
assert.equal(tag(get('feed').children.at(-1)).parent.textContent,`[new country?] ${geoReason}`);
assert.equal(tag(get('feed').children.at(-1)).className,'tag tag-geo');
assert.equal(get('notice').textContent,geoReason);
assert.equal(get('requests').textContent,'4','geo alerts are not DNS requests');

// ACR (#104): an [acr blocked] row, a toast and the card, all with the API's reason; not a DNS request.
const acrReason="TV was checking in with Samsung's content recognition (ACR) server log-ingestion.samsungacr.com every 15 seconds. ACR is how TVs report what's on screen. Blocked.";
assert.equal(get('acr').hidden,true,'no card before any detection');
socket.onmessage({data:JSON.stringify({type:'acr_detected',ts:Date.now(),device_mac:'f0:4f:7c:11:22:33',device:'TV',domain:'log-ingestion.samsungacr.com',acr_name:'samsungacr.com',provider:'Samsung',interval_s:15,lookups:8,source:'blocklist',reason:acrReason})});
assert.equal(tag(get('feed').children.at(-1)).parent.textContent,`[acr blocked] ${acrReason}`);
assert.equal(tag(get('feed').children.at(-1)).className,'tag tag-acr');
assert.equal(get('notice').textContent,acrReason);
assert.equal(get('acr').hidden,false);
assert.equal(get('acr-count').textContent,1);
assert.equal(get('acr-list').children[0].textContent,acrReason);
socket.onmessage({data:JSON.stringify({type:'acr_detected',ts:Date.now()+1,device_mac:'f0:4f:7c:11:22:33',device:'TV',domain:'log-ingestion.samsungacr.com',acr_name:'samsungacr.com',provider:'Samsung',interval_s:16,lookups:12,source:'blocklist',reason:acrReason.replace('15','16')})});
assert.equal(get('acr-count').textContent,1,'one card line per device and domain, the latest');
assert.match(get('acr-list').children[0].textContent,/every 16 seconds/);
assert.equal(get('requests').textContent,'4','ACR detections are not DNS requests');

// Rollbacks: a [rolled back?] row and a toast with the API's reason, which says it's a guess.
const rollbackReason="I removed the block on a.example for TV: it asked for a.example 14 times in a minute after the block, which can mean it stopped working. That's a guess, not proof it broke. Block it again as a pinned rule to keep it.";
const rolledBack=applied=>socket.onmessage({data:JSON.stringify({type:'rule_rolled_back',ts:Date.now(),rule:{id:9,pattern:'a.example',action:'block',device_mac:'f0:4f:7c:11:22:33',device:'TV',created_by:'voice',pinned:false},device_mac:'f0:4f:7c:11:22:33',device:'TV',sign:'retry_storm',reason:rollbackReason,applied})});
rolledBack(true);
assert.equal(tag(get('feed').children.at(-1)).parent.textContent,`[rolled back?] ${rollbackReason}`);
assert.equal(tag(get('feed').children.at(-1)).className,'tag tag-rollback');
assert.equal(get('notice').textContent,rollbackReason);
rolledBack(false);
assert.equal(get('notice').textContent,`${rollbackReason} Eddie's DNS hasn't confirmed the block is gone yet.`);
assert.equal(get('requests').textContent,'4','rollbacks are not DNS requests');

// Confirming shows the pending read-back, and clears on expiry and on dismissal.
const confirm=()=>socket.onmessage({data:JSON.stringify({type:'voice_state',state:'confirming',kind:'lockdown',prompt:'Lock down the TV?',expires_in_ms:15000})});
confirm();
assert.equal(get('assistant-pending').textContent,'Lock down the TV?');
assert.equal(get('assistant-pending').hidden,false);
assert.equal(get('assistant-state').textContent,'SAY YES TO CONFIRM OR NO TO CANCEL');
timers.filter(t=>t.ms===15000).at(-1).fn();
assert.equal(get('assistant-pending').hidden,true,'expiry clears the pending action');
assert.equal(get('assistant-state').textContent,'EDDIE IS LISTENING');
confirm();
socket.onmessage({data:JSON.stringify({type:'voice_state',state:'dismissed',is_eddie_active:false})});
assert.equal(get('assistant-pending').hidden,true,'dismissal clears the pending action');
assert.equal(get('assistant-pending').textContent,'');

assert.doesNotMatch(source,/localStorage|sessionStorage|indexedDB|document\.cookie/,'nothing is persisted client-side');
assert.equal(get('broker-count').textContent,'0','an ordinary lookup is not broker traffic');
socket.onmessage({data:JSON.stringify({domain:'rlcdn.com',device:'TV',action:'allow',broker:'data broker',ts:Date.now()})});
assert.equal(get('broker-count').textContent,'1','broker lookups get their own counter');
assert.equal(get('feed').children.at(-1).className,'event broker','broker lookups are highlighted');
assert.match(get('feed').children.at(-1).textContent,/\[approved\] TV → rlcdn\.com \[data broker\]$/);
// A snapshot's verdicts sit among its DNS lines by time, not in a block at the end.
socket.onmessage({data:JSON.stringify({type:'snapshot',events:[
  {domain:'a.example',device:'TV',action:'flag',ts:1000},{domain:'c.example',device:'TV',action:'allow',ts:3000}],
  ai_verdicts:[{type:'ai_verdict',domain:'b.example',label:'needed',reason:'Why',ts:2000}],totals:{seen:2,blocked:0}})});
assert.deepEqual(get('feed').children.map(row=>row.textContent.match(/[abc]\.example/)[0]),['a.example','b.example','c.example']);
assert.equal(get('broker-count').textContent,'0','a snapshot recounts broker lookups from its own events');
console.log('Frontend query/verdict/rule/confirmation event checks passed');

// "What they know": the demo week always shows its banner; live facts never do.
context.window.eddie.renderReport({demo:true,label:'Demo data: an invented, seeded week.',hours:168,facts:[{fact:'Streaming happens most on Sundays',evidence:'450 of 1691 streaming minutes',confidence:.65}]});
assert.equal(get('report-demo').hidden,false);
assert.match(get('report-demo').textContent,/^DEMO DATA · Demo data/);
assert.match(get('report-title').textContent,/DEMO WEEK/);
assert.equal(get('report-facts').children[0].textContent,'Streaming happens most on Sundays — 450 of 1691 streaming minutes (confidence 65%)');
context.window.eddie.renderReport({demo:false,hours:24,facts:[]});
assert.equal(get('report-demo').hidden,true);
assert.equal(get('report-title').textContent,'what they know · last 24h');

// Device panel (#35): name, distinct trackers blocked and the latest blocked domains.
(async () => {
  const flush = () => new Promise(resolve => setImmediate(resolve));
  const calls = [];
  // Each fetch waits until the test answers it, so the pending state can be checked in between.
  context.fetch = (path, init) => new Promise(resolve => calls.push({path, init, reply: (status, body) => resolve({ok: status < 400, status, json: async () => body})}));
  const answer = async (path, status, body) => { const i = calls.findIndex(c => c.path === path); assert.ok(i >= 0, `no request to ${path}`); calls.splice(i, 1)[0].reply(status, body); await flush(); await flush(); };

  socket.onmessage({data:JSON.stringify({type:'snapshot',events:[],totals:{seen:0,blocked:0},home_mode:false})});
  await answer('/api/devices', 200, {hours:24, devices:[
    {mac:'f0:4f:7c:11:22:33', name:'Fire Stick', trackers:14, blocked:160, recent_blocks:[{domain:'device-metrics-us.amazon.com',ts:2},{domain:'unagi-na.amazon.com',ts:1}]},
    {mac:'172.20.10.9', name:'172.20.10.9', trackers:1, blocked:1, recent_blocks:[{domain:'ads.example',ts:1}]},
    ...['a','b','c','d'].map(n => ({mac:n, name:n, trackers:0, blocked:0, recent_blocks:[]}))]});
  assert.equal(get('device-count').textContent, 6);
  const rows = get('device-list').children;
  assert.equal(rows[0].textContent, 'Fire Stick · 14 trackers blocked');
  assert.equal(rows[0].children[0].textContent, 'recent: device-metrics-us.amazon.com, unagi-na.amazon.com');
  assert.equal(rows[1].textContent, '172.20.10.9 · 1 tracker blocked');
  assert.equal(rows[2].children.length, 0, 'no recent line without blocks');
  assert.equal(rows.at(-1).textContent, '+1 more');

  // Modes: the initial state comes from the backend; until then the toggles can't be pressed.
  assert.equal(get('home-mode').disabled, true);
  await answer('/api/mode', 200, {home_mode:false, offline:false, offline_applied:true});
  assert.equal(get('home-mode').attrs['aria-label'], 'home mode: off');
  assert.equal(get('home-mode').disabled, false);
  // A stored spoof rule (#99) turns the house into Pinocchio; the button still toggles home mode.
  assert.equal(get('home-mode').attrs['data-spoofing'], 'false');
  await answer('/api/rules', 200, [{id:1, pattern:'*.tracker.example', action:'spoof', device:null, created_by:'user'},
                                   {id:2, pattern:'a.example', action:'block', device:null, created_by:'user'}]);
  assert.equal(get('home-mode').attrs['data-spoofing'], 'true');
  assert.equal(get('home-mode').attrs['aria-label'], 'home mode: off · spoofing 1 rule');
  context.renderRules([]);
  assert.equal(get('home-mode').attrs['data-spoofing'], 'false');
  assert.equal(get('home-mode').attrs['aria-label'], 'home mode: off');
  assert.equal(get('offline-mode').textContent, 'offline mode: off');
  assert.equal(get('mic').textContent, 'start mic');
  socket.onmessage({data:JSON.stringify({type:'voice_status',status:'ready',engine:'vosk'})});
  assert.equal(get('mic').textContent, 'stop mic');
  socket.onmessage({data:JSON.stringify({type:'voice_status',status:'off'})});
  assert.equal(get('mic').textContent, 'start mic');

  // A click shows pending, not the new state, until the bridge answers with the API's echo.
  get('home-mode').onclick();
  assert.equal(get('home-mode').attrs['aria-label'], 'home mode: turning on…');
  assert.equal(get('home-mode').disabled, true);
  assert.equal(get('home-mode').attrs['aria-busy'], 'true');
  assert.equal(get('home-mode').attrs['aria-pressed'], 'false', 'not shown as on before the backend confirms');
  get('home-mode').onclick();
  assert.equal(calls.filter(c => c.path === '/api/mode' && c.init).length, 1, 'a second tap while pending sends nothing');
  const sent = calls.find(c => c.path === '/api/mode' && c.init);
  assert.equal(sent.init.method, 'POST');
  assert.deepEqual(JSON.parse(sent.init.body), {home_mode:true});
  await answer('/api/mode', 200, {home_mode:true});
  assert.equal(get('home-mode').attrs['aria-label'], 'home mode: on');
  assert.equal(get('home-mode').attrs['aria-pressed'], 'true');
  assert.equal(get('home-mode').attrs['aria-busy'], 'false');
  assert.equal(get('home-note').textContent, 'home mode is a backend flag; DNS does not enforce it yet');

  // A failed change keeps the last acknowledged state and says so.
  get('home-mode').onclick();
  await answer('/api/mode', 502, {message:'The traffic API did not confirm the change.'});
  assert.equal(get('notice').textContent, 'Home mode change not confirmed: The traffic API did not confirm the change.');
  assert.equal(get('home-mode').attrs['aria-label'], 'home mode: on');
  await answer('/api/mode', 200, {home_mode:true, offline:false, offline_applied:true});  // the reload after a failure

  // Offline mode the API stored but DNS hasn't loaded is shown in red, not as done.
  get('offline-mode').onclick();
  assert.equal(get('offline-mode').textContent, 'offline mode: turning on…');
  assert.equal(get('mic').textContent, 'start mic', 'no offline prefix before the backend confirms');
  assert.deepEqual(JSON.parse(calls.find(c => c.path === '/api/offline-mode').init.body), {offline:true});
  await answer('/api/offline-mode', 200, {offline:true, applied:false});
  assert.equal(get('offline-mode').textContent, 'offline mode: on');
  assert.equal(get('mic').textContent, 'offline • start mic');
  assert.equal(get('offline-note').textContent, 'offline mode on not yet applied by DNS');
  assert.equal(get('home-note').textContent, 'home mode is a backend flag; DNS does not enforce it yet', 'both notes show at once');

  // Backend broadcasts (another screen, or voice) update the toggles.
  socket.onmessage({data:JSON.stringify({type:'offline_mode', offline:true, applied:true})});
  assert.equal(get('offline-note').textContent, '');
  socket.onmessage({data:JSON.stringify({type:'mode', home_mode:false})});
  assert.equal(get('home-mode').attrs['aria-label'], 'home mode: off');
  assert.equal(get('home-note').textContent, '');
  assert.equal(get('requests').textContent, '0', 'mode events are not DNS requests (the snapshot reset the count)');

  // Away mode shows only once DNS has loaded it, with who it covers and who stays up.
  const away = {type:'away_mode', away:true, away_until:null, away_applied:false,
    away_devices:[{key:'a', name:'TV', score:3}, {key:'b', name:'Laptop', score:1}], away_exempt:[{key:'c', name:'Porch cam'}]};
  socket.onmessage({data:JSON.stringify(away)});
  assert.equal(get('away-note').textContent, 'away mode on not yet applied by DNS');
  socket.onmessage({data:JSON.stringify({...away, away_applied:true})});
  assert.equal(get('away-note').textContent, 'away mode: on · cutting telemetry from TV, Laptop · Porch cam stays up');
  const until = Date.now() + 3600000;
  socket.onmessage({data:JSON.stringify({...away, away_applied:true, away_until:until, away_exempt:[]})});
  assert.equal(get('away-note').textContent, `away mode: on until ${new Date(until).toLocaleString([], {weekday:'short', hour:'numeric', minute:'2-digit'})} · cutting telemetry from TV, Laptop`);
  socket.onmessage({data:JSON.stringify({...away, away_applied:true, away_until:Date.now() - 1})});
  assert.equal(get('away-note').textContent, '', 'past its end time it is off');
  socket.onmessage({data:JSON.stringify({type:'away_mode', away:false, away_until:null, away_applied:true, away_devices:[], away_exempt:[]})});
  assert.equal(get('away-note').textContent, '');

  // No backend: the toggles say unavailable and can't be pressed.
  timers.filter(t => t.ms === 30000).at(-1).fn();
  await answer('/api/devices', 502, {message:'Devices unavailable.'});
  await answer('/api/mode', 502, {message:'Modes unavailable.'});
  assert.equal(get('device-count').textContent, 'unavailable');
  assert.equal(get('home-mode').attrs['aria-label'], 'home mode: unavailable');
  assert.equal(get('offline-mode').disabled, true);
  console.log('Frontend device panel and mode toggle checks passed');

  // Ask before blocking (#79): the card shows the API's question and says blocked only on applied: true.
  const q = {id:1, device:'Laptop', domains:[{domain:'px.example', reason:'Fingerprinting'}], prompt:'A new likely tracker on Laptop: px.example. Block it?', ts:1};
  socket.onmessage({data:JSON.stringify({type:'snapshot',events:[],totals:{seen:0,blocked:0},home_mode:false,questions:[q]})});
  assert.equal(get('question').hidden, false);
  assert.equal(get('question-text').textContent, q.prompt);
  assert.equal(get('feed').children.length, 0, 'a question is not a feed line');
  get('question-actions').children[0].onclick();
  assert.equal(get('question-actions').children[0].disabled, true);
  assert.deepEqual(JSON.parse(calls.find(c => c.path === '/api/questions/answer').init.body), {id:1, answer:'block'});
  await answer('/api/questions/answer', 200, {id:1, answer:'block', rules:[{id:9, pattern:'px.example'}], applied:false});
  assert.doesNotMatch(get('question-result').textContent, /^Blocked/);
  assert.match(get('question-result').textContent, /isn't confirmed/);
  assert.equal(get('question-undo').hidden, false);
  assert.equal(get('question-actions').hidden, true);
  socket.onmessage({data:JSON.stringify({type:'question', ...q, id:2})});
  assert.equal(get('question-actions').hidden, false);
  get('question-actions').children[0].onclick();
  await answer('/api/questions/answer', 200, {id:2, answer:'block', rules:[{id:10, pattern:'px.example'}], applied:true});
  assert.match(get('question-result').textContent, /^Blocked px\.example on every device/);
  socket.onmessage({data:JSON.stringify({type:'question', ...q, id:3})});
  socket.onmessage({data:JSON.stringify({type:'question_resolved', id:3, answer:'ignore', by:'voice', applied:null})});
  assert.equal(get('question-actions').hidden, true);
  console.log('Frontend question card checks passed');
})().catch(error => { console.error(error); process.exit(1); });
