/* Behaviour checks against the real frontend functions, without third-party packages. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.events = {}; this.style = {}; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(k, v) { this.events[k] = v; }
  append(...kids) { this.children.push(...kids); }
  replaceChildren(...kids) { this.children = kids; }
}
const document = {
  querySelector: () => new Element('stub'),
  createElement: (tag) => new Element(tag),
  createElementNS: (_ns, tag) => new Element(tag),
};
let code = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
code = code.slice(0, code.lastIndexOf('\nboot().catch'));
code += `
globalThis.apiUnderTest = { state, drill, csvCell, yRange, barChart, lineChart, fmt, memberColor };
globalThis.captureFollowUps = () => { followUp = (label, query, dataset) => { globalThis.lastFollowUp = {query, dataset}; }; };
`;
const ctx = { document, console, setInterval(){}, setTimeout(){}, clearInterval(){}, clearTimeout(){}, window:{} };
vm.createContext(ctx); vm.runInContext(code, ctx);
const t = ctx.apiUnderTest;
const modelA = {metrics:[{name:'revenue',label:'Revenue',format:'currency'}],dimensions:[{name:'region',label:'Region',values:['India']},{name:'practice',label:'Practice',values:['Cloud']}]};
const modelB = {metrics:[{name:'hours',label:'Hours',format:'number'}],dimensions:[{name:'department',label:'Department',values:['HR']}]};
t.state.models = {A:modelA,B:modelB};
ctx.captureFollowUps();
t.drill({dataset:'A',model:modelA},{metrics:['revenue'],dimensions:['region'],filters:[]},'region','India');
assert.equal(ctx.lastFollowUp.dataset,'A');
assert.equal(ctx.lastFollowUp.query.dimensions[0],'practice');
let cases = 1;
for(const [values,kind] of [[[-500,100],'currency'],[[-500,-200],'currency'],[[1.2,1.5],'percent'],[[null,undefined],'number']]){
  const [lo,hi]=t.yRange(values,kind); assert(lo < hi); for(const v of values)if(v!=null)assert(lo<=v&&hi>=v);cases++;
}
for(const text of ['=HYPERLINK("bad")','+cmd','@SUM(1)',' -formula']){assert(t.csvCell(text).startsWith('"\''));cases++;}
assert.equal(t.csvCell(-500),'"-500"'); cases++;
assert.equal(t.fmt(123,'currency_EUR'),'€123.00');cases++;
const res={query:{dimensions:['region']},rows:[{region:'India',revenue:-500},{region:'Europe',revenue:100}]};
const chart=t.barChart(res,'revenue',modelA);
assert(chart.children.filter(n=>n.tag==='path').some(n=>/h-/.test(n.attrs.d)));cases++;
const line=t.lineChart({rows:[{period:'2026-01',revenue:10},{period:'2026-02',revenue:null},{period:'2026-03',revenue:20}]},'revenue',modelA);
const path=line.children.find(n=>n.tag==='path');assert.equal((path.attrs.d.match(/M/g)||[]).length,2);cases++;
assert.equal(t.memberColor('region','India',modelA),'#4453b5');cases++;
console.log('Frontend QC: '+cases+' behaviour checks passed.');
