'use strict';
const $=id=>document.getElementById(id);
let token='',records={profiles:[],calibrations:[]},lastFrame='';
async function api(path,body){const response=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json','X-AntSDR-Token':token},body:JSON.stringify(body)}:{});const data=await response.json();if(!response.ok)throw new Error(data.error||'Request failed');return data;}
async function refreshRecords(){records=await api('/api/records');const selected=$('profile').value;$('profile').replaceChildren(...records.profiles.map(p=>{const o=document.createElement('option');o.value=p.name;o.textContent=p.name;return o;}));if(records.profiles.some(p=>p.name===selected))$('profile').value=selected;editProfile();}
function editProfile(){const p=records.profiles.find(p=>p.name===$('profile').value);if(!p)return;if($('recordType').value==='profiles')$('editor').value=JSON.stringify(p,null,2);loadTuning(p);}
function draw(frame){if(frame.time_utc===lastFrame)return;lastFrame=frame.time_utc;const data=frame.power_dbfs,c=$('spectrum'),x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);x.font='12px monospace';for(let db=-120;db<=0;db+=30){const y=(0-db)/120*(c.height-30)+10;x.strokeStyle='#253846';x.beginPath();x.moveTo(38,y);x.lineTo(c.width,y);x.stroke();x.fillStyle='#9fb6c5';x.fillText(db,2,y-3);}x.strokeStyle='#60e4b4';x.lineWidth=1.5;x.beginPath();data.forEach((db,i)=>{const px=38+i/(data.length-1)*(c.width-38),py=10+(0-Math.max(-120,Math.min(0,db)))/120*(c.height-30);if(i)x.lineTo(px,py);else x.moveTo(px,py);});x.stroke();const w=$('waterfall'),ctx=w.getContext('2d');ctx.drawImage(w,0,0,w.width,w.height-2,0,2,w.width,w.height-2);for(let i=0;i<w.width;i++){const db=data[Math.min(data.length-1,Math.floor(i/w.width*data.length))],v=Math.max(0,Math.min(1,(db+120)/120));ctx.fillStyle=`hsl(${250-v*250} 85% ${8+v*55}%)`;ctx.fillRect(i,0,1,2);}$('frequency').textContent=`${(frame.frequency_start_hz/1e6).toFixed(3)} – ${((frame.frequency_start_hz+frame.bin_width_hz*data.length)/1e6).toFixed(3)} MHz`;}
async function poll(){try{const state=await api('/api/state'),h=state.health;if(h){$('connection').textContent=h.healthy?'Device healthy':h.maintenance?'Maintenance':'Device unavailable / unhealthy';$('connection').className=h.healthy?'good':'bad';$('revision').textContent=h.firmware?.source_revision?.slice(0,9)||'—';$('uptime').textContent=h.uptime_seconds!==undefined?Math.floor(h.uptime_seconds)+' s':'—';$('link').textContent=h.link_up?'Up':'Down';$('recoveries').textContent=h.restarts??'—';$('health').textContent=JSON.stringify(h,null,2);const r=h.runtime?.radio,n=h.runtime?.network;if(r?.fpga){const f=r.fpga,ip=v=>[v>>>24,(v>>>16)&255,(v>>>8)&255,v&255].join('.');$('runtime').textContent=[`Board: ${r.board} (${r.board_source})`,`Daemons: radio uid ${r.uid}, network uid ${n?.uid??'unavailable'}`,`Owner: ${f.lease_remaining_ms?ip(f.owner_ipv4):'Available'} · ${f.lease_remaining_ms} ms remaining`,`Discovery: ${r.discovery_replies} replies / ${r.discovery_rejected} rejected`,`Leases: ${r.leases_acquired} acquired / ${r.leases_denied} denied / ${r.leases_expired} expired`,`FPGA IPv4: ${ip(f.ipv4)} · ABI ${f.version.toString(16)}`,`Network updates: ${n?.network_updates??'unavailable'}`,`Status: ${r.stale?'stale diagnostics':r.fault||n?.fault||'OK'}`].join('\n');}else $('runtime').textContent=r?.fault||'Runtime diagnostics unavailable on this firmware';}if(state.job){$('job').textContent=state.job.kind+': '+state.job.status+(state.job.error?' — '+state.job.error:'');if(state.job.report)$('result').textContent=JSON.stringify(state.job.report,null,2);if(state.job.kind==='transmit')$('txStatus').textContent=$('job').textContent;}if(state.frame){draw(state.frame);if(state.frame.telemetry)renderReceiverTelemetry(state.frame.telemetry,state.frame.signal);}if(state.job?.kind==='inspect'&&state.job.report){renderReceiverTelemetry(state.job.report.telemetry);renderReceiverCapabilities(state.job.report.capabilities);}}catch(e){$('connection').textContent=e.message;}setTimeout(poll,300);}
async function start(kind){try{const threshold=$('threshold').value;await api('/api/jobs',{kind,trigger_mode:$('triggerMode').value,device_delay_seconds:$('triggerMode').value==='device-time'?Number($('deviceDelay').value):null,profile:$('profile').value,duration:Number($('duration').value),threshold_dbfs:threshold===''?null:Number(threshold),pretrigger_seconds:Number($('pretrigger').value),trigger_after_seconds:$('triggerAfter').value===''?null:Number($('triggerAfter').value),trigger_timeout:Number($('triggerTimeout').value)});}catch(e){$('job').textContent=e.message;}}
$('preview').onclick=()=>start('spectrum');$('record').onclick=()=>start('capture');$('stop').onclick=()=>api('/api/stop',{}).catch(e=>$('job').textContent=e.message);$('profile').onchange=editProfile;$('recordType').onchange=()=>{if($('recordType').value==='profiles')editProfile();else $('editor').value=JSON.stringify({schema_version:1,name:'measured-reference',device_serial:'',measured_at_utc:new Date().toISOString(),reference:'',frequency_error_ppm:0,level_correction_db:0},null,2);};$('save').onclick=async()=>{try{await api('/api/'+$('recordType').value,JSON.parse($('editor').value));$('saveStatus').textContent='Saved';await refreshRecords();}catch(e){$('saveStatus').textContent=e.message;}};
(async()=>{try{token=(await api('/api/session')).token;await refreshRecords();poll();}catch(e){$('connection').textContent=e.message;}})();

