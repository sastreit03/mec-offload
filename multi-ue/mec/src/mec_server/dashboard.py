"""Browser dashboard adapted from dist-sparks, using the multi-UE status API."""
HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Streaming MEC</title><style>
body{font:16px system-ui;background:#101820;color:#e9eff4;margin:24px}
.grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(280px,1fr);gap:20px}
.card{background:#192631;border:1px solid #344451;border-radius:12px;padding:18px}
img{width:100%;aspect-ratio:16/9;object-fit:contain;background:#000;border-radius:8px}
pre{white-space:pre-wrap;overflow-wrap:anywhere}select{font:inherit;padding:8px;margin:0 12px 16px 0}
small{color:#afc1ce}@media(max-width:850px){.grid{grid-template-columns:1fr}}
</style></head><body><h1>Streaming MEC</h1>
<label for="ue">Preview UE </label><select id="ue"><option value="">No connected UE</option></select>
<span id="connection">Connecting…</span><div class="grid"><section class="card">
<h2>Received and decoded video</h2><img id="preview" alt="Waiting for decoded video" hidden>
<p id="preview-status">Select a connected UE.</p>
<small>Live MEC preview, up to 5 FPS. Preview frames may be skipped; inference and collection retain their own frame handling.</small>
</section><section class="card"><h2>Selected UE and stream</h2><pre id="stream">Waiting…</pre>
<h2>Latest scenario</h2><pre id="scenario">None</pre><h2>MEC status</h2><pre id="server">Connecting…</pre></section></div>
<script>
const el=id=>document.getElementById(id);let state=null,source='',available=false;
function preview(){
 const alias=el('ue').value;
 const u=Object.values(state?.ues||{}).find(u=>u.alias===alias&&u.connected);
 const v=state?.videos?.[u?.active_video];
 const wanted=alias&&u&&v&&!document.hidden;
 const key=wanted?alias+'|'+u.active_video:'';
 if(key!==source){el('preview').removeAttribute('src');source=key;available=false;
  if(wanted)el('preview').src='/preview.mjpg?ue_alias='+encodeURIComponent(alias);}
 el('preview').hidden=!wanted;
 el('preview-status').textContent=document.hidden?'Preview paused while tab is hidden.':
  !u?'Select a connected UE.':!v?'Waiting for an active stream.':
  state?.preview?.streams?.[alias]?.error|| (available?'Live preview':'Waiting for decoded video…');
 el('stream').textContent=JSON.stringify({ue:u||null,video_id:u?.active_video||null,stream:v||null},null,2);
}
el('preview').onload=()=>{available=true;};el('preview').onerror=()=>{source='';};
el('ue').onchange=preview;document.addEventListener('visibilitychange',preview);
window.addEventListener('pagehide',()=>el('preview').removeAttribute('src'));
async function refresh(){try{
 const response=await fetch('/api/status',{cache:'no-store'});if(!response.ok)throw Error(response.status);
 state=await response.json();const previous=el('ue').value;
 const aliases=[...new Set(Object.values(state.ues).filter(u=>u.connected&&u.alias).map(u=>u.alias))].sort();
 el('ue').replaceChildren(...(aliases.length?aliases:['']).map(a=>new Option(a||'No connected UE',a)));
 el('ue').value=aliases.includes(previous)?previous:(aliases.includes('pixel1')?'pixel1':aliases[0]||'');
 el('connection').textContent=state.readiness.ready?'MEC ready':'MEC not ready';
 el('scenario').textContent=JSON.stringify(state.scenario,null,2);
 el('server').textContent=JSON.stringify({run_id:state.run_id,readiness:state.readiness,
 unresolved_frames:state.unresolved,radio_report_age_s:state.radio_report_age_s},null,2);preview();
 }catch(e){el('connection').textContent='Connection unavailable';el('preview').removeAttribute('src');source='';}
 finally{setTimeout(refresh,1000);}}
refresh();
</script></body></html>'''
