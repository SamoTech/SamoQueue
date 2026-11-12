'use strict';
let token = sessionStorage.getItem('qp_token') || '';
let userRole = sessionStorage.getItem('qp_role') || 'admin';
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
function signout(){disconnectMonitorAudio();stopRecordingPlayer();userRole='admin';sessionStorage.removeItem('qp_role');token='';sessionStorage.removeItem('qp_token');$('login').hidden=false;$('app').hidden=true;$('password').value='';}
function signin(){ $('login').hidden=true;$('app').hidden=false;document.querySelectorAll('[data-page="settings"]').forEach(e=>e.hidden=userRole!=='admin');refresh(); }
$('loginForm').addEventListener('submit',async e=>{e.preventDefault();$('loginError').textContent='';const btn=e.target.querySelector('button');btn.disabled=true;try{const data=await api('/login',{method:'POST',body:JSON.stringify({username:$('username').value,password:$('password').value})});token=data.token;userRole=data.role;sessionStorage.setItem('qp_role',userRole);sessionStorage.setItem('qp_token',token);signin();}catch(err){$('loginError').textContent=err.message;}finally{btn.disabled=false;}});
$('logout').addEventListener('click',signout);
if(token) signin();
setInterval(()=>{if(token)refresh(true);},4000);
setInterval(()=>{$('clock').textContent=new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});},1000);
async function refresh(quiet=false){if(!token)return;try{snapshot=await api('/dashboard');render();$('liveIndicator').innerHTML='<span class="pulse"></span> Dashboard refreshing';}catch(err){if(!quiet)flash(err.message);$('liveIndicator').textContent='⚠ Refresh failed';}}
$('refreshBtn').onclick=()=>refresh();
function navigate(page){activePage=page;document.querySelectorAll('.page').forEach(el=>el.hidden=el.id!=='page-'+page);document.querySelectorAll('.nav-link').forEach(el=>el.classList.toggle('active',el.dataset.page===page));$('crumb').textContent={overview:'Overview',calls:'Live calls',agents:'Agent management',reports:'Reports',recordings:'Call recordings',monitor:'Live listening',ivr:'IVR designer',settings:'Configuration'}[page];document.querySelector('.sidebar').classList.remove('open');if(snapshot)render();if(page==='recordings')loadRecordings();if(page==='monitor')loadMonitoring();if(page==='ivr')loadIvr();}
document.querySelectorAll('.nav-link').forEach(el=>el.onclick=()=>navigate(el.dataset.page));
document.querySelectorAll('[data-goto]').forEach(el=>el.onclick=()=>navigate(el.dataset.goto));
$('mobileMenu').onclick=()=>document.querySelector('.sidebar').classList.toggle('open');
$('callFilter').onchange=()=>renderCalls();
function render(){if(!snapshot)return;const d=snapshot;
  $('demoBanner').style.display=d.integration.mode==='DEMO'?'block':'none';
  document.querySelectorAll('.demo-action').forEach(b=>b.style.display=d.integration.mode==='DEMO'?'':'none');
  $('sideConnection').textContent=d.integration.connected?'● Native engine reporting healthy':'● Native engine not verified';
  $('sideConnection').style.color=d.integration.connected?'#64ddb2':'#f6c47b';
  $('provider').textContent=d.integration.last_source||'No events';
  $('lastEvent').textContent=relative(d.integration.last_event_at);
  document.querySelector('#page-settings .diag-row .warning').textContent=d.integration.connected?'Yes — engine-reported':'No';
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
 $('callRows').innerHTML=calls.length?calls.map(c=>`<tr><td class="table-strong">${escapeHtml(c.external_id)}</td><td>${escapeHtml(c.caller)}</td><td>${escapeHtml(queueBy(c.queue_id)?.name||c.queue_id)}</td><td>${badge(c.status)}</td><td>${timeFmt(c.arrived_at)}</td><td>${duration(c.wait_seconds)}</td><td>${escapeHtml(agentBy(c.agent_id)?.name||'—')}</td><td>${c.status==='answered'&&c.source==='queuepilot-native'&&userRole==='admin'?`<button class="small-button" data-request-listen="${escapeHtml(c.id)}">Request listen</button> ${c.control?.state==='pending'?'<span class="tag amber">Command pending</span>':`<button class="small-button" data-call-action="${c.hold_active?'resume':'hold'}" data-call-id="${escapeHtml(c.id)}">${c.hold_active?'Resume':'Hold'}</button>`}${c.control?` <span class="tag ${c.control.state==='rejected'?'red':'gray'}">${escapeHtml(c.control.action)}: ${escapeHtml(c.control.state)}</span>`:''}`:'—'}</td></tr>`).join(''):emptyRow(8,'No matching call events');document.querySelectorAll('[data-request-listen]').forEach(b=>b.onclick=()=>requestListen(b.dataset.requestListen));
 document.querySelectorAll('[data-call-action]').forEach(b=>b.onclick=async()=>{
   const action=b.dataset.callAction;if(!confirm('Request lab '+action+' for this live call?'))return;
   try{await api('/engine/calls/'+encodeURIComponent(b.dataset.callId)+'/control',
     {method:'POST',body:JSON.stringify({action})});flash(action+' request queued; verify engine acknowledgement');refresh();}
   catch(e){flash('Call control: '+e.message);}
 });}
