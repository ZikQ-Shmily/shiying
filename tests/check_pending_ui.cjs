const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const path = require('node:path');
const nodes = new Map();
function node(key) {
  if (!nodes.has(key)) nodes.set(key, {innerHTML:'',textContent:'',value:'',hidden:false,dataset:{},style:{},open:false,scrollTop:0,classList:{add(){},remove(){},toggle(){}},addEventListener(){},showModal(){this.open=true;},insertAdjacentHTML(position,html){this.innerHTML+=html;}});
  return nodes.get(key);
}
const document = {documentElement:{classList:{add(){},remove(){}}},body:{dataset:{}},querySelector:node,querySelectorAll(){return[];},addEventListener(){}};
const context = vm.createContext({document,window:{addEventListener(){}},Intl,Date,URL,structuredClone,console,setTimeout(){return 0;},clearTimeout(){}});
for (const file of ['app.js','integration.js','re0.js','roles.js']) vm.runInContext(fs.readFileSync(path.join(__dirname,'../static',file),'utf8'),context,{filename:file});
const run = code => vm.runInContext(code,context);
const read = code => JSON.parse(JSON.stringify(run(code)));
const reason = (code, detail) => ({code,detail});
const base = (id, extra={}) => ({id,title:id,kind:'tv',season:1,total_episodes:4,library_count:2,library_episodes:'1-2',sources:[],active_source_id:'',tracking:true,tmdb:null,status:'updating',next_check:'2020-01-01',due:true,updated_at:'2026-10-23T10:00:00+08:00',attention_reasons:[],needs_attention:false,...extra});
const fixtures = [
  base('manual missing',{status:'missing',missing_episodes:'3-4',needs_attention:true,attention_reasons:[reason('missing','等待入库：3-4')]}),
  base('integrated error',{status:'complete',tmdb:{id:123,overview:'test'},emby:{state:'matched'},needs_attention:true,attention_reasons:[reason('sync_error','RE0：<img src=x>'),reason('source_error','当前来源异常')]}),
  base('paused',{tracking:false,needs_attention:false,attention_reasons:[reason('missing','等待入库：3')]}),
  base('old date only'), base('unmatched',{tmdb:{id:234},emby:{state:'unmatched'},status:'pending',missing_episodes:'1-4'}),
];
run('state.role="admin";state.items='+JSON.stringify(fixtures)+';state.view="due"');
assert.deepEqual(read('filteredItems().map(item=>item.id)'), ['manual missing','integrated error']);
run('updateCounts()'); assert.equal(node('#nav-due').textContent,2);
run('renderPage()');
assert(node('#page-content').innerHTML.includes('所有处理类型'));
assert(!node('#page-content').innerHTML.includes('下次检查'));
let html=node('#resource-results').innerHTML;
assert(html.includes('处理原因')&&html.includes('待补集')&&html.includes('同步失败'));
assert(html.includes('RE0：&lt;img src=x&gt;')&&!html.includes('<img src=x>'));
assert(!html.includes('old date only')&&!html.includes('unmatched')&&!html.includes('paused'));
run('state.status="source_error"'); assert.deepEqual(read('filteredItems().map(item=>item.id)'),['integrated error']);
run('state.status="missing";state.search="manual"'); assert.deepEqual(read('filteredItems().map(item=>item.id)'),['manual missing']);
run('state.kind="movie"'); assert.equal(read('filteredItems().length'),0);
run('state.kind="";state.search="";state.status="";renderOverview()');
html=node('#page-content').innerHTML;
assert(html.includes('2 部待处理')&&html.includes('处理清单'));
assert(html.indexOf('integrated error')<html.indexOf('manual missing'));
assert(!html.includes('到期检查')&&!html.includes('检查清单'));
run('state.view="all";state.status="missing";state.items[2].status="missing"');
assert(read('filteredItems().map(item=>item.id)').includes('paused'));
run('state.status="";renderPage()');
assert(node('#resource-results').innerHTML.includes('最近更新')&&!node('#resource-results').innerHTML.includes('2020'));
run('openDetail("integrated error")');
html=node('#modal-content').innerHTML;
assert(html.includes('处理建议')&&html.includes('检查连接与授权')&&html.includes('原复查日期'));
assert(!html.includes('id="check-form"')&&!html.includes('已到期'));
run('openDetail("paused")');assert(node('#modal-content').innerHTML.includes('追更已暂停，不计入待处理'));
run('openEditor("manual missing")');html=node('#modal-content').innerHTML;
assert(!html.includes('name="next_check"')&&!html.includes('下次检查日期'));
run('state.items=[];state.view="due";renderResults()');assert(node('#resource-results').innerHTML.includes('当前没有待处理事项'));
run('state.role="user";state.view="overview";state.items=[];renderPage()');
assert(!node('#page-content').innerHTML.includes('处理建议')&&!node('#page-content').innerHTML.includes('处理原因'));
// The overview keeps every pending title in its scroll region and opens activity separately.
const many=Array.from({length:7},(_,i)=>base('pending '+i,{status:'missing',needs_attention:true,attention_reasons:[reason('missing','等待入库：3-4')],events:[{at:'2026-10-24T10:00:00+08:00',action:'requested',detail:'<img src=x>'}]}));
run('state.role="admin";state.items='+JSON.stringify(many)+';renderOverview()');
html=node('#page-content').innerHTML;
assert.equal((html.match(/class="task-row"/g)||[]).length,7);
assert(html.includes('overview-task-scroll')&&html.includes('tabindex="0"'));
assert(html.includes('data-action="recent-activity"')&&html.includes('aria-haspopup="dialog"'));
assert(!html.includes('admin-activity-list'));
run('openRecentActivity()');
assert(node('#modal-content').innerHTML.includes('admin-activity-list'));
assert(node('#modal-content').innerHTML.includes('&lt;img src=x&gt;'));
run('state.role="user"');node('#modal-content').innerHTML='user';run('openRecentActivity()');
assert.equal(node('#modal-content').innerHTML,'user');
console.log('Verified pending count, reason filters, paused/unverified/date exclusion, completed sync errors, priority order, both table variants, escaped errors, detail guidance, legacy review history and normal user views.');
// A same-day schedule is displayed independently of both past schedules and the actual library.
const scheduled=base('today schedule',{tmdb:{id:300146},emby:{state:'matched'},aired_count:15,aired_episodes:'1-15',airing_today_count:1,airing_today_episodes:'16',not_aired_count:14,library_count:14,library_episodes:'1-14',missing_episodes:'15',request_active:true});
run('state.role="admin";state.view="all";state.items='+JSON.stringify([scheduled]));
html=run('embyComparison(state.items[0])');
assert(html.includes('TMDB 已播 · 按排期')&&html.includes('今日排期 1 集')&&html.includes('第 16 集'));
assert(html.includes('后续 / 日期待定')&&html.includes('不直接计入缺集'));
assert(run('integratedTableRow(state.items[0])').includes('今日排期 1 集'));
run('state.role="user";openUserDetail("today schedule")');
html=node('#modal-content').innerHTML;
assert(html.includes('今日排期 1 集')&&html.includes('等待补齐：15。')&&!html.includes('等待补齐：15-16'));
assert(run('userCard(state.items[0])').includes('今日排期'));
assert(run('userHistoryRow(state.items[0])').includes('今日排期 1 集'));
assert(!run('airingScheduleNotice({tmdb:null})'));
assert(!run('airingScheduleNotice({tmdb:{},airing_today_count:0})').includes('<strong>'));
assert(run('airingScheduleNotice({tmdb:{},kind:"movie",airing_today_count:1,airing_today_episodes:"1"})').includes('今日排期 1 部'));
console.log('Verified same-day schedule display in admin/user detail, resource table, request cards/history, movie units and no false 15-16 missing label.');

