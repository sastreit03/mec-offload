#!/usr/bin/env python3
"""Trace queue tests plus a source-level guard against changing allocation logic."""
from pathlib import Path
import csv, re, subprocess, tempfile
root=Path(__file__).resolve().parents[2]/'gnb-core/ext/openairinterface5g'
er=root/'executables/edgeric'
baseline='0769d8df7817bba5cc40916b4002cc716709aead'
def strip_trace(s):
    s=s.replace('#include "executables/edgeric/scheduler_trace.h"','')
    for name in ['RIC_TRACE_SCOPE','RIC_TRACE_SET','RIC_TRACE_RETX_BEAM_FAILURE']:
        while (m:=re.search(r'\b'+name+r'\(',s)):
            a=m.start();end=m.end();depth=1
            while depth:
                depth+=(s[end]=='(')-(s[end]==')');end+=1
            assert s[end]==';';s=s[:a]+s[end+1:]
    return re.sub(r'\s+','',s)
for name in ['gNB_scheduler_dlsch.c','gNB_scheduler_ulsch.c']:
    path='openair2/LAYER2/NR_MAC_gNB/'+name
    before=subprocess.check_output(['git','show',baseline+':'+path],cwd=root,text=True)
    assert strip_trace((root/path).read_text())==strip_trace(before),name+' allocation logic changed'
