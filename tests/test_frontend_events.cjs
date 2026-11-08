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
  addEventListener(){} setAttribute(k,v){this.attrs[k]=v;} removeAttribute(k){delete this.attrs[k];} focus(){}
}
const ids=new Map();
const get=id=>{if(!ids.has(id))ids.set(id,new Element());return ids.get(id);};
let socket;
const timers=[];
const context={document:{getElementById:get,createElement:()=>new Element(),addEventListener(){},activeElement:{tagName:'BODY'}},window:{},location:{search:'',hostname:'localhost'},URLSearchParams,Date,console,
  setTimeout(fn,ms){timers.push({fn,ms});return timers.length;},clearTimeout(){},fetch(){},AbortController,EventSource:class{},WebSocket:class{constructor(){socket=this;}}};
const source=fs.readFileSync('frontend/app.js','utf8');
const styles=fs.readFileSync('frontend/style.css','utf8');
assert.match(styles, /prefers-reduced-motion:reduce[^}]*\.character\[data-motion\]/, 'reduced motion disables role animation');
assert.match(styles, /data-state="speaking"[^}]*--edge:45,113,255/, 'speaking remains blue for every role');
assert.match(styles, /--edge:255,210,55/, 'listening remains yellow for every role');
assert.match(styles, /data-state="thinking"\][^}]*animation:think-sway/, 'thinking sways the glow');
assert.doesNotMatch(styles, /data-state="thinking"\][^}]*--edge/, 'thinking keeps the listening colour');
// Concierge (#162): one gold accent with a forced-colours fallback, and a hat-tip that runs once, not on a loop.
assert.match(styles, /\.bellhop-e\{stroke:#e8b931/, 'the E is gold');
assert.match(styles, /forced-colors:active\)\{\.character \.bellhop-hat \.bellhop-e\{stroke:CanvasText/, 'the E has a monochrome fallback');
assert.match(styles, /\[data-hat-tip\] \.bellhop-hat\{[^}]*animation:role-hat-tip [^};]* 1\}/, 'the hat-tip plays once');
assert.doesNotMatch(styles, /role-hat-tip[^}]*infinite/, 'the hat-tip never loops');
assert.match(fs.readFileSync('frontend/index.html','utf8'), /<\/g><g class="role-accessory bellhop-hat"/, 'the hat is drawn over the head, outside the eyes and mouth');
const page = fs.readFileSync('frontend/index.html','utf8');
assert.match(page, /<footer>.*id="open-rules"[^>]*>rules \(<span id="rules-count">.*id="open-devices".*id="offline-mode".*id="suggestions-open"[^>]*>suggestions<.*id="capabilities-open"[^>]*>capabilities<\/button><\/div><\/footer>/, 'footer: rules, devices, online/offline, suggestions, capabilities last');
assert.doesNotMatch(page, /learn more/, 'learn more is gone');
assert.match(page, /id="suggestions-open"[^>]*hidden>/, 'no suggestions link in the footer');
assert.match(page, /id="capabilities-close"[^>]*hidden>/, 'capabilities has no close button');
assert.match(page, /id="close-devices"[^>]*hidden>/, 'devices has no close button');
assert.match(page, /<div class="devices-controls" hidden>.*id="kid-mode".*id="guest-mode".*id="quarantine-mode".*class="devices-hint".*<\/div><ul id="device-list">/, 'devices sheet shows just the devices');
assert.match(styles, /footer button span\{margin-left:0\}/, 'no stray space in "rules (7)"');
assert.doesNotMatch(page, /class="panels"|id="rules" class="rules"/, 'no rules panel on the main screen');
assert.match(page, /id="tracker-map-open"[^>]*hidden/, 'who sees what is hidden for now');
assert.match(page, /id="devices-sheet".*id="quarantine-mode".*id="device-list"/, 'quarantine lives with the devices');
assert.doesNotMatch(source, /DNS-only/, 'no DNS-only note');
assert.match(page, /<section id="capabilities"[^>]*hidden>.*what to ask eddie.*List my rules/, 'capabilities pane lists what to ask');
assert.doesNotMatch(page.slice(page.indexOf('id="suggestions"'), page.indexOf('id="tracker-map"')), /what to ask/, 'suggestions are just the privacy settings');
// getElementById returns the first match, so a repeated id makes one element overwrite another.
const pageIds=[...fs.readFileSync('frontend/index.html','utf8').matchAll(/\sid="([^"]+)"/g)].map(m=>m[1]);
assert.deepEqual(pageIds.filter((id,i)=>pageIds.indexOf(id)!==i),[],'every id in index.html is unique');
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
context.window.eddie.renderReport({demo:true,label:'Demo data',hours:168,facts:[{fact:'Someone here uses health, pharmacy or telehealth services',evidence:'14 lookups to GoodRx',confidence:.7,category:'Health',sensitive:true},{fact:'Someone here shops online',evidence:'19 lookups to Target',confidence:.5,category:'Money',sensitive:false}]});
assert.equal(get('report-facts').children[0].textContent,'Health · sensitive: Someone here uses health, pharmacy or telehealth services — 14 lookups to GoodRx (confidence 70%)');
assert.equal(get('report-facts').children[1].textContent,'Money: Someone here shops online — 19 lookups to Target (confidence 50%)');
context.window.eddie.renderReport({demo:false,hours:24,facts:[]});
assert.equal(get('report-demo').hidden,true);
assert.equal(get('report-title').textContent,'what they know · last 24h');

// Devices (#35, #119): one row per device, opened from the link bottom right.
(async () => {
  const flush = () => new Promise(resolve => setImmediate(resolve));
  const calls = [];
  // Each fetch waits until the test answers it, so the pending state can be checked in between.
  context.fetch = (path, init) => new Promise(resolve => calls.push({path, init, reply: (status, body) => resolve({ok: status < 400, status, json: async () => body})}));
  const answer = async (path, status, body) => { const i = calls.findIndex(c => c.path === path); assert.ok(i >= 0, `no request to ${path}`); calls.splice(i, 1)[0].reply(status, body); await flush(); await flush(); };

  socket.onmessage({data:JSON.stringify({type:'snapshot',events:[],totals:{seen:0,blocked:0},home_mode:false})});
  await answer('/api/devices', 200, {hours:24, devices:[
    {mac:'f0:4f:7c:11:22:33', ip:'172.20.10.5', label:'Fire Stick', name:'Fire Stick', vendor:'Amazon', kid:false, trackers:14, blocked:160,
     recent_domains:['device-metrics-us.amazon.com', 'netflix.com'], recent_blocks:[]},
    {mac:'172.20.10.9', ip:'172.20.10.9', label:null, name:'172.20.10.9', kid:true, trackers:1, blocked:1, recent_domains:[], recent_blocks:[]},
    {mac:'aa:bb:cc:dd:ee:07', ip:'172.20.10.7', label:null, name:'aa:bb:cc:dd:ee:07', status:'pending', kid:false, trackers:0, blocked:0, recent_domains:[]}]});
  assert.equal(get('device-count').textContent, 3);
  let rows = get('device-list').children;
  assert.equal(rows[0].dataset.mac, 'aa:bb:cc:dd:ee:07', 'a new device comes first, with its decision buttons');
  assert.equal(rows[0].children.at(-1).textContent, '[new]approvedeny');
  const [name, kid, info, looked] = rows[1].children;
  assert.equal(name.value, 'Fire Stick');
  assert.equal(name.placeholder, '---');
  assert.equal(rows[2].children[0].value, '', 'an unnamed device shows ---, never its vendor or IP as a name');
  assert.equal(rows[2].children[0].placeholder, '---');
  name.onfocus(); assert.equal(name.placeholder, 'add a title'); name.value = 'Fire Stick'; name.onblur();
  assert.equal(name.placeholder, '---');
  assert.ok(!calls.some(c => c.path === '/api/devices/update'), 'an unchanged name is not sent');
  assert.equal(kid.textContent, 'adult');
  assert.equal(rows[2].children[1].textContent, 'child');
  assert.equal(rows[2].children[1].attrs['aria-pressed'], 'true');
  assert.equal(info.textContent, '172.20.10.5 · Amazon · 14 trackers blocked', 'the IP goes under the name');
  assert.equal(looked.textContent, 'looked up: device-metrics-us.amazon.com, netflix.com');
  assert.equal(rows[2].children.length, 3, 'no looked-up line without lookups');
  assert.equal(get('kid-mode').textContent, 'kid mode: off');
  assert.equal(get('kid-note').textContent, "children's devices: --- (172.20.10.9)");
  // Kid mode reads only stored rules: a device "*" block rule is a locked-down device.
  context.renderRules([{id:3, pattern:'*', action:'block', device_mac:'172.20.10.9', device:'172.20.10.9', created_by:'voice'}]);
  assert.equal(get('kid-mode').textContent, 'kid mode: on');
  assert.equal(get('kid-mode').attrs['aria-pressed'], 'true');
  rows = get('device-list').children;
  assert.equal(rows[2].children[2].textContent, '172.20.10.9 · locked down · 1 tracker blocked');

  // Naming: Enter or leaving the field saves it; the toast says how voice finds it.
  const unnamed = rows[2].children[0];
  unnamed.onfocus(); unnamed.value = ' Kids tablet '; unnamed.onblur();
  assert.deepEqual(JSON.parse(calls.find(c => c.path === '/api/devices/update').init.body), {mac:'172.20.10.9', label:'Kids tablet'});
  await answer('/api/devices/update', 200, {mac:'172.20.10.9', label:'Kids tablet', kid:true});
  assert.equal(get('notice').textContent, 'Named Kids tablet. Voice can find it as "Kids tablet".');
  await answer('/api/devices', 200, {hours:24, devices:[]});
  context.renderDevices([{mac:'172.20.10.9', ip:'172.20.10.9', label:'Kids tablet', kid:true, trackers:0, recent_domains:[]},
                         {mac:'f0:4f:7c:11:22:33', ip:'172.20.10.5', label:'Fire Stick', kid:false, trackers:0, recent_domains:[]}]);
  rows = get('device-list').children;
  // A name being typed survives a refresh.
  rows[1].children[0].onfocus(); context.renderDevices([]); assert.equal(get('device-list').children.length, 2); assert.equal(get('device-count').textContent, 0);
  context.renderDevices([{mac:'172.20.10.9', ip:'172.20.10.9', label:'Kids tablet', kid:true, trackers:0, recent_domains:[]},
                         {mac:'f0:4f:7c:11:22:33', ip:'172.20.10.5', label:'Fire Stick', kid:false, trackers:0, recent_domains:[]}]);
  rows[1].children[0].onblur();

  // Child or adult: one click, sent as kid; a newly marked child isn't covered until kid mode runs again.
  context.confirm = () => true;
  rows[1].children[1].onclick();
  assert.deepEqual(JSON.parse(calls.find(c => c.path === '/api/devices/update').init.body), {mac:'f0:4f:7c:11:22:33', kid:true});
  await answer('/api/devices/update', 200, {mac:'f0:4f:7c:11:22:33', kid:true});
  assert.equal(get('notice').textContent, "Fire Stick is a child's device. Kid mode doesn't cover it until you turn kid mode on again.");
  await answer('/api/devices', 200, {hours:24, devices:[
    {mac:'172.20.10.9', ip:'172.20.10.9', label:'Kids tablet', kid:true, trackers:0, recent_domains:[]},
    {mac:'f0:4f:7c:11:22:33', ip:'172.20.10.5', label:'Fire Stick', kid:true, trackers:0, recent_domains:[]}]});
  assert.equal(get('kid-mode').textContent, 'kid mode: on for 1 of 2');
  assert.equal(get('kid-mode').attrs['aria-pressed'], 'false');

  // The kid mode button asks first, then shows the bridge's own words, never its own claim.
  let asked = '';
  context.confirm = text => { asked = text; return false; };
  get('kid-mode').onclick();
  assert.equal(asked, "Turn on kid mode? Fire Stick will only reach the kid-safe list, plus Eddie's essentials like sign-in and updates.");
  assert.ok(!calls.some(c => c.path === '/api/kid-mode'), 'no request without a yes');
  context.confirm = () => true;
  get('kid-mode').onclick();
  assert.equal(get('kid-mode').textContent, 'kid mode: turning on…');
  assert.deepEqual(JSON.parse(calls.find(c => c.path === '/api/kid-mode').init.body), {on:true});
  await answer('/api/kid-mode', 200, {message:"I saved the lockdown rules for Fire Stick, but Eddie's DNS hasn't loaded them yet, so nothing is blocked yet."});
  assert.equal(get('notice').textContent, "I saved the lockdown rules for Fire Stick, but Eddie's DNS hasn't loaded them yet, so nothing is blocked yet.");
  await answer('/api/rules', 200, [{id:3, pattern:'*', action:'block', device_mac:'172.20.10.9', created_by:'voice'}]);
  await answer('/api/devices', 200, {hours:24, devices:[]});
  assert.equal(get('kid-mode').textContent, 'kid mode: off');
  assert.equal(get('kid-mode').disabled, true, 'no child device: nothing to switch');

  // The devices link opens and closes the list.
  get('open-devices').onclick();
  assert.equal(get('devices-sheet').hidden, false);
  assert.equal(get('open-devices').attrs['aria-expanded'], 'true');
  await answer('/api/rules', 200, []); await answer('/api/devices', 200, {hours:24, devices:[]});
  context.flashDevice('ip:172.20.10.9');  // a lookup lights up its row; no row, nothing happens
  get('close-devices').onclick();
  assert.equal(get('devices-sheet').hidden, true);

  // Modes: the initial state comes from the backend; until then the toggles can't be pressed.
  assert.equal(get('home-mode').disabled, true);
  await answer('/api/mode', 200, {home_mode:false, offline:false, offline_applied:true});
  assert.equal(get('character').dataset.role, 'regular', 'regular Eddie has no roof');
  const visualContract = {
    regular: ['none', 'upright', 'attentive'], home: ['roof', 'settled', 'breathe'],
    kid: ['propeller', 'playful', 'bounce'], concierge: ['bellhop', 'formal', 'hat-tip'],
    away: ['suitcase', 'ready', 'travel']
  };
  for (const [role, expected] of Object.entries(visualContract)) {
    socket.onmessage({data:JSON.stringify({type:'session_state', role, acknowledged:true})});
    assert.deepEqual([get('character').dataset.accessory, get('character').dataset.stance, get('character').dataset.motion], expected, `${role} uses the shared visual contract`);
  }
  socket.onmessage({data:JSON.stringify({type:'session_state', role:'kid', acknowledged:true})});
  socket.onmessage({data:JSON.stringify({type:'session_state', role:'concierge', acknowledged:true, pending:true})});
  assert.equal(get('character').dataset.role, 'kid', 'a pending session change keeps the acknowledged accessory');
  socket.onmessage({data:JSON.stringify({type:'session_state', role:'mystery', acknowledged:true})});
  assert.equal(get('character').dataset.role, 'regular', 'an unknown acknowledged role falls back to regular Eddie');
  socket.onmessage({data:JSON.stringify({type:'session_state', role:'regular', acknowledged:true})});
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
  assert.equal(get('offline-mode').textContent, 'online', 'the footer says just online or offline');
  // Mic indicator (#128): the heading's mic icon shows only while the stream is open, and the footer
  // button only offers to start it; stopping is by voice.
  assert.equal(get('mic').textContent, 'start mic');
  assert.equal(get('mic').hidden, false);
  assert.equal(get('mic-on').hidden, true);
  socket.onmessage({data:JSON.stringify({type:'voice_status',status:'starting'})});
  assert.equal(get('mic-on').hidden, true, 'models still loading: the stream is not open yet');
  socket.onmessage({data:JSON.stringify({type:'voice_status',status:'ready',engine:'vosk'})});
  assert.equal(get('mic-on').hidden, false);
  assert.equal(get('mic').hidden, true, 'no stop button while the mic is on');
  get('mic').onclick();
  assert.equal(calls.filter(c => c.path === '/api/voice/stop').length, 0, 'the footer never stops the mic');
  for (const status of ['off', 'error']) {
    socket.onmessage({data:JSON.stringify({type:'voice_status',status:'ready',engine:'vosk'})});
    socket.onmessage({data:JSON.stringify({type:'voice_status',status})});
    assert.equal(get('mic-on').hidden, true, status);
    assert.equal(get('mic').hidden, false, status);
    assert.equal(get('mic').textContent, 'start mic');
  }

  // A click shows pending, not the new state, until the bridge answers with the API's echo.
  get('home-mode').onclick();
  assert.equal(get('character').dataset.role, 'regular', 'pending Home Mode does not add the roof');
  assert.equal(get('home-mode').attrs['aria-label'], 'home mode: turning on…');
  assert.equal(get('home-mode').disabled, true);
  assert.equal(get('home-mode').attrs['aria-busy'], 'true');
  assert.equal(get('home-mode').attrs['aria-pressed'], 'false', 'not shown as on before the backend confirms');
  get('home-mode').onclick();
  assert.equal(calls.filter(c => c.path === '/api/mode' && c.init).length, 1, 'a second tap while pending sends nothing');
  const sent = calls.find(c => c.path === '/api/mode' && c.init);
  assert.equal(sent.init.method, 'POST');
  assert.deepEqual(JSON.parse(sent.init.body), {home_mode:true});
  await answer('/api/mode', 200, {home_mode:true, home_manual:true, home_applied:true});
  assert.equal(get('home-mode').attrs['aria-label'], 'home mode: on');
  assert.equal(get('home-mode').attrs['aria-pressed'], 'true');
  assert.equal(get('home-mode').attrs['aria-busy'], 'false');
  assert.equal(get('home-note').textContent, 'home mode: blocking camera uploads · turned on by hand');
  assert.equal(get('character').dataset.role, 'home', 'acknowledged and applied Home Mode adds the roof');

  // A failed change keeps the last acknowledged state and says so.
  get('home-mode').onclick();
  await answer('/api/mode', 502, {message:'The traffic API did not confirm the change.'});
  assert.equal(get('notice').textContent, 'Home mode change not confirmed: The traffic API did not confirm the change.');
  assert.equal(get('home-mode').attrs['aria-label'], 'home mode: on');
  await answer('/api/mode', 200, {home_mode:true, offline:false, offline_applied:true});  // the reload after a failure

  // Offline mode the API stored but DNS hasn't loaded is shown in red, not as done.
  get('offline-mode').onclick();
  assert.equal(get('offline-mode').textContent, 'going offline…');
  assert.equal(get('mic').textContent, 'start mic', 'no offline prefix before the backend confirms');
  assert.deepEqual(JSON.parse(calls.find(c => c.path === '/api/offline-mode').init.body), {offline:true});
  await answer('/api/offline-mode', 200, {offline:true, applied:false});
  assert.equal(get('offline-mode').textContent, 'offline');
  assert.equal(get('mic').textContent, 'start mic', 'the footer already says offline');
  assert.equal(get('offline-note').textContent, 'offline mode on not yet applied by DNS');
  assert.equal(get('home-note').textContent, 'home mode: blocking camera uploads · turned on by hand', 'both notes show at once');

  // Backend broadcasts (another screen, or voice) update the toggles.
  socket.onmessage({data:JSON.stringify({type:'offline_mode', offline:true, applied:true})});
  assert.equal(get('offline-note').textContent, '');
  socket.onmessage({data:JSON.stringify({type:'mode', home_mode:false, home_manual:true, home_applied:true, reason:'manual'})});
  assert.equal(get('character').dataset.role, 'regular', 'exiting Home Mode removes the roof');
  assert.equal(get('home-mode').attrs['aria-label'], 'home mode: off');
  assert.equal(get('home-note').textContent, '', 'home mode off says nothing; the hollow house shows it');
  assert.equal(get('home-mode').hidden, true, 'the house flag hides when home mode is off');
  // Phone presence (#20): shown as on only once DNS has loaded it.
  socket.onmessage({data:JSON.stringify({type:'mode', home_mode:true, home_manual:false, home_applied:false, reason:'presence'})});
  assert.equal(get('character').dataset.role, 'home', 'backend-acknowledged Home Mode adds the roof while DNS catches up');
  assert.equal(get('home-note').textContent, 'home mode on not yet applied by DNS');
  socket.onmessage({data:JSON.stringify({type:'mode', home_mode:true, home_manual:false, home_applied:true, reason:'presence'})});
  assert.equal(get('character').dataset.role, 'home');
  assert.equal(get('home-note').textContent, 'home mode: blocking camera uploads · a household phone is home');
  assert.equal(get('home-mode').hidden, false, 'the house flag shows while home mode is on');
  socket.onmessage({data:JSON.stringify({type:'mode', home_mode:false, home_manual:false, home_applied:true, reason:'presence'})});
  assert.equal(get('home-note').textContent, '');
  assert.equal(get('requests').textContent, '0', 'mode events are not DNS requests (the snapshot reset the count)');

  // Away mode shows only once DNS has loaded it, with who it covers and who stays up.
  const away = {type:'away_mode', away:true, away_until:null, away_applied:false,
    away_devices:[{key:'a', name:'TV', score:3}, {key:'b', name:'Laptop', score:1}], away_exempt:[{key:'c', name:'Porch cam'}]};
  socket.onmessage({data:JSON.stringify(away)});
  assert.equal(get('away-note').textContent, 'away mode on not yet applied by DNS');
  socket.onmessage({data:JSON.stringify({...away, away_applied:true})});
  assert.equal(get('character').dataset.role, 'away', 'acknowledged away mode uses Away Eddie');
  assert.equal(get('away-note').textContent, 'away mode: on · cutting telemetry from TV, Laptop · Porch cam stays up');
  const until = Date.now() + 3600000;
  socket.onmessage({data:JSON.stringify({...away, away_applied:true, away_until:until, away_exempt:[]})});
  assert.equal(get('away-note').textContent, `away mode: on until ${new Date(until).toLocaleString([], {weekday:'short', hour:'numeric', minute:'2-digit'})} · cutting telemetry from TV, Laptop`);
  socket.onmessage({data:JSON.stringify({...away, away_applied:true, away_until:Date.now() - 1})});
  assert.equal(get('away-note').textContent, '', 'past its end time it is off');
  socket.onmessage({data:JSON.stringify({type:'away_mode', away:false, away_until:null, away_applied:true, away_devices:[], away_exempt:[]})});
  assert.equal(get('away-note').textContent, '');
  socket.onclose();
  assert.equal(get('character').dataset.role, 'regular', 'a disconnected stream falls back to regular Eddie');
  socket.onopen();

  // No backend: the toggles say unavailable and can't be pressed.
  timers.filter(t => t.ms === 30000).at(-1).fn();
  await answer('/api/devices', 502, {message:'Devices unavailable.'});
  await answer('/api/mode', 502, {message:'Modes unavailable.'});
  assert.equal(get('character').dataset.role, 'regular', 'disconnect falls back to regular Eddie');
  assert.equal(get('device-count').textContent, 'unavailable');
  assert.equal(get('home-mode').attrs['aria-label'], 'home mode: unavailable');
  assert.equal(get('offline-mode').disabled, true);
  console.log('Frontend devices, kid mode and mode toggle checks passed');

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

  // Tracker map (#107): companies down, devices across, ad-ID sync rows red with a block button.
  get('tracker-map-open').onclick();
  assert.equal(get('tracker-map').hidden, false);
  await answer('/api/tracker-map', 200, {window:'24h', device_count:3, devices:['Phone','TV','Tablet'], text:'Google sees 2 of your 3 devices.', companies:[
    {company:'Google', device_count:2, requests:3, blocked:1, devices:[{device:'TV',requests:2},{device:'Phone',requests:1}], id_sync:[]},
    {company:'LiveRamp', device_count:1, requests:1, blocked:0, devices:[{device:'TV',requests:1}], id_sync:[{domain:'rlcdn.com',requests:1,blocked:0,devices:['TV'],rule_id:null}]}]});
  assert.equal(get('tracker-map-text').textContent, 'Google sees 2 of your 3 devices.');
  const [thead, tbody] = get('tracker-map-grid').children;
  assert.deepEqual(thead.children[0].children.map(c => c.textContent), ['company','Phone','TV','Tablet']);
  assert.deepEqual(tbody.children[0].children.map(c => c.textContent), ['Google · 2/3','■','■','·']);
  assert.notEqual(tbody.children[0].className, 'sync');
  assert.equal(tbody.children[1].className, 'sync', 'an ad-ID sync company is marked for the red style');
  let sync = get('tracker-map-sync').children[0];
  assert.match(sync.children[0].textContent, /^\[id sync\] LiveRamp · rlcdn\.com · 1 device, 1 lookup, 0 blocked$/);
  const blockButton = sync.children[1];
  assert.equal(blockButton.textContent, 'block rlcdn.com');
  blockButton.onclick();
  assert.equal(blockButton.disabled, true);
  assert.equal(JSON.parse(calls.find(c => c.path === '/api/tracker-map/block').init.body).domain, 'rlcdn.com');
  await answer('/api/tracker-map/block', 200, {pattern:'*.rlcdn.com', existing:false, applied:false});
  assert.match(get('notice').textContent, /hasn't confirmed/, 'a stored rule is not claimed as enforced');
  await answer('/api/tracker-map', 200, {device_count:1, devices:['TV'], text:'LiveRamp sees 1 of your 1 device.', companies:[
    {company:'LiveRamp', device_count:1, devices:[{device:'TV',requests:1}], id_sync:[{domain:'rlcdn.com',requests:1,blocked:0,devices:['TV'],rule_id:4}]}]});
  sync = get('tracker-map-sync').children[0];
  assert.equal(sync.children[1].textContent, 'rule in place');
  assert.equal(sync.children[1].disabled, true);
  sync.children[1].disabled = false; sync.children[1].onclick();
  await answer('/api/tracker-map/block', 502, {message:'The traffic API did not confirm the block.'});
  assert.match(get('notice').textContent, /^Couldn't block rlcdn\.com: The traffic API/);
  assert.equal(sync.children[1].disabled, false);
  get('tracker-map-close').onclick();
  assert.equal(get('tracker-map').hidden, true);
  console.log('Frontend tracker map checks passed');

  // Guest Mode (#163): shown only as the bridge saved it; the bridge's refusal is the toast.
  for (let i; (i = calls.findIndex(c => c.path === '/api/guest')) >= 0;) calls.splice(i, 1);  // earlier panel refreshes
  context.loadGuest();
  await answer('/api/guest', 200, {active:false, allowed:[], approvable:['house_guide','timers'], owner_only:['traffic']});
  assert.equal(get('guest-mode').textContent, 'guest mode: off');
  assert.deepEqual(get('guest-allowed').children.map(l => [l.children[0].checked, l.children[1].textContent]), [[false,'house guide'],[false,'timers']]);
  let guestAsk = '';
  context.confirm = text => { guestAsk = text; return true; };
  get('guest-mode').onclick();
  assert.match(guestAsk, /every voice as a guest/);
  assert.equal(get('guest-mode').textContent, 'guest mode: off', 'not shown as on before the bridge saves it');
  assert.equal(get('character').dataset.role, 'regular', 'a guest switch press still waiting on the bridge adds no hat');
  await answer('/api/guest', 200, {active:true, allowed:[], approvable:['house_guide','timers'], owner_only:['traffic']});
  assert.equal(get('guest-mode').textContent, 'guest mode: on');
  // Concierge Eddie (#162): the saved profile puts the hat on and tips it once.
  assert.equal(get('character').dataset.role, 'concierge', 'an acknowledged guest session wears the bellhop hat');
  assert.equal(get('character').dataset.accessory, 'bellhop');
  assert.equal(get('character').dataset.hatTip, 'once', 'activation tips the hat');
  delete get('character').dataset.hatTip;  // stands in for the animation having played
  context.renderModes(); context.renderGuest();
  assert.equal(get('character').dataset.hatTip, undefined, 're-renders do not tip again');
  socket.onclose();
  assert.equal(get('character').dataset.role, 'regular', 'a disconnected stream drops the hat');
  socket.onopen();
  assert.equal(get('character').dataset.role, 'concierge', 'reconnecting to the same guest session restores the hat');
  assert.equal(get('character').dataset.hatTip, undefined, 'a reconnect is not a new activation');
  assert.equal(get('notice').textContent, 'Guest mode is on.');
  const timersBox = get('guest-allowed').children[1].children[0];
  timersBox.checked = true; timersBox.onchange();
  assert.deepEqual(JSON.parse(calls.find(c => c.path === '/api/guest').init.body), {allowed:['timers']});
  await answer('/api/guest', 403, {message:'This control only works on the Eddie computer.'});
  assert.equal(get('notice').textContent, 'Guest mode not saved: This control only works on the Eddie computer.');
  assert.equal(get('guest-allowed').children[1].children[0].checked, false, 'a refused change is not shown');
  // Checkout returns Eddie to the owner's acknowledged role; the next guest session tips the hat again.
  socket.onmessage({data:JSON.stringify({type:'mode', home_mode:true, home_manual:true, home_applied:true})});
  assert.equal(get('character').dataset.role, 'concierge', 'Guest Mode wins over home mode');
  context.loadGuest(); await answer('/api/guest', 502, {message:'unavailable'});
  assert.equal(get('character').dataset.role, 'home', 'an unreadable guest profile drops the hat');
  context.loadGuest(); await answer('/api/guest', 200, {active:true, allowed:[], approvable:['house_guide','timers'], owner_only:['traffic']});
  assert.equal(get('character').dataset.hatTip, undefined, 'a poll blip is not a new activation');
  context.confirm = () => true; get('guest-mode').onclick();
  await answer('/api/guest', 200, {active:false, allowed:[], approvable:['house_guide','timers'], owner_only:['traffic']});
  assert.equal(get('character').dataset.role, 'home', 'checkout restores owner Home Eddie');
  assert.equal(get('character').dataset.accessory, 'roof');
  get('guest-mode').onclick();
  await answer('/api/guest', 200, {active:true, allowed:[], approvable:['house_guide','timers'], owner_only:['traffic']});
  assert.equal(get('character').dataset.hatTip, 'once', 'the next guest session tips the hat again');
  socket.onmessage({data:JSON.stringify({type:'mode', home_mode:false, home_manual:true, home_applied:true})});
  console.log('Frontend guest mode checks passed');
})().catch(error => { console.error(error); process.exit(1); });

// EDDIE_SPEAKER=ipad: a reply clip waits for the first tap, then plays through Web Audio.
(async () => {
  const fetched = [], started = [];
  class AudioContext {
    constructor() { this.state = 'suspended'; this.destination = {}; }
    resume() { this.state = 'running'; }
    createBuffer() { return 'silence'; }
    createBufferSource() { return {connect() {}, start() { if (this.buffer !== 'silence') started.push(this.buffer); }}; }
    async decodeAudioData(bytes) { return {decoded: bytes}; }
  }
  context.window.AudioContext = AudioContext;
  const otherFetch = context.fetch;  // the checks above are still running against it
  context.fetch = (path, init) => !String(path).startsWith('/api/voice/audio/') ? otherFetch(path, init)
    : (fetched.push(path), Promise.resolve({ok: true, arrayBuffer: async () => 'wav'}));
  context.consumeEvent({type: 'speech_audio', id: 'abc'});
  assert.equal(get('voice-unlock').hidden, false, 'a reply before any tap asks for one');
  assert.deepEqual(fetched, [], 'nothing is fetched until sound is allowed');
  get('voice-unlock').onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(get('voice-unlock').hidden, true);
  assert.deepEqual(fetched, ['/api/voice/audio/abc'], 'the waiting reply plays after the tap');
  assert.deepEqual(started, [{decoded: 'wav'}]);
  context.consumeEvent({type: 'speech_audio', id: 'def'});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(fetched.at(-1), '/api/voice/audio/def', 'later replies play straight away');
  console.log('Frontend dashboard voice checks passed');
})().catch(error => { console.error(error); process.exit(1); });
