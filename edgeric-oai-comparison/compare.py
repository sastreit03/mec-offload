import subprocess,pathlib,collections,json,hashlib,csv,difflib,os
out=pathlib.Path(__file__).parent
s=pathlib.Path('/home/sstreit/mec-offload-ad/gnb-core/ext/openairinterface5g')
e=pathlib.Path('/home/sstreit/EdgeRIC-5G-OAI/openairinterface5g')
def git(p,*a):return subprocess.check_output(['git','-C',str(p),*a])
def tree(p,ref):
 d={}
 for row in git(p,'ls-tree','--full-tree','-rz',ref).split(b'\0'):
  if row:
   m,k=row.split(b'\t');mode,typ,sha=m.decode().split(); d[k.decode()]={'mode':mode,'type':typ,'sha':sha}
 return d
def blobhash(data):return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
et=tree(e.parent,'HEAD:openairinterface5g');bt=tree(s,'2024.w34');st={}
assert len(et)>3000
for row in git(s,'ls-files','--stage','-z').split(b'\0'):
 if not row:continue
 meta,k=row.split(b'\t');mode,sha,stage=meta.decode().split();k=k.decode()
 if mode=='160000':st[k]={'mode':mode,'type':'commit','sha':git(s/k,'rev-parse','HEAD').decode().strip()}
 else:
  p=s/k;data=os.readlink(p).encode() if p.is_symlink() else p.read_bytes();st[k]={'mode':mode,'type':'blob','sha':blobhash(data)}
for k,v in et.items():
 assert v['type']=='blob'
 assert blobhash((e/k).read_bytes())==v['sha']
rows=[]
for k in sorted(et.keys()|st.keys()):
 a=et.get(k); b=st.get(k)
 status='Sionna-only' if not a else 'EdgeRIC-only' if not b else 'content-different' if (a['type'],a['sha'])!=(b['type'],b['sha']) else 'identical'
 rows.append({'path':k,'status':status,'mode_diff':bool(a and b and a['mode']!=b['mode']),'edgeric_mode':a['mode'] if a else '', 'sionna_mode':b['mode'] if b else ''})
for name,rr in [('ran-all-files.tsv',rows),('ran-content-differences.tsv',[r for r in rows if r['status']!='identical'])]:
 with (out/name).open('w') as f:
  w=csv.DictWriter(f,fieldnames=list(rows[0]),delimiter='\t');w.writeheader();w.writerows(rr)
counts=collections.Counter(r['status'] for r in rows)
print('DIRECT COUNTS',dict(counts),flush=True)
print('MODES',sum(r['mode_diff'] for r in rows),'mode-only',sum(r['mode_diff'] and r['status']=='identical' for r in rows),flush=True)
print('BASELINE content equal',sum(et[k]['sha']==bt[k]['sha'] for k in et.keys()&bt.keys()),'EdgeRIC entries',len(et),'baseline entries',len(bt),flush=True)
print('BASELINE mode differences',sum(et[k]['mode']!=bt[k]['mode'] for k in et.keys()&bt.keys()),flush=True)
with (out/'sionna-to-edgeric.diff').open('w') as f:
 for r in rows:
  if r['status']=='identical':continue
  k=r['path'];a=st.get(k);b=et.get(k)
  if (a and a['type']=='commit') or (b and b['type']=='commit'):
   f.write(f'Submodule {k}: Sionna={a} EdgeRIC={b}\n');continue
  ab=(s/k).read_bytes() if a else b'';bb=(e/k).read_bytes() if b else b''
  if b'\0' in ab or b'\0' in bb:f.write(f'Binary files Sionna/{k} and EdgeRIC/{k} differ; Sionna bytes={len(ab)}, EdgeRIC bytes={len(bb)}\n');continue
  diff=list(difflib.unified_diff(ab.decode(errors='replace').splitlines(True),bb.decode(errors='replace').splitlines(True),fromfile='Sionna/'+k if a else '/dev/null',tofile='EdgeRIC/'+k if b else '/dev/null'))
  for line in diff:f.write(line if line.endswith('\n') else line+'\n\\ No newline at end of file\n')
  if not diff:f.write(f'Empty file present only in {r["status"]}: {k}\n')
core=e.parent/'oai-cn5g';proof=[]
for p in sorted(core.rglob('*')):
 if p.is_file():
  k=str(p.relative_to(core));b=bt.get('doc/tutorial_resources/oai-cn5g/'+k);h=blobhash(p.read_bytes());proof.append({'path':k,'bytes':p.stat().st_size,'upstream_blob':b['sha'] if b else '', 'edgeric_blob':h,'result':'identical' if b and b['sha']==h else 'extra'})
with (out/'core-upstream-verification.tsv').open('w') as f:
 w=csv.DictWriter(f,fieldnames=list(proof[0]),delimiter='\t');w.writeheader();w.writerows(proof)
summary={'counts':dict(counts),'mode_differences':sum(r['mode_diff'] for r in rows),'mode_only':sum(r['mode_diff'] and r['status']=='identical' for r in rows),'upstream_same':sum(et[k]['sha']==bt[k]['sha'] for k in et.keys()&bt.keys()),'edgeric_entries':len(et),'baseline_entries':len(bt),'core':proof}
(out/'summary.json').write_text(json.dumps(summary,indent=2))
print('Completed',flush=True)
