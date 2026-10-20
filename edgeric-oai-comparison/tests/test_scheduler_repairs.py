#!/usr/bin/env python3
"""Compile exact production helpers/retransmission functions with boundary stubs.
No copied allocation model: source functions and real OAI TBS are extracted on each run.
External CCE/PUCCH/FAPI/config calls are deterministic test doubles; this is not OTA.
"""
from pathlib import Path
import subprocess, tempfile, os, csv
ROOT=Path(__file__).resolve().parents[2]/'gnb-core/ext/openairinterface5g'
MAC=ROOT/'openair2/LAYER2/NR_MAC_gNB'
def function(text, name):
    import re
    match=re.search(r'^(?:static )?(?:bool|int|uint32_t|void) '+name+r'\(',text,re.M)
    assert match, name
    start=match.start(); brace=text.index('{',start); depth=1; end=brace+1
    while depth:
        depth += (text[end]=='{')-(text[end]=='}'); end+=1
    return text[start:end]+'\n'
prim=(MAC/'gNB_scheduler_primitives.c').read_text()
dl=(MAC/'gNB_scheduler_dlsch.c').read_text(); ul=(MAC/'gNB_scheduler_ulsch.c').read_text()
tbs=(ROOT/'openair2/LAYER2/NR_MAC_COMMON/nr_compute_tbs_common.c').read_text()
pre=r'''
#include <assert.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include "scheduler_allocation.h"
#include "scheduler_trace.h"
#define min(a,b) ((a)<(b)?(a):(b))
#define max(a,b) ((a)>(b)?(a):(b))
#define LOG_D(...) ((void)0)
#define AssertFatal(c,...) assert(c)
#define DevAssert(c) assert(c)
#define NR_PUSCH_Config__transformPrecoder_enabled 0
#define NR_MAX_PDSCH_TBS 3824
#define INDEX_MAX_TBS_TABLE 93
#define CEILIDIV(a,b) ((a+b-1)/b)
#define ROUNDIDIV(a,b) (((a<<1)+b)/(b<<1))
#define BLER_UPDATE_FRAME 10
#define BLER_FILTER 0.9f
#define MAX_FRAME_NUMBER 1024
#define TYPE_C_RNTI_ 0
typedef int rnti_t;
typedef int frame_t;
typedef int slot_t;
typedef int module_id_t;
static void *agent;
static float policy=FLT_MAX;
static int proposal=INT_MAX;
static float ric_get_weights(void *a,int r) { return policy; }
static float ric_get_ul_weights(void *a,int r) { return policy; }
static int ric_get_mcs(void *a,int r) { return proposal; }
typedef struct { int max_mcs,min_mcs; float lower,upper; } NR_bler_options_t;
typedef struct { unsigned rounds[4]; } NR_mac_dir_stats_t;
typedef struct { int last_frame,mcs; float bler; unsigned rounds[4]; } NR_bler_stats_t;
'''
source=pre+tbs[tbs.index('static const uint16_t Tbstable_nr'):tbs.index('static const uint16_t NPRB_LBRM')]+function(tbs,'nr_compute_tbs')
for name in ['multiple_2_3_5','nr_find_nb_rb','nr_find_nb_rb_new','get_mcs_from_bler_new']:
    source+=function(prim,name)