// A successful empty library is an ordinary pending title; metadata ambiguity remains distinct.
const absent=base('not in library',{tmdb:{id:286171},emby:{state:'absent'},status:'pending',library_count:0,library_episodes:'',aired_count:28,missing_episodes:'1-28',request_active:true});
const premiere={...absent,id:'premiere',status:'premiering',kind:'tv',aired_count:0,airing_today_count:4};
run('state.items='+JSON.stringify([absent,premiere]));
assert.equal(run('hasVerifiedLibrary(state.items[0])'),true);
assert(run('statusBadge(state.items[0])').includes('追更中，待补集'));
assert(run('embyComparison(state.items[0])').includes('库中未找到对应作品'));
assert(!run('integratedTableRow(state.items[0])').includes('入库待核对'));
assert(run('statusBadge(state.items[1])').includes('今日开播'));
assert.equal(run('requestStatus(state.items[1])[0]'),'今日开播');
assert.equal(run('requestStatus(state.items[0])[0]'),'等待入库');
assert.equal(run('premiereLabel({kind:"movie"})'),'今日上映');
let choices=run('seasonChoices([{season_number:1,name:"第一季",episode_count:40,air_date:"2026-10-23"}])');
assert(!choices.includes('<select')&&choices.includes('type="radio"')&&choices.includes('name="season"')&&choices.includes(' checked'));
assert(choices.includes('40 集')&&choices.includes('2026-10-23 首播')&&choices.includes('已选中'));
choices=run('seasonChoices([{season_number:1},{season_number:2,name:"<img src=x>"}])');
assert.equal((choices.match(/ checked/g)||[]).length,1);
assert(choices.includes('value="2"')&&!choices.includes('<img src=x>'));
console.log('Verified season cards, premiere/movie labels, absent versus ambiguous library and waiting-for-import user state.');

