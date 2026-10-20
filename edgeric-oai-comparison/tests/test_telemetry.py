#!/usr/bin/env python3
"""Execute production DL aggregation and metric expressions against boundary stubs."""
from pathlib import Path
import subprocess,tempfile,re
root=Path(__file__).resolve().parents[2]/'gnb-core/ext/openairinterface5g'
s=(root/'openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch.c').read_text()
a=s.index('static void nr_store_dlsch_buffer(');b=s.index('\nvoid finish_nr_dl_harq',a)
code=r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <stdio.h>
typedef int module_id_t;typedef int frame_t;typedef int slot_t;
typedef struct {int lcid,suspended;} nr_lc_config_t;
typedef struct {int bytes_in_buffer,pdus_in_buffer;} status_t;
typedef struct {nr_lc_config_t c[3];int n;} config_t;
typedef struct {int num_total_bytes,dl_pdus_total,transm_interrupt;config_t lc_config;status_t rlc_status[8];float pusch_snrx10;} NR_UE_sched_ctrl_t;
static struct {int rnti;NR_UE_sched_ctrl_t UE_sched_ctrl;} ue;
static status_t buffers[8];static unsigned reported,calls;static float snr,estimate;
#define UE_iterator(list,name) for(typeof(ue) *name=&ue;name;name=0)
#define seq_arr_size(c) ((c)->n)
#define seq_arr_at(c,i) (&(c)->c[i])
#undef seq_arr_at
#define seq_arr_at(cfg,i) (&(cfg)->c[i])
#define LOG_D(...) ((void)0)
#define DL_SCH_LCID_DTCH 4
#define nr_timer_is_active(t) (*(t))
#define nr_mac_rlc_status_ind(r,f,l) buffers[l]
#define ric_set_dl_buffer(a,r,b) (reported=(b),calls++)
#define ric_set_snr(a,r,v) (snr=(v))
#define ric_set_dl_tbs(a,r,v) (estimate=(v))
'''+s[a:b]+r'''
int main(){
 ue.UE_sched_ctrl.lc_config=(config_t){.c={{1,0},{2,0},{4,0}},.n=3};
 buffers[1]=(status_t){100,1};buffers[2]=(status_t){200,2};buffers[4]=(status_t){300,3};
 nr_store_dlsch_buffer(0,0,0);assert(reported==600 && calls==1 && ue.UE_sched_ctrl.dl_pdus_total==6);
 ue.UE_sched_ctrl.lc_config.c[1].suspended=1;ue.UE_sched_ctrl.transm_interrupt=1;
 nr_store_dlsch_buffer(0,0,0);assert(reported==100 && calls==2);
 memset(buffers,0,sizeof(buffers));nr_store_dlsch_buffer(0,0,0);assert(reported==0 && calls==3);
 ue.UE_sched_ctrl.lc_config.n=0;nr_store_dlsch_buffer(0,0,0);assert(reported==0 && calls==4);
 NR_UE_sched_ctrl_t *sched_ctrl=&ue.UE_sched_ctrl;
 for(int raw=-153;raw<=153;raw+=17){sched_ctrl->pusch_snrx10=raw;
'''
code+=re.search(r'    ric_set_snr\([^\n]+',s).group()+r'''
 assert(fabsf(snr-raw/10.0f)<1e-6);}
 uint32_t tbs=10000001;
'''+re.search(r'      ric_set_dl_tbs\([^\n]+',s).group()+r'''
 assert(estimate==(float)tbs);puts("Production DL buffer, SNR and PF metric tests: PASS");}
'''
with tempfile.TemporaryDirectory(prefix='oai-telemetry-test-') as tmp:
    c=Path(tmp)/'test.c';c.write_text(code);exe=Path(tmp)/'test'
    subprocess.run(['cc','-std=gnu11','-Wall','-Werror','-Wno-unused-variable','-fsanitize=undefined',str(c),'-lm','-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
