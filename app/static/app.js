'use strict';
let token = sessionStorage.getItem('qp_token') || '';
let snapshot = null;
let activePage = 'overview';
let modalKind = '';
let modalId = null;
let toastTimer;
const $ = (id) => document.getElementById(id);
const escapeHtml = v => String(v ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = n => Number(n || 0).toLocaleString('en-US');
const pct = n => n == null ? '—' : n+'%';
const duration = s => s == null ? '—' : (Math.floor(s/60) ? Math.floor(s/60)+'m ' : '')+Math.floor(s%60)+'s';
const timeFmt = v => v ? new Date(v*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',hour12:true}) : '—';
const relative = ts => !ts ? 'Never' : Math.max(0, Math.round((Date.now()/1000-ts)/60)) < 1 ? 'Just now' : Math.round((Date.now()/1000-ts)/60)+' min ago';
const statuses = {available:['Available','green'],busy:['On call','blue'],acw:['After-call work','amber'],away:['Away','amber'],offline:['Offline','gray'],queued:['Waiting','amber'],answered:['In progress','blue'],abandoned:['Abandoned','red'],completed:['Completed','green']};
const badge = (s) => {const [label,kind] = statuses[s] || [s,'gray'];return `<span class="tag ${kind}">${escapeHtml(label)}</span>`;};
const emptyRow = (cols,msg='No records yet') => `<tr><td class="empty" colspan="${cols}">${escapeHtml(msg)}</td></tr>`;
const queueBy = id => snapshot?.queues.find(x=>x.id===id);
const agentBy = id => snapshot?.agents.find(x=>x.id===id);
async function api(path,opts={}){
  const headers = {...opts.headers};
  if(token) headers.Authorization = 'Bearer '+token;
  if(opts.body) headers['Content-Type'] = 'application/json';
  const response = await fetch('/api'+path, {...opts,headers,cache:'no-store'});
  if(response.status===401 && path!=='/login') {signout();throw new Error('Session expired');}
  if(!response.ok){let detail='Request failed';try{const result=await response.json();detail=typeof result.detail==='string'?result.detail:JSON.stringify(result.detail);}catch{}throw new Error(detail);}
  return response.json();
}
function flash(message){$('toast').textContent=message;$('toast').style.display='block';clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').style.display='none',3100);}
function signout(){token='';sessionStorage.removeItem('qp_token');$('login').hidden=false;$('app').hidden=true;$('password').value='';}
function signin(){ $('login').hidden=true;$('app').hidden=false; refresh(); }
$('loginForm').addEventListener('submit',async e=>{e.preventDefault();$('loginError').textContent='';const btn=e.target.querySelector('button');btn.disabled=true;try{const data=await api('/login',{method:'POST',body:JSON.stringify({username:$('username').value,password:$('password').value})});token=data.token;sessionStorage.setItem('qp_token',token);signin();}catch(err){$('loginError').textContent=err.message;}finally{btn.disabled=false;}});
$('logout').addEventListener('click',signout);
if(token) signin();
setInterval(()=>{if(token)refresh(true);},4000);
setInterval(()=>{$('clock').textContent=new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});},1000);
async function refresh(quiet=false){if(!token)return;try{snapshot=await api('/dashboard');render();$('liveIndicator').innerHTML='<span class="pulse"></span> Dashboard refreshing';}catch(err){if(!quiet)flash(err.message);$('liveIndicator').textContent='⚠ Refresh failed';}}
$('refreshBtn').onclick=()=>refresh();
function navigate(page){activePage=page;document.querySelectorAll('.page').forEach(el=>el.hidden=el.id!=='page-'+page);document.querySelectorAll('.nav-link').forEach(el=>el.classList.toggle('active',el.dataset.page===page));$('crumb').textContent={overview:'Overview',calls:'Live calls',agents:'Agent management',reports:'Reports',aura:'Avaya Aura control',settings:'Configuration'}[page];document.querySelector('.sidebar').classList.remove('open');if(snapshot)render();if(page==='aura')loadAura();}
document.querySelectorAll('.nav-link').forEach(el=>el.onclick=()=>navigate(el.dataset.page));
document.querySelectorAll('[data-goto]').forEach(el=>el.onclick=()=>navigate(el.dataset.goto));
$('mobileMenu').onclick=()=>document.querySelector('.sidebar').classList.toggle('open');
$('callFilter').onchange=()=>renderCalls();
function render(){if(!snapshot)return;const d=snapshot;
  $('demoBanner').style.display=d.integration.mode==='DEMO'?'block':'none';
  document.querySelectorAll('.demo-action').forEach(b=>b.style.display=d.integration.mode==='DEMO'?'':'none');
  $('sideConnection').textContent=d.integration.connected?'● CTI connected':'● CTI connection not verified';
  $('sideConnection').style.color=d.integration.connected?'#64ddb2':'#f6c47b';
  $('provider').textContent=d.integration.last_source||'No events';
  $('lastEvent').textContent=relative(d.integration.last_event_at);
  document.querySelector('#page-settings .diag-row .warning').textContent=d.integration.connected?'Yes — bridge-reported':'No';
  $('appMode').textContent=d.integration.mode;
  $('statGrid').innerHTML=[
    ['Waiting in queues',num(d.totals.waiting),'◷','Currently waiting across all queues'],
    ['Answered today',num(d.totals.answered),'↗','Completed connections included'],
    ['Service level',pct(d.totals.sla_percent),'◉','Queue SLA thresholds · today'],
    ['Available agents',num(d.totals.available_agents),'♙',`Out of ${num(d.totals.total_agents)} registered agents`],
  ].map(x=>`<div class="stat-card"><div class="stat-top"><div class="stat-name">${x[0]}</div><div class="stat-icon">${x[2]}</div></div><div class="stat-value">${x[1]}</div><div class="stat-sub">${x[3]}</div></div>`).join('');
  drawChart(d.daily);drawDonut(d.agents);renderQueues();renderCalls();renderAgents();renderReports();renderSettings();
}
function health(q){if(!q.enabled)return ['Inactive','gray'];if(q.waiting>3||q.longest_wait_seconds>q.sla_seconds*3)return ['Needs attention','red'];if(q.waiting>0&&q.available_agents===0)return ['No available agents','amber'];if(q.sla_percent!=null&&q.sla_percent<q.target_percent)return ['Below target','amber'];return ['Healthy','green'];}
function renderQueues(){const qs=snapshot.queues;$('queueRows').innerHTML=qs.length?qs.map((q,i)=>{const h=health(q);return `<tr><td><div class="queue-name"><div class="queue-badge">${escapeHtml(q.name[0]||'Q')}</div><div><div class="table-strong">${escapeHtml(q.name)}</div><div class="table-sub">${escapeHtml(q.skill||'Unassigned skill')}</div></div></div></td><td class="table-strong">${num(q.waiting)}</td><td>${duration(q.longest_wait_seconds)}</td><td>${num(q.available_agents)}</td><td>${num(q.answered)}</td><td><div class="sla">${pct(q.sla_percent)} <span class="table-sub">/ ${q.target_percent}% target</span><div class="progress-track"><div class="progress-fill ${q.sla_percent!=null&&q.sla_percent<q.target_percent?'warn':''}" style="width:${q.sla_percent||0}%"></div></div></div></td><td><span class="tag ${h[1]}">${h[0]}</span></td></tr>`;}).join(''):emptyRow(7,'No queues configured. Add a queue in Configuration.');}
function drawChart(rows){const dates=Array.from({length:7},(_,i)=>{const date=new Date(Date.now()-(6-i)*86400*1000);return date.toISOString().slice(0,10)});
 const points=dates.map(k=>({key:k,answered:rows.find(r=>r.utc_day===k)?.answered||0,abandoned:rows.find(r=>r.utc_day===k)?.abandoned||0}));
 const width=680,height=200,left=30,right=12,top=15,bottom=27,max=Math.max(4,...points.map(x=>x.answered+x.abandoned));const w=width-left-right,h=height-top-bottom;
 let s=`<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-label="Seven-day traffic chart" role="img">`;
 for(let i=0;i<=4;i++){const y=top+h-i*h/4;const n=Math.ceil(max*i/4);s+=`<line x1="${left}" y1="${y}" x2="${width-right}" y2="${y}" stroke="#2c354a" stroke-dasharray="3 5"/><text x="${left-6}" y="${y+3}" fill="#74829e" font-size="10" text-anchor="end">${n}</text>`;}
 const barW=Math.min(25,w/20),group=w/7;
 points.forEach((p,i)=>{const x=left+group*(i+.5);const blue=p.answered/max*h;const amber=p.abandoned/max*h;
  s+=`<rect x="${x-barW-2}" y="${top+h-blue}" width="${barW}" height="${blue}" rx="3" fill="#997df6"/><rect x="${x+2}" y="${top+h-amber}" width="${barW}" height="${amber}" rx="3" fill="#e0ae6c"/><text x="${x}" y="${height-6}" text-anchor="middle" font-size="10" fill="#8693ad">${new Date(p.key+'T00:00:00Z').toLocaleDateString('en-US',{weekday:'short',timeZone:'UTC'})}</text>`;
 });$('trafficChart').innerHTML=s+'</svg>';
}
function drawDonut(agents){const names=['available','busy','acw','away','offline'];const colors=['#62d5ad','#7a9fe1','#e8b66d','#cf8e77','#394760'];let sum=agents.length,cursor=0;
 const portions=names.map((s,i)=>{const n=agents.filter(a=>a.status===s).length;const start=cursor;cursor+=sum?n/sum*100:0;return {name:s,n,start,end:cursor,color:colors[i]};});
 $('donut').style.background=sum?'conic-gradient('+portions.filter(p=>p.n).map(p=>`${p.color} ${p.start}% ${p.end}%`).join(',')+')':'#33415b';
 $('donut').querySelector('strong').textContent=sum;
 $('statusLegend').innerHTML=portions.map(p=>`<div class="legend-row"><i class="legend-dot" style="background:${p.color}"></i><span>${statuses[p.name][0]}</span><b>${p.n}</b></div>`).join('');
}
function renderCalls(){const d=snapshot,filter=$('callFilter').value;let calls=d.calls.filter(c=>filter==='all'||c.status===filter);
 $('callMiniStats').innerHTML=[['Waiting',d.totals.waiting],['In conversation',d.totals.active_calls],['Answered today',d.totals.answered],['Abandoned today',d.totals.abandoned]].map(([l,n])=>`<div class="mini-stat"><p>${l}</p><strong>${num(n)}</strong></div>`).join('');
 $('callRows').innerHTML=calls.length?calls.map(c=>`<tr><td class="table-strong">${escapeHtml(c.external_id)}</td><td>${escapeHtml(c.caller)}</td><td>${escapeHtml(queueBy(c.queue_id)?.name||c.queue_id)}</td><td>${badge(c.status)}</td><td>${timeFmt(c.arrived_at)}</td><td>${duration(c.wait_seconds)}</td><td>${escapeHtml(agentBy(c.agent_id)?.name||'—')}</td></tr>`).join(''):emptyRow(7,'No matching call events');}
function renderAgents(){const a=snapshot.agents;$('agentMiniStats').innerHTML=[['Total agents',a.length],['Available',a.filter(x=>x.status==='available').length],['On call',a.filter(x=>x.status==='busy').length],['Unavailable',a.filter(x=>['away','offline','acw'].includes(x.status)).length]].map(([l,n])=>`<div class="mini-stat"><p>${l}</p><strong>${num(n)}</strong></div>`).join('');
 $('agentRows').innerHTML=a.length?a.map(x=>`<tr><td><div class="table-strong">${escapeHtml(x.name)}</div><div class="table-sub">${escapeHtml(x.id)}</div></td><td>${escapeHtml(x.extension||'—')}</td><td>${escapeHtml(queueBy(x.queue_id)?.name||'Unassigned')}</td><td>${badge(x.status)}</td><td>${timeFmt(x.last_change)}</td><td><button class="small-button" data-status="${escapeHtml(x.id)}">Edit</button></td></tr>`).join(''):emptyRow(6,'No agents registered');
 document.querySelectorAll('[data-status]').forEach(x=>x.onclick=()=>statusModal(x.dataset.status));}
function renderReports(){const qs=snapshot.queues,d=snapshot.totals;
 $('reportMiniStats').innerHTML=[['Answered today',d.answered],['Abandoned today',d.abandoned],['Service level',pct(d.sla_percent)],['Total queues',d.total_queues]].map(([l,n])=>`<div class="mini-stat"><p>${l}</p><strong>${n}</strong></div>`).join('');
 $('reportRows').innerHTML=qs.length?qs.map(q=>`<tr><td class="table-strong">${escapeHtml(q.name)}</td><td>${q.answered}</td><td>${q.abandoned}</td><td>${duration(q.average_wait_seconds)}</td><td>${pct(q.sla_percent)}</td><td>${q.target_percent}% in ${q.sla_seconds}s</td></tr>`).join(''):emptyRow(6);}
function renderSettings(){$('queueSettings').innerHTML=snapshot.queues.length?snapshot.queues.map(q=>`<div class="queue-setting-row"><div><strong>${escapeHtml(q.name)}</strong><p>${escapeHtml(q.id)} · ${q.sla_seconds}s SLA · ${q.target_percent}% target${q.enabled?'':' · Disabled'}</p></div><button class="small-button" data-edit-queue="${escapeHtml(q.id)}">Edit</button></div>`).join(''):'<p class="small-note">No queues configured.</p>';
 document.querySelectorAll('[data-edit-queue]').forEach(b=>b.onclick=()=>queueModal(b.dataset.editQueue));}
async function demoStep(){try{const result=await api('/demo/step',{method:'POST'});flash('Demo event: '+result.outcome);refresh();}catch(e){flash(e.message)}}
$('seedDemoBtn').onclick=async()=>{try{const r=await api('/demo/seed',{method:'POST'});flash(r.seeded?'Demo data loaded':'Queues already exist');refresh();}catch(e){flash(e.message)}};
$('demoStepTop').onclick=demoStep;$('demoStepCalls').onclick=demoStep;
$('exportBtn').onclick=async()=>{try{const response=await fetch('/api/reports/calls.csv',{headers:{Authorization:'Bearer '+token}});if(!response.ok)throw new Error('Export failed');const blob=await response.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='call-queue-report.csv';a.click();URL.revokeObjectURL(url);}catch(err){flash(err.message)}};
const field = (name,label,value='',type='text')=>`<div class="field"><label for="fld_${name}">${escapeHtml(label)}</label><input id="fld_${name}" name="${name}" type="${type}" value="${escapeHtml(value)}" ${type==='number'?'min="1"':''} required></div>`;
const selectField = (name,label,items,value)=>`<div class="field"><label for="fld_${name}">${escapeHtml(label)}</label><select id="fld_${name}" name="${name}">${items.map(x=>`<option value="${escapeHtml(x[0])}" ${x[0]===value?'selected':''}>${escapeHtml(x[1])}</option>`).join('')}</select></div>`;
function openModal(title,body,kind,id=null){modalKind=kind;modalId=id;$('modalTitle').textContent=title;$('modalFields').innerHTML=body;$('modalError').textContent='';$('modal').showModal();}
function queueModal(id=null){const q=id?queueBy(id):{name:'',skill:'',sla_seconds:20,target_percent:80,enabled:1};openModal(id?'Edit queue':'Create queue',field('name','Queue name',q.name)+field('skill','Avaya skill / group label',q.skill)+field('sla_seconds','SLA threshold (seconds)',q.sla_seconds,'number')+field('target_percent','SLA target (%)',q.target_percent,'number')+selectField('enabled','Enabled',[['true','Enabled'],['false','Disabled']],q.enabled?'true':'false'),'queue',id);}
function agentModal(){openModal('Add agent',field('name','Full name')+field('extension','Extension number')+selectField('queue_id','Assigned queue',[['','Unassigned'],...snapshot.queues.map(q=>[q.id,q.name])],'')+selectField('status','Initial local status',Object.entries(statuses).filter(([s])=>['available','busy','acw','away','offline'].includes(s)).map(([s,val])=>[s,val[0]]),'offline'),'agent');}
function statusModal(id){const a=agentBy(id);if(!a)return;openModal('Edit local agent status',`<p class="small-note">This does not control Avaya. CTI events may overwrite your local edit.</p>`+selectField('status',a.name,[['available','Available'],['busy','On call'],['acw','After-call work'],['away','Away'],['offline','Offline']],a.status),'status',id);}
$('newQueueBtn').onclick=()=>queueModal();$('newAgentBtn').onclick=()=>agentModal();
$('closeModal').onclick=()=> $('modal').close();$('cancelModal').onclick=()=> $('modal').close();
$('modalForm').onsubmit=async e=>{e.preventDefault();$('modalError').textContent='';const form=new FormData(e.target);const data=Object.fromEntries(form.entries());
 try{if(modalKind==='queue'){data.sla_seconds=Number(data.sla_seconds);data.target_percent=Number(data.target_percent);data.enabled=data.enabled==='true';await api(modalId?'/queues/'+modalId:'/queues',{method:modalId?'PUT':'POST',body:JSON.stringify(data)});}else if(modalKind==='agent'){data.queue_id=data.queue_id||null;await api('/agents',{method:'POST',body:JSON.stringify(data)});}else if(modalKind==='status'){await api('/agents/'+modalId+'/status',{method:'PATCH',body:JSON.stringify(data)});} $('modal').close();flash('Saved successfully');refresh();}catch(err){$('modalError').textContent=err.message;}};


// Avaya Aura operations: strictly mapped resources and fail-closed command approvals.
let auraData=null;
async function loadAura(){
  if(!token || activePage!=='aura')return;
  try{
    const [bindings,readiness,commandResult,planResult] = await Promise.all([
      api('/aura/bindings'),api('/aura/readiness'),api('/aura/commands'),api('/aura/routing-plans')]);
    auraData={bindings,readiness,commands:commandResult.commands,plans:planResult.plans};renderAura();
  }catch(e){flash('Avaya integration: '+e.message);}
}
function renderAura(){
  if(!auraData)return;
  const {bindings,readiness,commands,plans}=auraData;
  $('auraReadiness').innerHTML=[
    ['Mapped queues',readiness.queues_mapped],['Mapped agents',readiness.agents_mapped],
    ['AES bridge',readiness.bridge_reported_connected?'Reported online':'Not verified'],
    ['Call control',readiness.control_enabled?'Enabled':'Disabled']
  ].map(([a,b])=>`<div class="mini-stat"><p>${escapeHtml(a)}</p><strong>${escapeHtml(b)}</strong></div>`).join('');
  $('auraQueueBindings').innerHTML=bindings.queues.length?bindings.queues.map(q=>`<div class="queue-setting-row"><div><strong>${escapeHtml(q.name)}</strong>
     <p>VDN: ${escapeHtml(q.vdn||'not set')} · Skill: ${escapeHtml(q.skill_number||'not set')} · Vector: ${escapeHtml(q.vector_number||'not set')}</p></div>
     <button class="small-button" data-bind-q="${escapeHtml(q.id)}">Map</button></div>`).join(''):'<p class="small-note">Create QueuePilot queues first.</p>';
  $('auraAgentBindings').innerHTML=bindings.agents.length?bindings.agents.map(a=>`<div class="queue-setting-row"><div><strong>${escapeHtml(a.name)}</strong>
     <p>Station: ${escapeHtml(a.station||'not set')} · Agent login: ${escapeHtml(a.login_id||'not set')}</p></div>
     <button class="small-button" data-bind-a="${escapeHtml(a.id)}">Map</button></div>`).join(''):'<p class="small-note">Create agents first.</p>';
  $('auraStation').innerHTML=bindings.agents.filter(a=>a.station).map(a=>`<option value="${escapeHtml(a.station)}">${escapeHtml(a.name)} (${escapeHtml(a.station)})</option>`).join('');
  $('auraPlanQueue').innerHTML=bindings.queues.map(q=>`<option value="${escapeHtml(q.id)}">${escapeHtml(q.name)}</option>`).join('');
  $('auraCommands').innerHTML=commands.length?commands.map(c=>`<tr><td class="table-strong">${escapeHtml(c.id.slice(-12))}</td>
      <td>${escapeHtml(c.action)}</td><td>${escapeHtml(c.station)}</td><td>${escapeHtml(c.destination||'—')}</td><td>${escapeHtml(c.status)}</td>
      <td>${escapeHtml(c.result_code||'—')}</td><td>${c.status==='pending_approval'?`<button class="small-button" data-aura-approve="${escapeHtml(c.id)}" ${readiness.control_enabled?'':'disabled'}>Approve</button>`:''}
      ${['pending_approval','queued'].includes(c.status)?`<button class="small-button" data-aura-cancel="${escapeHtml(c.id)}">Cancel</button>`:''}</td></tr>`).join(''):emptyRow(7,'No requests yet');
  $('auraPlans').innerHTML=plans.length?plans.map(p=>`<div class="queue-setting-row"><div><strong>${escapeHtml(p.change_type)} · ${escapeHtml(p.status)}</strong>
      <p>${escapeHtml(p.desired)} — ${escapeHtml(p.reason)}</p></div></div>`).join(''):'<p class="small-note">No routing proposals created.</p>';
  document.querySelectorAll('[data-bind-q]').forEach(b=>b.onclick=()=>{
    const q=bindings.queues.find(x=>x.id===b.dataset.bindQ);
    openModal('Avaya queue mapping',field('vdn','VDN',q.vdn||'')+field('skill_number','Skill / hunt group number',q.skill_number||'')+
      field('vector_number','Vector number (optional)',q.vector_number||''),'auraQueueBinding',q.id);
    $('modalFields').querySelectorAll('input').forEach(x=>x.required=false);
  });
  document.querySelectorAll('[data-bind-a]').forEach(b=>b.onclick=()=>{
    const a=bindings.agents.find(x=>x.id===b.dataset.bindA);
    openModal('Avaya station mapping',field('station','Station / extension',a.station||a.extension||'')+
      field('login_id','Avaya agent login ID (optional)',a.login_id||''),'auraAgentBinding',a.id);
    $('modalFields').querySelector('#fld_login_id').required=false;
  });
  document.querySelectorAll('[data-aura-approve]').forEach(b=>b.onclick=async()=>{
    const phrase=window.prompt('This requests a LIVE Avaya telephony action. Type APPROVE AVAYA ACTION to approve:');
    if(phrase!=='APPROVE AVAYA ACTION')return;
    try{await api('/aura/commands/'+b.dataset.auraApprove+'/approve',{method:'POST',body:JSON.stringify({confirmation:phrase})});flash('Queued for AES bridge — not yet executed');await loadAura();}catch(e){flash(e.message);}
  });
  document.querySelectorAll('[data-aura-cancel]').forEach(b=>b.onclick=async()=>{
    if(!window.confirm('Cancel this unexecuted request?'))return;
    try{await api('/aura/commands/'+b.dataset.auraCancel+'/cancel',{method:'POST'});await loadAura();}catch(e){flash(e.message);}
  });
}
$('auraRefresh').onclick=loadAura;
$('auraCommandForm').onsubmit=async e=>{
  e.preventDefault();const data=Object.fromEntries(new FormData(e.target).entries());
  data.call_id=data.call_id.trim()||null;data.destination=data.destination.trim()||null;
  data.request_key=crypto.randomUUID().replace(/-/g,'');
  try{await api('/aura/commands',{method:'POST',body:JSON.stringify(data)});flash('Command created; approval required');e.target.reset();await loadAura();}catch(err){flash(err.message);}
};
$('auraPlanForm').onsubmit=async e=>{
  e.preventDefault();const data=Object.fromEntries(new FormData(e.target).entries());
  try{await api('/aura/routing-plans',{method:'POST',body:JSON.stringify(data)});flash('Plan saved; PBX unchanged');e.target.reset();await loadAura();}catch(err){flash(err.message);}
};
// Add mapping handling without modifying the previous modal operation flows.
const originalModalSubmit=$('modalForm').onsubmit;
$('modalForm').onsubmit=async e=>{
 if(modalKind!=='auraQueueBinding'&&modalKind!=='auraAgentBinding')return originalModalSubmit(e);
 e.preventDefault();$('modalError').textContent='';
 const data=Object.fromEntries(new FormData(e.target).entries());
 Object.keys(data).forEach(k=>data[k]=data[k].trim()||null);
 const path=modalKind==='auraQueueBinding'?'/aura/queues/'+modalId+'/binding':'/aura/agents/'+modalId+'/binding';
 try{await api(path,{method:'PUT',body:JSON.stringify(data)});$('modal').close();flash('Mapping saved');await loadAura();}
 catch(err){$('modalError').textContent=err.message;}
};