$('transmit').onclick=async()=>{try{await api('/api/jobs',{kind:'transmit',settings:{waveform:$('txWaveform').value,frequency_hz:Number($('txFrequency').value)*1e6,tone_hz:Number($('txTone').value)*1e3,channel:Number($('txChannel').value),sample_rate:Number($('txRate').value)*1e6,bandwidth_hz:Number($('txBandwidth').value)*1e6,gain_db:Number($('txGain').value),amplitude:Number($('txAmplitude').value),duration:Number($('txDuration').value),start_delay_seconds:Number($('txDelay').value)}});$('txStatus').textContent='Burst scheduled';}catch(e){$('txStatus').textContent=e.message;}};
$('txStop').onclick=()=>api('/api/stop',{}).catch(e=>$('txStatus').textContent=e.message);

function loadTuning(p){
  $('rxFrequency').value=p.frequency_hz/1e6;$('rxRate').value=p.sample_rate/1e6;
  $('rxBandwidth').value=p.bandwidth_hz/1e6;$('rxOffset').value=(p.lo_offset_hz||0)/1e3;
  $('rxSettle').value=p.settle_seconds??0.05;$('rxChannels').value=p.channels.join(',');
  $('rxDc').value=String(p.auto_dc_offset??true);$('rxIq').value=String(p.auto_iq_balance??true);
  for(const c of [0,1]){const channel={...p,...(p.channel_settings?.[c]||{})};
    $('rxMode'+c).value=channel.gain_mode||'manual';$('rxGain'+c).value=channel.gain_db;
    $('rxAntenna'+c).value=channel.antenna||'RX2';updateGain(c);}
}
function updateGain(c){$('rxGain'+c).disabled=$('rxMode'+c).value!=='manual';}
for(const c of [0,1])$('rxMode'+c).onchange=()=>updateGain(c);
$('saveTuning').onclick=async()=>{try{
  const p=records.profiles.find(p=>p.name===$('profile').value);if(!p)throw new Error('Select a profile');
  const channels=$('rxChannels').value.split(',').map(Number),overrides={};
  for(const c of channels)overrides[c]={gain_mode:$('rxMode'+c).value,gain_db:Number($('rxGain'+c).value),antenna:$('rxAntenna'+c).value};
  await api('/api/profiles',{...p,frequency_hz:Number($('rxFrequency').value)*1e6,sample_rate:Number($('rxRate').value)*1e6,bandwidth_hz:Number($('rxBandwidth').value)*1e6,lo_offset_hz:Number($('rxOffset').value)*1e3,settle_seconds:Number($('rxSettle').value),channels,channel_settings:overrides,auto_dc_offset:$('rxDc').value==='true',auto_iq_balance:$('rxIq').value==='true'});
  $('tuningStatus').textContent='Saved. Tuning will apply to the next session.';await refreshRecords();
}catch(e){$('tuningStatus').textContent=e.message;}};
$('inspectRadio').onclick=()=>api('/api/jobs',{kind:'inspect',profile:$('profile').value}).catch(e=>$('tuningStatus').textContent=e.message);

