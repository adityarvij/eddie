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
const context={document:{getElementById:get,createElement:()=>new Element(),addEventListener(){},activeElement:{tagName:'BODY'}},window:{},location:{search:'',hostname:'localhost'},URLSearchParams,Date,console,
  setTimeout(){return 1;},clearTimeout(){},fetch(){},EventSource:class{},WebSocket:class{constructor(){socket=this;}}};
vm.runInNewContext(fs.readFileSync('frontend/app.js','utf8'),context);
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
console.log('Frontend query/verdict event checks passed');
