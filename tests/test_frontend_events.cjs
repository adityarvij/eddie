const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
class Element {
  get textContent(){return this._text ?? this.children.map(node=>node.textContent).join('');}
  set textContent(value){this._text=value;}
  constructor(){this.children=[];this.hidden=true;this.dataset={};this.firstChild={textContent:''};this.classList={add(){},remove(){},toggle(){}};}
  append(...nodes){for(const node of nodes){node.parent=this;this.children.push(node);}}
  prepend(...nodes){for(const node of nodes){node.parent=this;this.children.unshift(node);}}
  remove(){this.parent.children=this.parent.children.filter(node=>node!==this);}
  querySelector(){return null;} cloneNode(){return new Element();}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  addEventListener(){} setAttribute(){} focus(){}
}
const ids=new Map();
const get=id=>{if(!ids.has(id))ids.set(id,new Element());return ids.get(id);};
let socket;
const timers=[];
const context={document:{getElementById:get,createElement:()=>new Element(),addEventListener(){},activeElement:{tagName:'BODY'}},window:{},location:{search:'',hostname:'localhost'},URLSearchParams,Date,console,
  setTimeout(fn,ms){timers.push({fn,ms});return timers.length;},clearTimeout(){},fetch(){},EventSource:class{},WebSocket:class{constructor(){socket=this;}}};
const source=fs.readFileSync('frontend/app.js','utf8');
vm.runInNewContext(source,context);
socket.onmessage({data:JSON.stringify({domain:'example.org',device:'TV',action:'allow',ts:Date.now()})});
assert.equal(get('requests').textContent,'1');
socket.onmessage({data:JSON.stringify({type:'ai_verdict',domain:'example.org',label:'needed',confidence:.9,reason:'App content',ts:Date.now()})});
assert.equal(get('requests').textContent,'1','AI events must not be counted as DNS requests');
assert.equal(get('allowed').textContent,'1');
assert.match(get('feed').children.at(-1).textContent,/^\[ai\] example\.org → needed/);
const tag=row=>row.children.at(-1).children[0];  // a row ends with the line: [tag, text]
assert.equal(tag(get('feed').children.at(-1)).className,'tag tag-ai');
socket.onmessage({data:JSON.stringify({domain:'ads.example',device:'TV',action:'block',reason:'On a list',ts:Date.now()})});
assert.match(get('feed').children.at(-1).textContent,/\[blocked\] TV → ads\.example — On a list$/);
assert.equal(tag(get('feed').children.at(-1)).className,'tag tag-block');
socket.onmessage({data:JSON.stringify({domain:'new.example',device:'TV',action:'flag',ts:Date.now()})});
assert.match(get('feed').children.at(-1).textContent,/\[flagged\] TV → new\.example$/);
assert.match(get('feed').children[0].textContent,/\[approved\] TV → example\.org$/);

// Rule changes: a [rule] feed row and a toast that say stored, never enforced.
socket.onmessage({data:JSON.stringify({type:'rule_change',change:'added',id:7,pattern:'device-metrics-us.amazon.com',action:'block',device_mac:'f0:4f:7c:11:22:33',device:'Fire Stick',created_by:'voice'})});
assert.equal(get('feed').children.at(-1).textContent,'[rule] Rule added: block device-metrics-us.amazon.com · Fire Stick · by voice');
assert.equal(tag(get('feed').children.at(-1)).className,'tag tag-rule');
assert.equal(get('notice').textContent,'Rule added: block device-metrics-us.amazon.com · Fire Stick · by voice');
socket.onmessage({data:JSON.stringify({type:'rule_change',change:'removed',id:8,pattern:'a.example',action:'allow',device_mac:null,device:null,created_by:'api'})});
assert.equal(get('notice').textContent,'Rule removed: allow a.example · all devices · by api');
assert.equal(get('requests').textContent,'3','rule changes are not DNS requests');

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
