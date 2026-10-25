const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map();let handler,poll;
function node(k){if(!nodes.has(k))nodes.set(k,{innerHTML:'',addEventListener(){},classList:{add(){},remove(){},toggle(){}},style:{},open:true,dataset:{itemId:'item'},insertAdjacentHTML(_,s){this.innerHTML+=s;}});return nodes.get(k);}
const document={body:{dataset:{}},querySelector:node,querySelectorAll(){return[]},addEventListener(name,fn){if(name==='click')handler=fn;}};
const c=vm.createContext({document,window:{addEventListener(){}},console,Date,Intl,URL,setTimeout(fn){poll=fn;return 1;},clearTimeout(){},openEditor(){},connection:{config:{re0_via_cms:true}}});
for(const f of ['app.js','re0.js'])vm.runInContext(fs.readFileSync(__dirname+'/../static/'+f,'utf8'),c);
const run=s=>vm.runInContext(s,c);
run('isAdministrator=()=>state.role==="admin";stamp=value=>value||"";state.role="admin";var example={id:"item",tracking:true,re0:{},cms_delivery:{state:"submitted"},cms_run:{state:"running",message:"<script>x</script>"}}');
let html=run('cmsDeliverySection(example)');
assert(html.includes('执行中…')&&html.includes(' disabled')&&html.includes('&lt;script&gt;'));
assert(!html.includes('<script>')&&!html.includes('已入库'));
run('example.cms_run.state="submitted"');html=run('cmsRunSection(example)');
assert(html.includes('已提交 CMS 转存')&&html.includes('立即入库一次')&&!html.includes(' disabled'));
assert(html.includes('Emby 扫描后才算实际入库'));
run('example.tracking=false');assert(!run('cmsRunSection(example)').includes('data-re0="run"'));
run('example.tracking=true;example.cms_run.state="queued";scheduleCmsRunPoll(example);state.role="user";api=()=>{throw new Error("late admin request")};');
(async()=>{await poll();console.log('Verified immediate-run controls, pending disabled state, escaped messages, honest transfer status, pause hiding and role isolation.');})().catch(e=>{console.error(e);process.exitCode=1;});
