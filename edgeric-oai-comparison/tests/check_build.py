#!/usr/bin/env python3
"""Run inside the disposable ran-build-cuda image; /work is a read-only OAI tree.
Uses existing Ninja compile/archive/link commands without CMake regeneration.
"""
import pathlib, shutil, subprocess, tempfile
src=pathlib.Path('/work'); dst=pathlib.Path('/oai-ran')
mac='openair2/LAYER2/NR_MAC_gNB/'
files=[mac+n for n in ['gNB_scheduler.c','gNB_scheduler_dlsch.c','gNB_scheduler_ulsch.c','gNB_scheduler_primitives.c','mac_proto.h','scheduler_allocation.h']]
files+=['executables/nr-softmodem.c']
files += [str(p.relative_to(src)) for p in (src/'executables/edgeric').iterdir() if p.suffix in ('.h','.cpp','.cc')]
for f in files: shutil.copy2(src/f,dst/f)
build=dst/'cmake_targets/ran_build/build'
commands=subprocess.check_output(['ninja','-t','commands','nr-softmodem'],cwd=build,text=True).splitlines()
selected=[]
trace_compile=next(c for c in commands if ' -c /oai-ran/executables/edgeric/wrapper.cpp' in c).replace('wrapper.cpp','scheduler_trace.cpp')
selected.append(trace_compile)
trace_object='executables/edgeric/CMakeFiles/edgericc.dir/scheduler_trace.cpp.o' 
for c in commands:
    if ' -c ' in c and any('/oai-ran/'+f in c for f in files if f.endswith(('.c','.cpp','.cc'))):
        selected.append(c)
    elif ' -c ' not in c and ('libL2_NR.a' in c or 'libedgericc.a' in c):
        if '/usr/bin/ar ' in c and 'libedgericc.a' in c:
            c=c.replace(' && /usr/bin/ranlib', ' '+trace_object+' && /usr/bin/ranlib')
        selected.append(c)
assert any(' -o nr-softmodem ' in c for c in selected)
for c in selected:
    print('RUN',c,flush=True)
    subprocess.run(c,cwd=build,shell=True,check=True)
deps=subprocess.check_output(['ldd','nr-softmodem'],cwd=build,text=True)
print(deps,flush=True)
assert 'not found' not in deps
# Other executable link commands are attempted only when their input artifacts exist.
for target in ['nr-uesoftmodem','lte-softmodem']:
    r=subprocess.run(['ninja','-t','commands',target],cwd=build,text=True,capture_output=True)
    if r.returncode:
        print('OTHER TARGET UNAVAILABLE',target,r.stderr)
        continue
    link=r.stdout.splitlines()[-1]
    print('OTHER TARGET LINK',target,flush=True)
    r=subprocess.run(link,cwd=build,shell=True)
    print('OTHER TARGET RESULT',target,r.returncode,flush=True)
print('gNB compile/link checks passed',flush=True)

# Fresh isolated CMake configuration checks the new source/Threads dependency too.
with tempfile.TemporaryDirectory(prefix='trace-cmake-') as tmp:
    t=pathlib.Path(tmp)
    (t/'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.16)\nproject(trace_link LANGUAGES CXX)\nset(CMAKE_CXX_STANDARD 17)\nadd_subdirectory(/work/executables/edgeric edgeric)\nadd_executable(trace_link main.cpp)\ntarget_link_libraries(trace_link PRIVATE edgericc)\n')
    (t/'main.cpp').write_text('#include <cstdint>\n#include "wrapper.h"\nint main() { EdgeRIC *r = ric_create(); ric_destroy(r); }\n')
    subprocess.run(['cmake','-S',str(t),'-B',str(t/'build'),'-G','Ninja'],check=True)
    subprocess.run(['cmake','--build',str(t/'build'),'--parallel','2'],check=True)
print('Fresh EdgeRIC CMake configure/build/link passed (smoke executable not run)',flush=True)
