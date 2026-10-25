const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
const nodes=new Map();
function node(key){if(!nodes.has(key))nodes.set(key,{innerHTML:'',textContent:'',value:'',hidden:false,dataset:{},style:{},classList:{add(){},remove(){},toggle(){}},addEventListener(){}});return nodes.get(key);}
let timerDelay;
const document={body:{dataset:{}},querySelector:node,querySelectorAll(){return[]},addEventListener(){}};
const context=vm.createContext({document,window:{addEventListener(){}},Intl,Date,URL,console,setTimeout(fn,delay){timerDelay=delay;return 1;},clearTimeout(){}});
for(const file of ['app.js','integration.js','roles.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../static',file),'utf8'),context);
const run=code=>vm.runInContext(code,context);
(async()=>{
 run('state.role="admin";connection.config={emby_configured:true,emby_url:"http://nas.example:8096",tmdb_configured:true,cms_configured:true,cms_url:"http://nas.example:9526"}');
 run('renderServiceConnections()');let html=node('#connection-services').innerHTML;
 assert(html.includes('待检测')&&!html.includes('已连接'));
 assert(!html.includes('http://')&&!html.includes('https://')&&!html.includes('service-address'));
 assert(html.includes('拾影 → 已授权 CMS → RE0')&&html.includes('拾影 → 我的 CMS → 115'));
 const health={checking:false,services:[{id:'emby',name:'Emby',address:'http://nas.example:8096',configured:true,state:'connected',checked_at:'2026-10-23T02:00:00+08:00',route:'拾影 → Emby'},
 {id:'cms',name:'CloudMediaSync',address:'http://nas.example:9526',configured:true,state:'error',checked_at:'2026-10-23T02:00:00+08:00',message:'<img src=x> RE0 授权失效',route:'拾影 → CMS → RE0'}]};
 run('connection.health='+JSON.stringify(health)+';renderServiceConnections()');html=node('#connection-services').innerHTML;
 assert(html.includes('已连接')&&html.includes('连接异常')&&html.includes('检查于 10/23 02:00'));
 assert(html.includes('连接失败，请在设置中检查')&&!html.includes('<img src=x>'));
 assert(!html.includes('nas.example')&&!html.includes('title='));
 run('connection.health.checking=true;renderServiceConnections();scheduleIntegrationPoll()');html=node('#connection-services').innerHTML;
 assert(html.includes('检测中')&&!html.includes('已连接'));assert.equal(timerDelay,2000);
 run('connection.health.checking=false;connection.healthError="server unreachable";renderServiceConnections()');
 assert(!node('#connection-services').innerHTML.includes('已连接'));
 assert(node('#connection-status-note').textContent.includes('状态读取失败'));
 run('connection.config.re0_via_cms=true;connection.healthError="";renderServiceConnections()');
 html=node('#connection-services').innerHTML;
 assert(!html.includes('RE0 查询端')&&html.includes('CloudMediaSync'));
 const settings=run('integrationSettingsSection()');
 assert(!settings.includes('name="re0_gateway_url"')&&!settings.includes('name="re0_gateway_token"'));
 assert(settings.includes('CMS 连接密钥')&&settings.includes('CMS 自身的账号授权'));
 run('connection.config.re0_via_cms=false');
 run('state.role="user";renderServiceConnections()');assert.equal(node('#connection-services').innerHTML,'');
 // Late admin responses cannot reinsert private addresses after a logout or role switch.
 run('state.role="admin";var resolveHealth;api=()=>new Promise(resolve=>resolveHealth=resolve);var healthPromise=refreshServiceConnections()');
 run('state.role="user";connection.health=null;resolveHealth('+JSON.stringify(health)+')');await run('healthPromise');
 assert.equal(run('connection.health'),null);
 run('state.role="admin";connection.healthError="";connection.pending=false;connection.health={checking:false};scheduleIntegrationPoll()');assert.equal(timerDelay,60000);
 const index=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
 assert(!index.includes('class="topbar"')&&!index.includes('breadcrumb-view')&&!index.includes('today-label'));
 assert(index.includes('sidebar-connections" data-admin-only'));
 assert.equal((index.match(/aria-label="打开导航"/g)||[]).length,1);
 assert(index.indexOf('id="global-sync-label"')<index.indexOf('<main'));
 console.log('Verified actual-status badges, hidden connection addresses and preserved routes, error escaping, check times, checking/failed refresh states, admin-only display, late-response isolation, polling interval and removed top bar/mobile menu.');
})().catch(error=>{console.error(error);process.exitCode=1});
