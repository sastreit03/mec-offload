static uint32_t tb(int rb,int symbols) {return nr_compute_tbs(4,4900,rb,symbols,6,0,0,1)>>3;}
static void sizing_tests(void)
{
 assert(!multiple_2_3_5(0) && !multiple_2_3_5(-5));
 assert(multiple_2_3_5(1) && multiple_2_3_5(120) && !multiple_2_3_5(14));
 assert(nr_weight_cap(.8f,100,30)==30 && nr_weight_cap(.8f,100,20)==20);
 assert(nr_weight_cap(FLT_MAX,100,30)==30 && nr_weight_cap(0,100,30)==0);
 assert(nr_weight_cap(.2f,100,100)==20 && nr_weight_cap(NAN,100,30)==0);
 uint32_t t; uint16_t rb;
 for (int tp=0;tp<=1;tp++) for (int lo=0;lo<=80;lo++) for (int hi=0;hi<=80;hi++) {
   for (int which=0;which<4;which++) {
     unsigned bytes=which==0?1:which==1?tb(hi?hi:1,10):which==2?tb(hi?hi:1,10)+1:tb((lo+hi)/2+1,10);
     bool ok=nr_find_nb_rb(4,4900,tp,1,10,6,bytes,lo,hi,&t,&rb);
     int first=lo,last=hi;
     if (tp==0) {while(first<=last && !multiple_2_3_5(first))first++;while(last>=first && !multiple_2_3_5(last))last--;}
     if (!lo || first>last) {assert(!ok && t==0 && rb==0);continue;}
     assert(rb>=lo && rb<=hi && t==tb(rb,10));
     assert(tp || multiple_2_3_5(rb));
     assert(ok==(t>=bytes));
     if(!ok) assert(rb==last); // usable partial grant
   }
 }
 policy=0; assert(!nr_find_nb_rb_new(1,4,4900,1,1,10,6,100,5,30,100,&t,&rb) && !rb && !t);
 policy=.8f; nr_find_nb_rb_new(1,4,4900,1,1,10,6,99999,5,30,100,&t,&rb); assert(rb==30);
 policy=FLT_MAX; nr_find_nb_rb_new(1,4,4900,1,1,10,6,tb(17,10),5,30,100,&t,&rb); assert(rb>5 && rb<=30 && t>=tb(17,10));
 NR_bler_options_t bo={.max_mcs=27,.lower=.1f,.upper=.2f}; NR_mac_dir_stats_t stats={{10,0}};
 int proposals[]={INT_MAX,-1,0,27,28,255,256};
 for(unsigned i=0;i<sizeof(proposals)/sizeof(*proposals);i++) {
   NR_bler_stats_t bs={.last_frame=1,.mcs=10,.bler=.15f}; proposal=proposals[i];
   int m=get_mcs_from_bler_new(1,&bo,&stats,&bs,28,11);
   assert(m==((proposal>=0 && proposal<=27)?proposal:10));
   bs=(NR_bler_stats_t){.last_frame=1,.mcs=10,.bler=.15f};
   assert(get_mcs_from_bler_new(1,&bo,&stats,&bs,28,2)==10); // cadence unchanged
 }
}
static void map_tests(void)
{
 for(int bwp=0;bwp<=40;bwp+=20) for(int start=0;start<=20;start+=10) for(int count=1;count<=50;count+=7) {
   uint16_t map[160]={0};nr_mark_grant(map,bwp,start,count,0x70);
   for(int i=0;i<160;i++)assert(map[i]==((i>=bwp+start && i<bwp+start+count)?0x70:0));
   assert(!nr_grant_valid(map,bwp,100,start,count,count-1,100,0x70));
   assert(!nr_grant_valid(map,bwp,100,start,0,100,100,0x70));
   assert(!nr_grant_valid(map,bwp,100,99,2,100,100,0x70));
   assert(!nr_grant_valid(map,bwp,100,start,count,100,0,0x70));
#ifdef NR_SCHEDULER_ALLOCATION_DEBUG
   assert(!nr_grant_valid(map,bwp,100,start,count,100,100,0x70));
#endif
   assert(nr_grant_valid(map,bwp,100,start,count,100,100,0x80));
 }
 uint16_t map[100]={0};map[22]=4;
 assert(nr_free_span_start(map,20,20,5,4)==3);
 assert(nr_free_span_start(map,20,5,5,4)==-1);
 assert(nr_free_span_start(map,20,20,5,8)==0);
}
static void retransmission_tests(void)
{
 uint16_t map[160];gNB_MAC_INST mac={.frame_structure={20}};NR_ServingCellConfigCommon_t scc={0};
 mac.common_channels[0].ServingCellConfigCommon=&scc;mac.common_channels[0].vrb_map[0]=map;RC.nrmac[0]=&mac;
 coreset_t coreset={0};search_t search={0};typeof(*search.searchSpaceType) ss={0};search.searchSpaceType=&ss;
 for(int bwp=0;bwp<=20;bwp+=20) for(int changed=0;changed<=1;changed++) for(int fail=0;fail<4;fail++) {
   NR_UE_info_t ue={0};ue.UE_sched_ctrl.coreset=&coreset;ue.UE_sched_ctrl.search_space=&search;
   test_bwp=(bwp_info_t){bwp,60};test_tda=(NR_tda_info_t){changed?3:2,10,4,true};
   NR_sched_pdsch_t old={.nrOfLayers=1,.rbSize=10,.Qm=4,.R=4900,.tb_size=tb(10,10),.tda_info={2,10,4,true}};
   ue.UE_sched_ctrl.harq_processes[0].sched_pdsch=old;ue.UE_sched_ctrl.sched_pdsch.nrOfLayers=1;
   memset(map,0,sizeof(map));for(int i=0;i<20;i++)map[bwp+i]=0x3fff;
   int budget=fail==1?9:20;cce_result=fail==2?-1:0;pucch_result=fail==3?-1:0;cce_commits=pucch_calls=0;
   bool ok=allocate_dl_retransmission(0,1,1,&budget,&ue,0,0);
   assert(ok==(fail==0));assert(cce_commits==(fail==0));assert(budget==(fail==0?10:fail==1?9:20));
   assert(!memcmp(&old,&ue.UE_sched_ctrl.harq_processes[0].sched_pdsch,sizeof(old)));
   for(int i=0;i<160;i++)assert(map[i]==((i>=bwp && i<bwp+20)?0x3fff:(ok && i>=bwp+20 && i<bwp+30)?SL_to_bitmap(test_tda.startSymbolIndex,10):0));
   if(fail==1)assert(pucch_calls==0);
   NR_sched_pusch_t uold={.nrOfLayers=1,.rbSize=10,.Qm=4,.R=4900,.tb_size=tb(10,10),.tda_info={2,10,4,true},.ul_harq_pid=0};
   ue.current_UL_BWP.transform_precoding=1;ue.UE_sched_ctrl.ul_harq_processes[0].sched_pusch=uold;
   memset(map,0,sizeof(map));map[bwp+2]=0x3fff; // first candidate interrupted, later span usable
   if(changed)for(int i=0;i<3;i++)map[bwp+i]=0x3fff;
   budget=fail==1?9:20;cce_result=fail>=2?-1:0;ul_commits=cce_commits=0;
   post_process_pusch_t pp={1023,18};
   ok=allocate_ul_retransmission(&mac,&pp,map,&budget,0,&ue,0,&scc,1,&test_tda);
   assert(ok==(fail==0));assert(ul_commits==(fail==0) && cce_commits==(fail==0));
   assert(budget==(fail==0?10:fail==1?9:20));
   if(ok){assert(last_ul.rbStart==3 && last_ul.rbSize==10 && last_ul.tb_size==uold.tb_size);assert(last_ul.frame==0 && last_ul.slot==2);}
   for(int i=0;i<160;i++)assert(map[i]==((i==bwp+2 || (changed && i>=bwp && i<bwp+3))?0x3fff:(ok && i>=bwp+3 && i<bwp+13)?SL_to_bitmap(test_tda.startSymbolIndex,10):0));
   memset(map,0xff,sizeof(map));budget=60;cce_result=0;ul_commits=cce_commits=0;
   assert(!allocate_ul_retransmission(&mac,&pp,map,&budget,0,&ue,0,&scc,1,&test_tda));assert(budget==60 && !cce_commits && !ul_commits);
 }
}
int main(void){sizing_tests();newdata_tests();map_tests();retransmission_tests();puts("sizing, MCS, maps, and production DL/UL retransmissions: PASS");}
