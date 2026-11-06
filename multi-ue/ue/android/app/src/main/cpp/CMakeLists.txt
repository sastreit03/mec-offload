cmake_minimum_required(VERSION 3.22)
project(mec_yuv LANGUAGES CXX)
add_library(mec_yuv SHARED native_yuv.cpp yuv_transform.cpp)
target_compile_features(mec_yuv PRIVATE cxx_std_17)
# Profiling uses debug APKs too: keep the pixel conversion optimized in all variants.
target_compile_options(mec_yuv PRIVATE -O3 -Wall -Wextra -Werror)
