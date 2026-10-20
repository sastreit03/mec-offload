#!/usr/bin/env python3
"""Extract actual receiver/parser/wrapper code; fake only ZMQ receive + protobuf transport.
Run on host, without opening sockets. Does not certify wire delivery.
"""
from pathlib import Path
import re, subprocess, tempfile, os, csv
root=Path(__file__).resolve().parents[2]/'gnb-core/ext/openairinterface5g/executables/edgeric'
s=(root/'edgeric.cpp').read_text();w=(root/'wrapper.cpp').read_text()
def extract(text,name):
    m=re.search(r'^\s*(?:static )?(?:bool|void|int|float) '+re.escape(name)+r'\(',text,re.M);assert m,name
    a=m.start();b=text.index('{',a);d=1;e=b+1
    while d:d+=(text[e]=='{')-(text[e]=='}');e+=1
    return text[a:e]+'\n'
code=r'''
#include <map>
#include "scheduler_trace.h"
#include <vector>
#include <optional>
#include <cmath>
#include <limits>
#include <iostream>
#include <cassert>
#include <cstdint>
#include <cfloat>
#include <climits>
namespace google::protobuf {template<class T> struct RepeatedField {std::vector<T> v;int size() const{return v.size();}T Get(int i)const{return v.at(i);}};}
static bool has_message=true,parse_ok=true;
static google::protobuf::RepeatedField<float> dl,ul,mcs;
namespace zmq { struct message_t {void *data(){return nullptr;}int size(){return 1;}};using recv_result_t=std::optional<int>;enum class recv_flags {dontwait};struct socket_t {recv_result_t recv(message_t &,recv_flags){return has_message?recv_result_t(1):std::nullopt;}};}
static zmq::socket_t subscriber_weights,subscriber_mcs;
struct SchedulingWeights {bool ParseFromArray(void*,int){return parse_ok;}auto &weights(){return dl;}auto &ul_weights(){return ul;}int ran_index(){return 7;}};
struct mcs_control {bool ParseFromArray(void*,int){return parse_ok;}int mcs_size(){return mcs.size();}float mcs(int i){return ::mcs.Get(i);}int ran_index(){return 7;}};
struct edgeric {
 static inline std::map<uint16_t,float> weights_recved,ul_weights_recved;
 static inline std::map<uint16_t,uint8_t> mcs_recved;
 static inline int er_ran_index_weights=0,er_ran_index_mcs=0;
 static void ensure_initialized(){}
 static void get_weights_from_er();static void get_mcs_from_er();
 std::optional<float> get_weights(uint16_t r){if(weights_recved.count(r))return weights_recved.at(r);return {};}
 std::optional<float> get_ul_weights(uint16_t r){if(ul_weights_recved.count(r))return ul_weights_recved.at(r);return {};}
 std::optional<uint8_t> get_mcs(uint16_t r){if(mcs_recved.count(r))return mcs_recved.at(r);return {};}
};
struct EdgeRIC {void *obj;};
'''
# Name collision in fake protobuf class resolved explicitly.
code=code.replace('int mcs_size(){return mcs.size();}', 'int mcs_size(){return ::mcs.size();}')
for name in ['parse_normalized_weights','edgeric::get_weights_from_er','edgeric::get_mcs_from_er']:code+=extract(s,name)
for name in ['ric_get_weights','ric_get_ul_weights','ric_get_mcs']:code+=extract(w,name)
code+=r'''
int main(){
 ric_trace_start();
 edgeric obj;EdgeRIC r{&obj};
 assert(ric_get_weights(nullptr,1)==FLT_MAX && ric_get_ul_weights(nullptr,1)==FLT_MAX && ric_get_mcs(nullptr,1)==INT_MAX);
 assert(ric_get_mcs(&r,1)==INT_MAX);
 dl.v={1,2,2,8};ul.v={1,7,2,3};obj.get_weights_from_er();
 assert(std::abs(ric_get_weights(&r,1)-.2f)<1e-6 && std::abs(ric_get_ul_weights(&r,1)-.7f)<1e-6);
 dl.v={1,0,2,1};obj.get_weights_from_er();assert(ric_get_weights(&r,1)==0 && ric_get_weights(&r,3)==FLT_MAX);
 for(auto invalid:std::vector<std::vector<float>>{{1},{1,-1},{1,0},{1,NAN},{0,1},{1.5f,1},{1,1,1,2}}){
   ul.v=invalid;obj.get_weights_from_er();assert(ric_get_weights(&r,1)==0 && ric_get_ul_weights(&r,1)==.7f);
 }
 parse_ok=false;obj.get_weights_from_er();assert(ric_get_ul_weights(&r,1)==.7f);parse_ok=true;
 ul.v={};dl.v={2,1};obj.get_weights_from_er();assert(ric_get_weights(&r,1)==FLT_MAX && ric_get_ul_weights(&r,1)==FLT_MAX);
 has_message=false;obj.get_weights_from_er();assert(ric_get_weights(&r,2)==FLT_MAX);has_message=true;
 for(float v: {0.f,27.f,28.f,29.f,255.f,256.f,-1.f,1.5f,NAN,INFINITY}){
   mcs.v={1,v};obj.get_mcs_from_er();assert(ric_get_mcs(&r,1)==((std::isfinite(v)&&v>=0&&v<=28&&std::trunc(v)==v)?int(v):INT_MAX));
 }
 mcs.v={1,4};obj.get_mcs_from_er();mcs.v={1};obj.get_mcs_from_er();assert(ric_get_mcs(&r,1)==4);
 has_message=false;obj.get_mcs_from_er();assert(ric_get_mcs(&r,1)==INT_MAX);
 ric_trace_stop();
 std::cout<<"Production adapter receive/parser/getter tests: PASS\n";
}
'''
with tempfile.TemporaryDirectory(prefix='oai-adapter-test-') as tmp:
    c=Path(tmp)/'test.cpp';c.write_text(code);exe=Path(tmp)/'test'
    subprocess.run(['c++','-std=c++17','-Wall','-Werror','-fsanitize=undefined','-fno-sanitize-recover=all','-pthread','-I'+str(root),str(c),str(root/'scheduler_trace.cpp'),'-o',str(exe)],check=True)
    log=Path(tmp)/'policy.csv';env=os.environ.copy();env['OAI_SCHED_TRACE_FILE']=str(log)
    subprocess.run([str(exe)],check=True,env=env)
    rows=list(csv.DictReader(x for x in log.read_text().splitlines() if not x.startswith('#')))
    accepted=[r for r in rows if r['kind']=='1']
    assert [r['policy_seq'] for r in accepted]==['1','2','3']
    assert [(r['dl_entries'],r['ul_entries']) for r in accepted]==[('2','2'),('2','2'),('1','0')]
    assert all(r['ran_index']=='7' and int(r['receive_ns'])>0 for r in accepted)
    first=[r for r in rows if r['kind']=='2' and r['policy_seq']=='1' and r['rnti']=='1']
    assert len(first)==2 and abs(float(first[0]['weight'])-.2)<1e-6 and abs(float(first[1]['weight'])-.7)<1e-6
    assert rows[-1]['dropped']=='0' 

    stats=rows[-1]
    assert stats['policy_receive_attempts']=='12'
    assert stats['policy_receive_messages']=='11' # includes invalid messages
    assert stats['policy_receive_empty']=='1' # excludes separate MCS receives