code=r'''
#include "scheduler_trace.cpp"
#include <cassert>
#include <vector>
#include <cstring>
int main(int argc,char **argv) {
 assert(argc==3);
 if(!std::strcmp(argv[1],"queue")) {
   // No consumer: deterministic capacity and contention loss checks.
   enabled.store(true);
   for(size_t i=0;i<capacity+17;i++) {auto r=ric_trace_begin(0,i,0,0,0,0,0,0,FLT_MAX);ric_trace_finish(&r);}
   assert(queued==capacity && dropped==17);
   gate.test_and_set();auto r=ric_trace_begin(0,0,0,0,0,0,0,0,FLT_MAX);ric_trace_finish(&r);gate.clear();
   assert(dropped==18);
   ric_trace_receive(false);ric_trace_receive(true);
   assert(receive_empty==1 && receive_messages==1); // survives full queue
   for(size_t i=0;i<capacity;i++){assert(dequeue(r));assert(r.rnti==int(i));}
   assert(!dequeue(r));enabled.store(false);return 0;
 }
 if(!std::strcmp(argv[1],"disabled")) {
   unsetenv("OAI_SCHED_TRACE_FILE");ric_trace_start();assert(!ric_trace_enabled());
   ric_trace_receive(false);ric_trace_receive(true);assert(receive_empty==0 && receive_messages==0);
   auto r=ric_trace_begin(0,1,0,0,0,0,0,0,1);ric_trace_finish(&r);assert(queued==0 && dropped==0);return 0;
 }
 setenv("OAI_SCHED_TRACE_FILE",argv[2],1);ric_trace_start();
 if(!std::strcmp(argv[1],"open_fail")){assert(!ric_trace_enabled() && io_errors>0);return 0;}
 assert(ric_trace_enabled());
 ric_trace_receive(false);ric_trace_receive(false);ric_trace_receive(true);
 const auto received=ric_trace_now_ns();ric_trace_policy(42,received,2,1);
 ric_trace_weight(0,1,.2f);ric_trace_weight(0,2,.8f);ric_trace_weight(1,1,1.f);
 auto r=ric_trace_begin(1,1,1023,18,0,2,0,0,1.f);
 r.reference=100;r.remaining=30;r.free_span=30;r.cap=30;r.sizing_cap=24;
 r.stage=RIC_TRACE_COMMITTED;r.prbs=20;r.tbs=1000;r.mcs=10;
 ric_trace_finish(&r);
 r=ric_trace_begin(0,2,1,2,1,2,0,0,0.f);r.reference=100;r.cap=0;r.stage=RIC_TRACE_CAP;ric_trace_finish(&r);
 r=ric_trace_begin(0,1,1,2,1,2,1,RIC_TRACE_HARQ,.2f);ric_trace_finish(&r);
 ric_trace_clear();r=ric_trace_begin(0,3,1,2,1,2,0,0,FLT_MAX);ric_trace_finish(&r);
 ric_trace_policy(42,ric_trace_now_ns(),0,0); // repeated echoed ran_index gets a new local sequence
 if(!std::strcmp(argv[1],"stress")) {
   std::vector<std::thread> producers;
   for(int i=0;i<4;i++)producers.emplace_back([i]{for(int j=0;j<20000;j++){
     ric_trace_receive(j%2==0);
     auto rec=ric_trace_begin(0,i+1,0,0,0,0,0,0,.25f);ric_trace_finish(&rec);
   }});
   for(auto &p:producers)p.join();
 }
 if(!std::strcmp(argv[1],"auto_stop"))return 0; // process-exit destructor must drain safely
 ric_trace_stop();assert(!ric_trace_enabled());
 if(!std::strcmp(argv[1],"io_fail"))assert(io_errors>0);
 else assert(io_errors==0);
 if(!std::strcmp(argv[1],"stress"))assert(queued+dropped==80009);
}
'''
with tempfile.TemporaryDirectory(prefix='oai-trace-test-') as tmp:
    tmp=Path(tmp);c=tmp/'test.cpp';c.write_text(code);exe=tmp/'test'
    subprocess.run(['c++','-std=c++17','-O2','-g','-Wall','-Wextra','-Werror','-fsanitize=undefined','-fno-sanitize-recover=all','-pthread','-I'+str(er),str(c),'-o',str(exe)],check=True)
    for mode in ['queue','disabled','open_fail','normal','stress','auto_stop','io_fail']:
        file=tmp/(mode+'.csv')
        target='/dev/full' if mode=='io_fail' else str(tmp/'missing'/'file') if mode=='open_fail' else str(file)
        subprocess.run([str(exe),mode,target],check=True,timeout=30)
        if mode in ['queue','disabled','open_fail','io_fail']:continue
        rows=list(csv.DictReader(x for x in file.read_text().splitlines() if not x.startswith('#')))
        assert all(None not in r for r in rows)
        records=[r for r in rows if r['kind']!='4'];stats=rows[-1]
        assert stats['kind']=='4' and int(stats['queued'])==len(records) and stats['io_errors']=='0'
        assert int(stats['policy_receive_attempts']) == (80003 if mode=='stress' else 3)
        assert int(stats['policy_receive_empty']) == (40002 if mode=='stress' else 2)
        assert int(stats['policy_receive_messages']) == (40001 if mode=='stress' else 1)
        if mode=='stress':assert int(stats['queued'])+int(stats['dropped'])==80009
        else:
            assert stats['dropped']=='0'
            assert [r['policy_seq'] for r in records if r['kind']=='1']==['1','2']
            grant=next(r for r in records if r['stage']=='9')
            assert grant['target_frame']=='0' and grant['target_slot']=='2' and grant['quota']=='100'
            assert grant['cap']=='30' and grant['sizing_cap']=='24' and grant['prbs']=='20' and grant['tbs']=='1000'
            zero=next(r for r in records if r['kind']=='3' and r['weight']=='0')
            assert zero['weight_present']=='1' and zero['quota']=='0' and zero['prbs']=='0'
            fallback=next(r for r in records if r['kind']=='3' and r['weight_present']=='0')
            assert fallback['policy_seq']=='0' and fallback['receive_ns']=='0' and fallback['quota']=='-1'
            ret=next(r for r in records if r['retransmission']=='1');assert ret['exemption']=='1' and ret['quota']=='-1'
print('Trace FIFO, overflow/contention drops, concurrent producers, CSV, file errors, shutdown and unchanged allocation logic: PASS')
