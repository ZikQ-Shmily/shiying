const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map();
function node(key){if(!nodes.has(key))nodes.set(key,{innerHTML:'',textContent:'',value:'',dataset:{},disabled:false,checked:false,classList:{add(){},remove(){},toggle(){}},querySelector:node,querySelectorAll(){return[]},reportValidity(){return true},addEventListener(){}});return nodes.get(key);}
const document={documentElement:{classList:{add(){},remove(){}}},body:{dataset:{}},querySelector:node,querySelectorAll(){return[]},addEventListener(){}};
const context=vm.createContext({document,window:{addEventListener(){},location:{origin:'https://sy.example.com'}},Date,Intl,URL,console,setTimeout(){},clearTimeout(){},FormData:class{constructor(form){this.fields=form.fields}get(key){return this.fields[key]??null}has(key){return !!this.fields[key]}}});
for(const file of ['app.js','integration.js','roles.js','notifications.js'])vm.runInContext(fs.readFileSync(__dirname+'/../static/'+file,'utf8'),context,{filename:file});
const run=code=>vm.runInContext(code,context);
(async()=>{
 const index=fs.readFileSync(__dirname+'/../static/index.html','utf8');
 assert.match(index,/<script defer src="\/static\/notifications.js\?v=/);
 run('state.role="user";emailNotifications.config={to_email:"private@example.com"}');
 assert.equal(run('emailNotificationSettingsSection()'),'');
 run('state.role="admin";emailNotifications.error="<failure>"');
 assert(run('emailNotificationSettingsSection()').includes('&lt;failure&gt;'));
 run('emailNotifications.error="";emailNotifications.config={enabled:true,has_password:true,to_email:"owner@example.com",smtp_host:"<host>",last_error:"<mail-error>",pending_count:2}');
 const html=run('emailNotificationSettingsSection()');
 assert(html.includes('新追更邮件提醒')&&html.includes('发送测试邮件'));
 assert(html.includes('&lt;host&gt;')&&html.includes('&lt;mail-error&gt;'));
 assert.match(html,/name="smtp_password"[^>]*value=""/);
 const form=node('#email-notification-form');form.fields={enabled:true,to_email:'owner@example.com',from_email:'sender@example.com',smtp_username:'sender@example.com',smtp_host:'smtp.example.com',smtp_port:'465',security:'ssl',smtp_password:'entered-secret'};
 context.form=form;
 run('var calls=[];api=async(path,options)=>{calls.push({path,options});return {enabled:true,has_password:true,pending_count:0,ok:true,message:"测试邮件已交给发信服务器"}}');
 await run('emailSettingsAction(form,"test")');
 assert.deepEqual(JSON.parse(JSON.stringify(run('calls.map(call=>call.path)'))),['/api/notifications','/api/notifications/test']);
 assert.equal(run('calls[0].options.body.smtp_password'),'entered-secret');
 assert.equal(node('[name="smtp_password"]').value,'');
 assert.equal(node('#email-test-result').textContent,'测试邮件已交给发信服务器');
 // A second click cannot dispatch another save or test while the first is pending.
 run('var resolveSave;calls=[];api=(path,options)=>{calls.push({path,options});return new Promise(resolve=>resolveSave=resolve)};var saving=emailSettingsAction(form,"save")');
 await run('emailSettingsAction(form,"save")');assert.equal(run('calls.length'),1);
 run('resolveSave({enabled:true,has_password:true})');await run('saving');
 assert.equal(form.dataset.mailBusy,undefined);
 // A stale response does not write private admin settings into a new session.
 run('emailNotifications.config=null;var resolveSettings;api=()=>new Promise(resolve=>resolveSettings=resolve);var loading=refreshEmailSettings();state.role="user";resolveSettings({to_email:"private@example.com"})');await run('loading');
 assert.equal(run('emailNotifications.config'),null);
 console.log('Verified email settings loading, role isolation, escaped errors, empty password display, save-before-test, duplicate-click protection and stale-response isolation.');
})().catch(error=>{console.error(error);process.exitCode=1});