function renderReceiverTelemetry(data,signal=[]){
  const lines=[];
  for(const c of data.channels||[]){
    const label=c.gain_mode==='manual'?'Manual gain':`${c.gain_mode||'Unknown'} AGC`;
    lines.push(`RX${c.channel+1} · ${label} · Gain index ${c.gain_index??'unavailable'}`);
    lines.push(`RSSI ${c.rssi===undefined?'unavailable':c.rssi.toFixed(2)+' dB (relative)'} · Temperature ${c.temp===undefined?'unavailable':c.temp.toFixed(1)+' °C'} · LO ${c.lo_locked===undefined?'unavailable':c.lo_locked?'locked':'unlocked'}`);
    lines.push(`DC tracking ${c.auto_dc_offset?'on':'off'} · IQ tracking ${c.auto_iq_balance?'on':'off'}`);
    const overload=Object.entries(c.overload_flags||{}).filter(([key,on])=>on&&!['gain_locked','low_power'].includes(key)).map(([key])=>key.replaceAll('_',' '));
    lines.push(`Overload snapshot: ${overload.join(', ')||'none reported'}${c.gain_mode==='fast'?' · AGC '+(c.agc_state===5?'locked':'settling'):''}`);
    const samples=signal.find(v=>v.channel===c.channel);
    if(samples)lines.push(`Received peak ${samples.peak_dbfs.toFixed(1)} dBFS · Clipped samples ${samples.clipped_samples}`);
    lines.push('');
  }
  for(const error of data.errors||[])lines.push(`RX${error.channel+1}: ${error.error}`);
  $('receiverTelemetry').textContent=lines.join('\n');
}
function renderReceiverCapabilities(data){
  const lines=[`${data.rf_chip} · Shared LO, analog bandwidth and tracking`,data.gain_units,data.rssi_units,''];
  for(const c of data.channels||[]){
    lines.push(`RX${c.channel+1} · Antennas: ${(c.antennas||[]).join(', ')}`);
    if(c.bandwidth_hz)lines.push(`Analog bandwidth: ${c.bandwidth_hz.min/1e6}–${c.bandwidth_hz.max/1e6} MHz`);
    for(const f of c.filters||[]){const name=f.name.split('/').at(-1);lines.push(`${name}: ${f.bypassed?'bypassed':'active'}${f.taps?' · '+f.taps.length+' taps · '+(f.input_rate/1e6).toFixed(3)+' → '+(f.output_rate/1e6).toFixed(3)+' MS/s':''}`);}
    lines.push('');
  }
  for(const error of data.errors||[])lines.push(`RX${error.channel+1}: ${error.error}`);
  $('receiverCapabilities').textContent=lines.join('\n');
}