source+=(Path(__file__).with_name('retransmission_stubs.h')).read_text()
source+=function(dl,'allocate_dl_retransmission')+function(ul,'allocate_ul_retransmission')
# Execute the actual new-data cap and rejection blocks with beam cleanup counted.
newdata = r'''
static int cleanups;
#define reset_beam_status(...) (cleanups++)
#define ric_get_ul_weights(a,r) policy
static int dl_cap_case(int physical, int budget, int reference, float weight, bool ta)
{
  ric_trace_record trace={0};
  int frame=ta?100:1; int beam_index=0; (void)beam_index;
  struct {int idx;} beam={0}; int n_rb_sched[]={budget},rb_total_num[]={reference};
  struct {int ta_frame;} ctrl={0},*sched_ctrl=&ctrl;
  int entries[2]={0};int *iterator=entries; const int min_rbSize=5,rnti=1;
  uint16_t max_rbSize=physical; policy=weight; cleanups=0;
  while(iterator==entries) {
'''
a=dl.index('    max_rbSize = min(max_rbSize, n_rb_sched[beam.idx]);')
b=dl.index('    sched_pdsch->dmrs_parms =',a)
newdata+=dl[a:b]+r'''
    return max_rbSize;
  }
  return 0;
}
static int ul_cap_case(int physical,int budget,int reference,float weight,bool inactive,bool tp,int minimum,int power_cap)
{
 ric_trace_record trace={0}; struct {int mcs;} sched={0};
 struct {int idx;} beam={0};int n_rb_sched[]={budget},ul_rb_budget[]={reference};
 struct {int transform_precoding;} bwp={tp?0:1},*current_BWP=&bwp;
 struct {bool sched_inactive;struct {int rnti;} *UE;} entries[2]={{.sched_inactive=inactive}},*iterator=entries;
 struct {int bwpSize,bwpStart;} bi={100,20};int rbStart=10,min_rb=minimum;
 uint16_t available_rb=min(physical,budget);policy=weight;cleanups=0;
 while(iterator==entries) {
'''
a=ul.index('    /* Cap known-data grants;');b=ul.index('    int nrOfLayers =',a)
newdata+=ul[a:b]+r'''
    // Simulated power-adjustment output; test the production post-power rounding.
    available_rb=max(min_grant_rb,min(available_rb,power_cap));
'''
a=ul.index('    /* Round down after power adjustment');b=ul.index('    if (sched.mcs <',a)
newdata+=ul[a:b]+r'''
    assert(available_rb>=min_grant_rb && available_rb<=physical && available_rb<=budget);
    return available_rb;
 }
 return 0;
}
static void newdata_tests(void) {
 assert(dl_cap_case(30,20,100,.8f,false)==20);
 assert(dl_cap_case(30,20,100,FLT_MAX,false)==20);
 assert(dl_cap_case(30,20,100,0,false)==0 && cleanups==1);
 assert(dl_cap_case(30,20,100,0,true)==20); // TA exemption, still physically bounded
 assert(dl_cap_case(3,20,100,1,true)==0 && cleanups==1);
 assert(ul_cap_case(30,20,100,.8f,false,false,5,100)==20);
 assert(ul_cap_case(30,20,100,FLT_MAX,false,false,5,100)==20);
 assert(ul_cap_case(30,20,100,0,false,false,5,100)==0 && cleanups==2);
 assert(ul_cap_case(30,20,100,0,true,false,5,100)==5);
 assert(ul_cap_case(30,20,100,0,true,true,7,100)==8);
 assert(ul_cap_case(30,20,100,FLT_MAX,false,true,7,14)==12);
 assert(ul_cap_case(30,7,100,FLT_MAX,false,true,7,100)==0 && cleanups==2);
 assert(ul_cap_case(0,20,100,FLT_MAX,false,false,5,100)==0 && cleanups==2);
}
'''
source+=function(ul,'ul_weighted_prb_count_valid')+newdata
source+=(Path(__file__).with_name('scheduler_cases.c')).read_text().replace('int main(void){', 'int main(void){ric_trace_start();').replace('puts("sizing,', 'ric_trace_stop();puts("sizing,')
# Structural integration guards complement executed helper/retx tests.
assert dl.index('nr_grant_valid',dl.index('static void pf_dl')) < dl.index('nr_acknack_scheduling',dl.index('static void pf_dl'))
assert ul.index('nr_grant_valid',ul.index('static int  pf_ul')) < ul.index('post_process_ulsch(nrmac, pp_pusch, iterator->UE',ul.index('static int  pf_ul'))
assert '*n_rb_sched -= new_sched.rbSize;' in ul
assert 'pusch_snrx10 / 10.0f' in dl and 'tbs * 1.0000001' not in dl
assert dl.count('ric_set_dl_buffer(')==1 and 'ric_set_dl_buffer(agent, UE->rnti, sched_ctrl->num_total_bytes)' in dl
with tempfile.TemporaryDirectory(prefix='oai-scheduler-test-') as tmp:
    c=Path(tmp)/'test.c'; c.write_text(source)
    er=ROOT/'executables/edgeric'
    trace_obj=Path(tmp)/'trace.o'
    subprocess.run(['c++','-std=c++17','-O1','-g','-fsanitize=undefined','-pthread','-c',str(er/'scheduler_trace.cpp'),'-o',str(trace_obj)],check=True)
    for mode in ['normal','debug']:
        exe=Path(tmp)/mode; obj=Path(tmp)/(mode+'.o')
        cmd=['cc','-std=gnu11','-O1','-g','-Wall','-Werror','-Wno-unused-variable','-Wno-unused-function','-fsanitize=undefined','-fno-sanitize-recover=all','-I'+str(MAC),'-I'+str(er),'-c',str(c),'-o',str(obj)]
        if mode=='debug':cmd+=['-DNR_SCHEDULER_ALLOCATION_DEBUG']
        subprocess.run(cmd,check=True)
        subprocess.run(['c++','-fsanitize=undefined','-pthread',str(obj),str(trace_obj),'-lm','-o',str(exe)],check=True)
        env=os.environ.copy();env.pop('OAI_SCHED_TRACE_FILE',None)
        subprocess.run([str(exe)],check=True,timeout=30,env=env)
        log=Path(tmp)/(mode+'.csv');env['OAI_SCHED_TRACE_FILE']=str(log)
        subprocess.run([str(exe)],check=True,timeout=30,env=env)
        rows=list(csv.DictReader(x for x in log.read_text().splitlines() if not x.startswith('#')))
        decisions=[r for r in rows if r['kind']=='3']
        assert decisions and any(r['stage']=='9' and int(r['prbs'])>0 for r in decisions)
        assert any(r['stage']!='9' and r['prbs']=='0' for r in decisions)
        assert any(r['direction']=='1' and r['target_frame']=='0' and r['target_slot']=='2' for r in decisions)
print('Production scheduler tests passed: normal/debug, tracing off/on, UBSan.')
