#include "yuv_transform.h"
#include <algorithm>
#include <cassert>
#include <iostream>
#include <stdexcept>
#include <vector>

// Independent reference: physically rotate into a temporary matrix, then scale.
void check(int rotation, int sp, int dp, int width, int height, int dw, int dh) {
  int left=2, top=2, dl=1, dt=1;
  int sr=(left+width+3)*sp, dr=(dl+dw+4)*dp;
  size_t slast=size_t(top+height-1)*sr+size_t(left+width-1)*sp+1;
  size_t dlast=size_t(dt+dh-1)*dr+size_t(dl+dw-1)*dp+1;
  std::vector<uint8_t> src(slast, 0xa5), dst(dlast+16, 0xcd), expected=dst;
  for (int y=0;y<height;++y) for(int x=0;x<width;++x)
    src[(top+y)*sr+(left+x)*sp]=uint8_t(x*7+y*13);
  int rw=rotation%180 ? height:width, rh=rotation%180 ? width:height;
  std::vector<uint8_t> rotated(rw*rh);
  for(int y=0;y<height;++y) for(int x=0;x<width;++x) {
    int rx=x, ry=y;
    if(rotation==90) {rx=height-1-y; ry=x;}
    if(rotation==180) {rx=width-1-x; ry=height-1-y;}
    if(rotation==270) {rx=y; ry=width-1-x;}
    rotated[ry*rw+rx]=src[(top+y)*sr+(left+x)*sp];
  }
  for(int y=0;y<dh;++y) for(int x=0;x<dw;++x)
    expected[(dt+y)*dr+(dl+x)*dp]=rotated[(y*rh/dh)*rw+x*rw/dw];
  mec::transform({src.data(),slast,sr,sp},left,top,width,height,
                 {dst.data(),dlast,dr,dp},dl,dt,dw,dh,rotation);
  assert(dst==expected); // Includes untouched row padding, chroma neighbors, and canary bytes.
  try {
    mec::transform({src.data(),slast-1,sr,sp},left,top,width,height,
                   {dst.data(),dlast,dr,dp},dl,dt,dw,dh,rotation);
    assert(false);
  } catch(const std::invalid_argument&) {}
  try {
    mec::transform({src.data(),slast,sr,sp},left,top,width,height,
                   {dst.data(),dlast-1,dr,dp},dl,dt,dw,dh,rotation);
    assert(false);
  } catch(const std::invalid_argument&) {}
}

int main() {
  for(int rotation:{0,90,180,270}) for(int sp:{1,2}) for(int dp:{1,2}) {
    for(auto size: {std::pair<int,int>{6,4}, {35,67}, {720,1280}}) {
      int rw=rotation%180 ? size.second:size.first;
      int rh=rotation%180 ? size.first:size.second;
      check(rotation,sp,dp,size.first,size.second,rw,rh);
      check(rotation,sp,dp,size.first,size.second,8,12);
      check(rotation,sp,dp,size.first,size.second,47,73);
    }
  }
  // U and V views alias alternating bytes of the same destination allocation.
  uint8_t u[]={1,2,3,4}, v[]={11,12,13,14}, uv[8]={};
  mec::transform({u,4,2,1},0,0,2,2,{uv,7,4,2},0,0,2,2,90);
  mec::transform({v,4,2,1},0,0,2,2,{uv+1,7,4,2},0,0,2,2,90);
  uint8_t expected[]={3,13,1,11,4,14,2,12};
  assert(std::equal(uv,uv+8,expected));
  try {
    mec::transform({u,4,2,1},0,0,2,2,{uv,8,4,2},0,0,2,2,45);
    assert(false);
  } catch(const std::invalid_argument&) {}
  std::cout << "Native YUV rotations, scaling, padding, bounds, and chroma conversion: PASS\n";
}
