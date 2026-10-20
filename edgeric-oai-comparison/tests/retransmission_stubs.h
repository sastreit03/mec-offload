/* Only external boundaries/types are stubbed; allocation functions come from production. */
typedef struct { int startSymbolIndex,nrOfSymbols,k2; bool valid_tda; } NR_tda_info_t;
typedef struct { int bwpStart,bwpSize; } bwp_info_t;
typedef struct { int N_PRB_DMRS,N_DMRS_SLOT; } NR_pdsch_dmrs_t;
typedef struct { int N_PRB_DMRS,num_dmrs_symb; } NR_pusch_dmrs_t;
typedef struct {
 int nrOfLayers,pm_index,time_domain_allocation,rbStart,pucch_allocation;
 int mcs; uint16_t rbSize,Qm,R; uint32_t tb_size;
 NR_tda_info_t tda_info; NR_pdsch_dmrs_t dmrs_parms; bwp_info_t bwp_info;
} NR_sched_pdsch_t;
typedef struct {
 int nrOfLayers,time_domain_allocation,rbStart,frame,slot,ul_harq_pid;
 int mcs; uint16_t rbSize,Qm,R; uint32_t tb_size;
 NR_tda_info_t tda_info; NR_pusch_dmrs_t dmrs_info; bwp_info_t bwp_info;
} NR_sched_pusch_t;
typedef struct { int transform_precoding; void *pucch_Config; } NR_UE_UL_BWP_t;
typedef struct { int unused; } NR_UE_DL_BWP_t;
typedef struct { int controlResourceSetId; } coreset_t;
typedef struct { struct {int present;} *searchSpaceType; } search_t;
typedef struct {
 struct {NR_sched_pdsch_t sched_pdsch;} harq_processes[1];
 struct {NR_sched_pusch_t sched_pusch;} ul_harq_processes[1];
 NR_sched_pdsch_t sched_pdsch;
 coreset_t *coreset; search_t *search_space;
 int aggregation_level,sched_pdcch,pdcch_cl_adjust,dl_cce_fail,ul_cce_fail,cce_index;
} NR_UE_sched_ctrl_t;
typedef struct {
 NR_UE_sched_ctrl_t UE_sched_ctrl; NR_UE_DL_BWP_t current_DL_BWP; NR_UE_UL_BWP_t current_UL_BWP;
 int rnti,UE_beam_index; struct {int downlinkHARQ_FeedbackDisabled_r17;} sc_info;
} NR_UE_info_t;
typedef struct { int dmrs_TypeA_Position; } NR_ServingCellConfigCommon_t;
typedef struct {
 struct { NR_ServingCellConfigCommon_t *ServingCellConfigCommon; uint16_t *vrb_map[1]; } common_channels[1];
 struct {int numb_slots_frame;} frame_structure;
} gNB_MAC_INST;
typedef struct {int frame,slot;} post_process_pusch_t;
static struct { gNB_MAC_INST *nrmac[1]; } RC;
static bwp_info_t test_bwp;
static NR_tda_info_t test_tda;
static int cce_result,pucch_result,cce_commits,pucch_calls,ul_commits;
static NR_sched_pusch_t last_ul;
static uint16_t SL_to_bitmap(int start,int size) { return ((1u<<size)-1)<<start; }
#define get_dl_tda(...) 1
#define get_dl_tda_info(...) test_tda
#define get_dl_dmrs_params(...) ((NR_pdsch_dmrs_t){6,1})
#define get_ul_dmrs_params(...) ((NR_pusch_dmrs_t){6,1})
#define get_pdsch_bwp_start_size(...) test_bwp
#define get_pusch_bwp_start_size(...) test_bwp
#define get_NTN_Koffset(...) 0
#define get_cce_index(...) cce_result
#define get_FeedbackDisabled(...) false
#define nr_get_pucch_resource(...) 0
#define nr_acknack_scheduling(...) (pucch_calls++,pucch_result)
#define fill_pdcch_vrb_map(...) (cce_commits++)
static void post_process_ulsch(gNB_MAC_INST *m,post_process_pusch_t *p,NR_UE_info_t *u,NR_sched_pusch_t *s) {last_ul=*s;ul_commits++;}
