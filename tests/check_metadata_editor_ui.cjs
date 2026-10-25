const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map();
function node(key){if(!nodes.has(key))nodes.set(key,{innerHTML:'',textContent:'',dataset:{},style:{},open:false,disabled:false,classList:{add(){},remove(){}},addEventListener(){},showModal(){this.open=true},close(){this.open=false},scrollIntoView(){}});return nodes.get(key);}
const document={body:{dataset:{}},documentElement:{classList:{add(){},remove(){}}},querySelector:node,querySelectorAll(){return[]},addEventListener(){}};
const c=vm.createContext({document,window:{addEventListener(){}},console,Date,Intl,URL,structuredClone,setTimeout(){},clearTimeout(){}});
c.testForm={querySelector:node,querySelectorAll(){return[]}};
for(const file of ['app.js','integration.js','roles.js'])vm.runInContext(fs.readFileSync(__dirname+'/../static/'+file,'utf8'),c);
const run=code=>vm.runInContext(code,c);
const original={id:'work',title:'尚公主',kind:'tv',year:2026,season:1,total_episodes:40,finished:true,completed:false,tracking:true,library_episodes:'1-5',sources:[],active_source_id:'',notes:'',tmdb:{id:286171},emby:{state:'matched'}};
(async()=>{
  run('state.role="admin";state.items='+JSON.stringify([original])+';openEditor("work")');
  const html=node('#modal-content').innerHTML;
  for(const name of ['title','year','total_episodes']) assert.match(html,new RegExp('name="'+name+'" disabled'));
  for(const name of ['season','finished']) assert.match(html,new RegExp('name="'+name+'" disabled'));
  assert(html.includes('TMDB 自动更新 · 只读'));
  assert(!html.includes('<select name="kind"'));
  assert.match(html, /name="kind" value="剧集" disabled/);
  // Disabled metadata fields are absent from FormData; saving a note must preserve them.
  run('FormData=class {get(name){return {library_episodes:"1-5",notes:"新的备注"}[name]??null}has(name){return name==="tracking"}};var sentPayload;api=async(path,options)=>{sentPayload=options.body;return {}};loadItems=async()=>{};renderPage=()=>{};toast=()=>{}');
  await run('saveItem({preventDefault(){},target:testForm})');
  const saved=JSON.parse(JSON.stringify(run('sentPayload')));
  for(const field of ['title','kind','year','season','total_episodes','finished']) assert.equal(saved[field],original[field],field);
  assert.equal(saved.notes,'新的备注');
  assert.equal(node('#item-error').textContent,'');
  console.log('Verified TMDB metadata is read-only and note saves preserve disabled metadata values.');
})().catch(error=>{console.error(error);process.exitCode=1});