// Active tracking includes unfinished imports/missing episodes; the overview and destination agree.
const trackingStatuses=['updating','missing','pending','waiting_source','wanted','upcoming','premiering'];
const trackingRecords=[...trackingStatuses.map(status=>base(status,{status})),base('finished',{status:'complete'}),base('paused',{status:'missing',tracking:false}),base('removed',{status:'missing',deleted_at:'2026-10-25T00:00:00+08:00'})];
run('state.role="admin";state.view="overview";state.items='+JSON.stringify(trackingRecords)+';renderOverview()');
html=node('#page-content').innerHTML;
assert.match(html,/data-target="updating"[^>]*>[\s\S]*?class="stat-value">7<\/div>/);
run('state.view="all";state.status="updating_group";state.kind="";state.search="";renderPage()');
assert.deepEqual(read('filteredItems().map(item=>item.id)'),trackingStatuses);
assert(node('#page-content').innerHTML.includes('追更中'));
assert.deepEqual(read('resourceFilterOptions("status").map(row=>row[1])'),['所有状态','追更中','追更中，待补集','追更中，已补集','今日开播','尚未播出','待找资源','已完成','来源异常']);
assert.equal(run('resourceStatus({status:"pending"})'),'missing');
assert.equal(run('resourceStatus({status:"waiting_source"})'),'missing');
assert.equal(run('resourceStatus({status:"updating",source_broken:true})'),'broken');
console.log('Verified active tracking includes missing/pending titles and excludes completed, paused and removed records, with matching overview count and list.');

const wantedRecord=base("unbound",{status:"wanted",library_count:0,request_active:true,needs_attention:true,attention_reasons:[reason("source_missing","尚未绑定资源来源")]});
const caughtUpRecord=base("caught up",{status:"updating",tmdb:{id:123},emby:{state:"matched"},library_count:2,missing_episodes:""});
run("state.items="+JSON.stringify([wantedRecord,caughtUpRecord])+";state.view=\"all\";state.status=\"wanted\"");
assert.deepEqual(read("filteredItems().map(item=>item.id)"),["unbound"]);
assert.equal(run("requestStatus(state.items[0])[0]"),"待找资源");
assert(run("attentionBadges(state.items[0])").includes("待找资源"));
run("state.status=\"updating\"");
assert.deepEqual(read("filteredItems().map(item=>item.id)"),["caught up"]);
assert(run("statusBadge(state.items[1])").includes("追更中，已补集"));
run("state.items[1].emby.state=\"unmatched\"");
assert(!run("statusBadge(state.items[1])").includes("已补集"));
console.log("Verified unbound titles use waiting-for-source filters/guidance, and caught-up status requires verified library progress.");
