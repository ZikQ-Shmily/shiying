const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map();
function node(key){if(!nodes.has(key))nodes.set(key,{innerHTML:'',textContent:'',value:'',hidden:false,dataset:{},style:{},classList:{add(){},remove(){},toggle(){}},addEventListener(){},showModal(){throw Error('users must not be a modal');}});return nodes.get(key);}
const document={documentElement:{classList:{add(){},remove(){}}},body:{dataset:{}},querySelector:node,querySelectorAll(){return[]},addEventListener(){}};
const c=vm.createContext({document,window:{addEventListener(){}},location:{hash:'#overview'},history:{replaceState(){}},Date,Intl,URL,console,setTimeout(){return 0},clearTimeout(){}});
for(const f of ['app.js','integration.js','roles.js'])vm.runInContext(fs.readFileSync(__dirname+'/../static/'+f,'utf8'),c,{filename:f});
const run=s=>vm.runInContext(s,c);
const requester=(user_id,username,active=true)=>({user_id,username,provider:'emby',active,submitted_at:'2026-10-25T01:00:00+08:00'});
const item=(id,requesters)=>({id,title:'作品 '+id,kind:'tv',season:1,status:'updating',library_count:2,library_episodes:'1-2',sources:[],requesters,attention_reasons:[],needs_attention:false,tmdb:null,tracking:true});
const records=[item('first',[requester('emby:server:a','Alice'),requester('emby:server:b','Bob',false)]),item('second',[requester('emby:server:b','Bob')])];
const users=[{id:'a',server_id:'server',username:'Alice',requests:1},{id:'b',server_id:'server',username:'Bob <x>',requests:1,disabled:true}];
(async()=>{
 run('state.role="admin";state.csrf="session-a";state.items='+JSON.stringify(records)+';state.trash=[]');
 for(const view of ['all','due','complete','trash']){
  run('state.view='+JSON.stringify(view)+';renderPage()');
  assert.equal(document.body.dataset.layout,'workspace');
  assert(node('#page-content').innerHTML.includes('resource-panel'));
 }
 run('state.view="all";renderPage()');
 assert(node('#resource-results').innerHTML.includes('role="region"'));
 assert(node('#resource-results').innerHTML.includes('class="table-bottom"'));
 run('state.view="overview";renderPage()');assert.equal(document.body.dataset.layout,'page');
 // A delayed recycle-bin request uses the same fixed frame before and after loading.
 run('var resolveTrash;api=()=>new Promise(resolve=>resolveTrash=resolve);var trashRequest=navigate("trash",false)');
 assert.equal(document.body.dataset.layout,'workspace');
 const trashFrame=node('#page-content').innerHTML;
 assert(trashFrame.includes('resource-panel')&&trashFrame.includes('resource-toolbar'));
 assert(node('#resource-results').innerHTML.includes('正在读取回收站'));
 run('resolveTrash({items:[]})');await run('trashRequest');
 assert.equal(node('#page-content').innerHTML,trashFrame);
 assert(node('#resource-results').innerHTML.includes('回收站是空的'));
 run('api=async()=>{throw new Error("<unavailable>")};var failedTrash=navigate("trash",false)');await run('failedTrash');
 assert.equal(document.body.dataset.layout,'workspace');
 assert(node('#resource-results').innerHTML.includes('回收站读取失败'));
 assert(node('#resource-results').innerHTML.includes('&lt;unavailable&gt;'));
 run('api=()=>new Promise(resolve=>resolveTrash=resolve);var lateTrash=navigate("trash",false)');
 await run('navigate("all",false)');run('resolveTrash({items:[{id:"stale"}]})');await run('lateTrash');
 assert.equal(run('state.view'),'all');assert.equal(run('state.trash.length'),0);
 run('adminUsers.rows='+JSON.stringify(users)+';state.view="users";renderPage()');
 assert.equal(document.body.dataset.layout,'workspace');
 assert(node('#page-content').innerHTML.includes('users-panel'));
 assert(!node('#page-content').innerHTML.includes('modal-body'));
 assert(node('#users-content').innerHTML.includes('3 条提交 · 2 条保留追更'));
 assert(node('#users-content').innerHTML.includes('已取消'));
 run('adminUsers.selected="emby:server:a";renderAdminUsersContent()');
 assert(node('#users-content').innerHTML.includes('Alice 的提交'));
 assert(node('#users-content').innerHTML.includes('作品 first'));
 assert(!node('#users-content').innerHTML.includes('作品 second'));
 run('adminUsers.query="bob";renderAdminUserAccounts()');
 assert(node('#users-account-list').innerHTML.includes('Bob &lt;x&gt;'));
 assert(!node('#users-account-list').innerHTML.includes('Alice'));
 run('adminUsers.selected="emby:server:b";renderAdminUsersContent()');
 assert(node('#users-content').innerHTML.includes('账号已禁用'));
 assert(node('#users-content').innerHTML.includes('Bob &lt;x&gt; 的提交'));
 run('api=async()=>{throw new Error("network unavailable")};var refresh=loadAdminUsers()');await run('refresh');
 assert(node('#users-content').innerHTML.includes('当前保留上次读取结果'));
 assert(node('#users-content').innerHTML.includes('作品 second'));
 // A late response cannot restore admin data after logout or a role change.
 run('resetAdminUsers();var resolveUsers,resolveItems;api=path=>new Promise(resolve=>{if(path==="/api/users")resolveUsers=resolve;else resolveItems=resolve});var pending=loadAdminUsers()');
 run('resetAdminUsers();state.role="user";state.csrf="session-b";state.items=[];resolveUsers({users:'+JSON.stringify(users)+'});resolveItems({items:'+JSON.stringify(records)+'})');await run('pending');
 assert.equal(run('adminUsers.rows'),null);assert.equal(run('state.items.length'),0);
 run('renderUserPortal=()=>{};renderPage()');assert.equal(document.body.dataset.layout,'page');
 await run('navigate("users",false)');assert.equal(run('state.view'),'overview');
 const index=fs.readFileSync(__dirname+'/../static/index.html','utf8');
 assert(index.includes('data-admin-only data-view="users"'));
 assert(!index.includes('data-action="users"'));
 console.log('Verified viewport list modules, independent users route, per-account submissions, cancelled/disabled states, search escaping, cached refresh errors and stale-response/role isolation.');
})().catch(e=>{console.error(e);process.exitCode=1});