function renderAgents(){const a=snapshot.agents;$('agentMiniStats').innerHTML=[['Total agents',a.length],['Available',a.filter(x=>x.status==='available').length],['On call',a.filter(x=>x.status==='busy').length],['Unavailable',a.filter(x=>['away','offline','acw'].includes(x.status)).length]].map(([l,n])=>`<div class="mini-stat"><p>${l}</p><strong>${num(n)}</strong></div>`).join('');
 $('agentRows').innerHTML=a.length?a.map(x=>`<tr><td><div class="table-strong">${escapeHtml(x.name)}</div><div class="table-sub">${escapeHtml(x.id)}</div></td><td>${escapeHtml(x.extension||'—')}</td><td>${escapeHtml(queueBy(x.queue_id)?.name||'Unassigned')}</td><td>${badge(x.status)}${x.control_state&&x.control_state!=='applied'?` <span class="tag amber">${escapeHtml(x.control_state)}: ${escapeHtml(x.desired_state)}</span>`:''}</td><td>${timeFmt(x.last_change)}</td><td><button class="small-button" data-status="${escapeHtml(x.id)}">Edit</button></td></tr>`).join(''):emptyRow(6,'No agents registered');
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
function queueModal(id=null){const q=id?queueBy(id):{name:'',skill:'',sla_seconds:20,target_percent:80,enabled:1};openModal(id?'Edit queue':'Create queue',field('name','Queue name',q.name)+field('skill','Local ACD skill label',q.skill)+field('sla_seconds','SLA threshold (seconds)',q.sla_seconds,'number')+field('target_percent','SLA target (%)',q.target_percent,'number')+selectField('enabled','Enabled',[['true','Enabled'],['false','Disabled']],q.enabled?'true':'false'),'queue',id);}
function agentModal(){openModal('Add agent',field('name','Full name')+field('extension','Extension number')+selectField('queue_id','Assigned queue',[['','Unassigned'],...snapshot.queues.map(q=>[q.id,q.name])],'')+selectField('status','Initial local status',Object.entries(statuses).filter(([s])=>['available','busy','acw','away','offline'].includes(s)).map(([s,val])=>[s,val[0]]),'offline'),'agent');}
function statusModal(id){const a=agentBy(id);if(!a)return;
 const options=a.native_managed?[['available','Available'],['away','Away'],['offline','Offline']]:[['available','Available'],['busy','On call (directory only)'],['acw','After-call work (directory only)'],['away','Away'],['offline','Offline']];
 const help=a.native_managed?'Sends a durable request to the local QueuePilot ACD. Active calls are never interrupted; pending commands apply when the agent is idle.':'This agent is not mapped to the local SIP engine; status is directory-only.';
 openModal('Edit agent availability',`<p class="small-note">${escapeHtml(help)}</p>`+selectField('status',a.name,options,a.desired_state?({ready:'available',aux:'away',offline:'offline'}[a.desired_state]||a.status):a.status),'status',id);
}
$('newQueueBtn').onclick=()=>queueModal();$('newAgentBtn').onclick=()=>agentModal();
$('closeModal').onclick=()=> $('modal').close();$('cancelModal').onclick=()=> $('modal').close();
$('modalForm').onsubmit=async e=>{e.preventDefault();$('modalError').textContent='';const form=new FormData(e.target);const data=Object.fromEntries(form.entries());
 try{if(modalKind==='queue'){data.sla_seconds=Number(data.sla_seconds);data.target_percent=Number(data.target_percent);data.enabled=data.enabled==='true';await api(modalId?'/queues/'+modalId:'/queues',{method:modalId?'PUT':'POST',body:JSON.stringify(data)});}else if(modalKind==='agent'){data.queue_id=data.queue_id||null;await api('/agents',{method:'POST',body:JSON.stringify(data)});}else if(modalKind==='status'){const result=await api('/agents/'+modalId+'/status',{method:'PATCH',body:JSON.stringify(data)});$('modal').close();flash(result.scope==='pending_native_runtime'?'Request queued for native SIP runtime':'Directory status saved');refresh();return;} $('modal').close();flash('Saved successfully');refresh();}catch(err){$('modalError').textContent=err.message;}};


// QueuePilot lab: one stereo WAV, approved RTP listening, IVR drafts and reports.
let recordingBlobUrl=null;
let monitoringSocket=null;
let monitorAudioContext=null;
let scheduledAudioTime=0;
let editingIvr=null;
function stopRecordingPlayer(){
  const player=$('recordingPlayer');if(player){player.pause();player.removeAttribute('src');player.load();}
  if(recordingBlobUrl)URL.revokeObjectURL(recordingBlobUrl);
  recordingBlobUrl=null;if($('recordingPlayerWrap'))$('recordingPlayerWrap').hidden=true;
}
async function blobRequest(path){
  const response=await fetch('/api'+path,{cache:'no-store',headers:{Authorization:'Bearer '+token}});
  if(response.status===401){signout();throw new Error('Session expired');}
  if(!response.ok)throw new Error('Download failed ('+response.status+')');
  return response.blob();
}
async function playRecording(id,label,download=false){
  try{
    const blob=await blobRequest('/cc/recordings/'+encodeURIComponent(id)+'/audio'+(download?'?download=true':''));
    const url=URL.createObjectURL(blob);
    if(download){const link=document.createElement('a');link.href=url;link.download=label;link.click();setTimeout(()=>URL.revokeObjectURL(url),30000);return;}
    stopRecordingPlayer();recordingBlobUrl=url;$('recordingPlayerWrap').hidden=false;
    $('recordingPlayer').src=url;$('nowPlaying').textContent=label;
    await $('recordingPlayer').play();
  }catch(err){flash('Recording: '+err.message);}
}
async function loadRecordings(){
  if(!token)return;
  try{const data=await api('/cc/recordings?limit=200');
    $('recordingRows').innerHTML=data.recordings.length?data.recordings.map(r=>`<tr>
      <td>${new Date(r.recorded_at*1000).toLocaleString()}</td>
      <td>${escapeHtml(r.external_call_id.slice(-12))}</td><td>${escapeHtml(r.queue_name)}</td>
      <td>${escapeHtml(r.agent_name||'—')}</td><td>${duration(r.duration_seconds)}</td>
      <td>${(r.bytes/1024/1024).toFixed(1)} MB</td>
      <td><button class="small-button" data-play="${escapeHtml(r.id)}">Play</button>
      <button class="small-button" data-download="${escapeHtml(r.id)}">↓</button></td></tr>`).join(''):emptyRow(7,'No recordings indexed yet. Native RTP capture requires explicit lab opt-in and an approved notice policy.');
    document.querySelectorAll('[data-play]').forEach(b=>b.onclick=()=>{
      const r=data.recordings.find(x=>x.id===b.dataset.play);if(r)playRecording(r.id,r.original_name);
    });
    document.querySelectorAll('[data-download]').forEach(b=>b.onclick=()=>{
      const r=data.recordings.find(x=>x.id===b.dataset.download);if(r)playRecording(r.id,r.original_name,true);
    });
  }catch(e){flash('Recordings: '+e.message);}
}
$('reloadRecordings').onclick=loadRecordings;

async function requestListen(callId){
  if(!window.confirm('Request supervisor observation of this live QueuePilot call? Separate approval and the native media mixer are required.'))return;
  try{await api('/cc/monitor/sessions',{method:'POST',body:JSON.stringify({call_id:callId,purpose:'quality_assurance'})});
    flash('Listening request created. Reviewer approval required.');navigate('monitor');
  }catch(e){flash('Monitoring: '+e.message);}
}
async function loadMonitoring(){
  if(!token)return;
  try{const data=await api('/cc/monitor/sessions');
    $('monitorRows').innerHTML=data.sessions.length?data.sessions.map(x=>`<tr>
      <td>${escapeHtml(x.native_call_id.slice(-12))}</td><td>${escapeHtml(x.agent_name||'—')}</td>
      <td>${escapeHtml(x.target_station)}</td><td>${new Date(x.created_at*1000).toLocaleString()}</td>
      <td>${escapeHtml(x.state)}</td><td>
      ${x.state==='pending_approval'&&userRole==='reviewer'?`<button class="small-button" data-monapprove="${escapeHtml(x.id)}">Approve</button>`:''}
      ${x.state==='active'&&userRole==='reviewer'?`<button class="small-button" data-monplay="${escapeHtml(x.id)}">Listen in browser</button>`:''}
      ${['pending_approval','queued','active','leased'].includes(x.state)?`<button class="small-button" data-monstop="${escapeHtml(x.id)}">Stop</button>`:''}
      </td></tr>`).join(''):emptyRow(6,'No live monitoring requests');
    document.querySelectorAll('[data-monapprove]').forEach(b=>b.onclick=async()=>{
      if(window.prompt('Separate approval required. Type APPROVE LIVE LISTEN')!=='APPROVE LIVE LISTEN')return;
      try{await api('/cc/monitor/sessions/'+encodeURIComponent(b.dataset.monapprove)+'/approve',
       {method:'POST',body:JSON.stringify({confirmation:'APPROVE LIVE LISTEN'})});flash('Approved; waiting for the opt-in native RTP observer to attach');loadMonitoring();}
      catch(e){flash(e.message);}
    });
    document.querySelectorAll('[data-monstop]').forEach(b=>b.onclick=async()=>{
      if(!window.confirm('Stop this listening session? The native media engine must acknowledge listener stop.'))return;
      try{await api('/cc/monitor/sessions/'+encodeURIComponent(b.dataset.monstop)+'/stop',{method:'POST'});
       disconnectMonitorAudio();flash('Stop requested; await native engine acknowledgment');loadMonitoring();}catch(e){flash(e.message);}
    });
    document.querySelectorAll('[data-monplay]').forEach(b=>b.onclick=()=>connectMonitorAudio(b.dataset.monplay));
  }catch(e){flash('Live monitoring: '+e.message);}
}
$('reloadMonitoring').onclick=loadMonitoring;
function disconnectMonitorAudio(){
  if(monitoringSocket){monitoringSocket.close();monitoringSocket=null;}
  if(monitorAudioContext){monitorAudioContext.close();monitorAudioContext=null;}
  scheduledAudioTime=0;
  if($('livePlayerWrap'))$('livePlayerWrap').hidden=true;
}
$('disconnectLive').onclick=disconnectMonitorAudio;
async function connectMonitorAudio(sid){
  try{
    disconnectMonitorAudio();
    const result=await api('/cc/monitor/sessions/'+encodeURIComponent(sid)+'/audio-ticket',{method:'POST'});
    // AudioContext is intentionally created only after a direct user click.
    monitorAudioContext=new (window.AudioContext||window.webkitAudioContext)();
    await monitorAudioContext.resume();
    const scheme=location.protocol==='https:'?'wss:':'ws:';
    const ws=new WebSocket(scheme+'//'+location.host+'/api/cc/monitor/sessions/'+encodeURIComponent(sid)+'/audio/ws?ticket='+encodeURIComponent(result.ticket));
    monitoringSocket=ws;ws.binaryType='arraybuffer';
    $('livePlayerWrap').hidden=false;$('liveAudioState').textContent='Connecting to QueuePilot native RTP observer…';
    ws.onopen=()=>$('liveAudioState').textContent='Stream connected · 16kHz PCM';
    ws.onclose=()=>$('liveAudioState').textContent='Disconnected (or no active session)';
    ws.onerror=()=>$('liveAudioState').textContent='Media gateway error';
    ws.onmessage=(event)=>{
      if(!(event.data instanceof ArrayBuffer)||!monitorAudioContext)return;
      const ctx=monitorAudioContext;const bytes=new DataView(event.data);
      const length=Math.floor(bytes.byteLength/2);
      if(length<1)return;
      const buffer=ctx.createBuffer(1,length,16000);
      const channel=buffer.getChannelData(0);
      for(let i=0;i<length;i++)channel[i]=bytes.getInt16(i*2,true)/32768;
      const source=ctx.createBufferSource();source.buffer=buffer;source.connect(ctx.destination);
      if(scheduledAudioTime < ctx.currentTime || scheduledAudioTime > ctx.currentTime + 2)
        scheduledAudioTime=ctx.currentTime+0.07;
      source.start(scheduledAudioTime);
      scheduledAudioTime+=buffer.duration;
    };
  }catch(err){disconnectMonitorAudio();flash('Live audio: '+err.message);}
}

function exampleIvr(){
  const queue=snapshot?.queues[0]?.id||'REPLACE_WITH_EXISTING_QUEUE_ID';
  return {nodes:[
    {id:'start',type:'start',label:'Incoming VDN call',next:'greeting'},
    {id:'greeting',type:'announcement',label:'Welcome',prompt:'Welcome to our company',next:'options'},
    {id:'options',type:'menu',label:'Main menu',prompt:'Press 1 for an agent or 0 to end',choices:{'1':'agents','0':'goodbye'}},
    {id:'agents',type:'queue',label:'Customer Service queue',queue_id:queue},
    {id:'goodbye',type:'hangup',label:'End call'}
  ]};
}
$('ivrSample').onclick=()=>{$('ivrGraph').value=JSON.stringify(exampleIvr(),null,2);if(!$('ivrName').value)$('ivrName').value='Main IVR';};
async function loadIvr(){
  if(!token)return;
  try{const data=await api('/cc/ivr');
    $('ivrFlowList').innerHTML=data.flows.length?data.flows.map(x=>`<div class="queue-setting-row"><div><strong>${escapeHtml(x.name)} · v${x.version}</strong>
      <p>${escapeHtml(x.status)} · ${escapeHtml(x.description)}</p></div><div>
      <button class="small-button" data-ivredit="${escapeHtml(x.id)}">View/edit</button>
      ${x.status==='draft'?`<button class="small-button" data-ivrpropose="${escapeHtml(x.id)}">Propose deployment</button>`:''}</div></div>`).join(''):'<p class="small-note">No IVR flows yet; load the example and save a new draft.</p>';
    document.querySelectorAll('[data-ivredit]').forEach(b=>b.onclick=async()=>{
      try{const x=await api('/cc/ivr/'+encodeURIComponent(b.dataset.ivredit));editingIvr=x.status==='draft'?x.id:null;
        $('ivrName').value=x.name;$('ivrDescription').value=x.description;$('ivrGraph').value=JSON.stringify(x.graph,null,2);
        if(x.status!=='draft')flash('Read-only: this flow is awaiting deployment review');
      }catch(e){flash(e.message);}
    });
    document.querySelectorAll('[data-ivrpropose]').forEach(b=>b.onclick=async()=>{
      if(window.prompt('Type PROPOSE IVR DEPLOYMENT. This does not activate IVR.')!=='PROPOSE IVR DEPLOYMENT')return;
      try{await api('/cc/ivr/'+encodeURIComponent(b.dataset.ivrpropose)+'/propose-deployment',
       {method:'POST',body:JSON.stringify({confirmation:'PROPOSE IVR DEPLOYMENT'})});flash('Change proposed; native IVR not yet activated');loadIvr();}
      catch(e){flash(e.message);}
    });
    if(!$('ivrGraph').value)$('ivrSample').click();
  }catch(e){flash('IVR: '+e.message);}
}
$('reloadIvr').onclick=loadIvr;
$('ivrForm').onsubmit=async e=>{
  e.preventDefault();
  try{const data={name:$('ivrName').value,description:$('ivrDescription').value,graph:JSON.parse($('ivrGraph').value)};
    await api('/cc/ivr'+(editingIvr?'/'+encodeURIComponent(editingIvr):''),
       {method:editingIvr?'PUT':'POST',body:JSON.stringify(data)});
    editingIvr=null;flash('IVR draft saved (native IVR not yet activated)');$('ivrForm').reset();loadIvr();
  }catch(err){flash('IVR: '+err.message);}
};
function reportParams(){
  const start=$('reportFrom').value,end=$('reportTo').value;
  const p=new URLSearchParams();
  if(start)p.set('start',new Date(start).toISOString());
  if(end)p.set('end',new Date(end).toISOString());
  return p.toString()?'?'+p.toString():'';
}
async function downloadReport(path,name){
  try{const blob=await blobRequest(path);const url=URL.createObjectURL(blob);
    const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);
    flash('Report exported: '+name);
  }catch(e){flash('Report: '+e.message);}
}
$('exportSelectedCsv').onclick=()=>downloadReport('/cc/reports/'+$('reportType').value+'.csv'+reportParams(),'queuepilot-'+$('reportType').value+'.csv');
$('exportSelectedJson').onclick=()=>downloadReport('/cc/reports/'+$('reportType').value+reportParams(),'queuepilot-'+$('reportType').value+'.json');
$('exportAll').onclick=()=>downloadReport('/cc/reports/all.zip'+reportParams(),'queuepilot-all-reports.zip');
