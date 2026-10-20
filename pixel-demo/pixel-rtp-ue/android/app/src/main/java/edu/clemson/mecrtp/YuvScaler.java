package edu.clemson.mecrtp;
import java.nio.ByteBuffer;
/** Crop/resize raw YUV before encoding; respects row/pixel strides. */
public final class YuvScaler {
    private YuvScaler() { }
    public static void plane(ByteBuffer source,int sourceRow,int sourcePixel,ByteBuffer target,int targetRow,int targetPixel,
                             int left,int top,int sourceWidth,int sourceHeight,int targetWidth,int targetHeight) {
        int srcBase=source.position(),dstBase=target.position();
        for(int y=0;y<targetHeight;y++) {
            int sy=top+(int)((long)y*sourceHeight/targetHeight);
            for(int x=0;x<targetWidth;x++) {
                int sx=left+(int)((long)x*sourceWidth/targetWidth);
                target.put(dstBase+y*targetRow+x*targetPixel,source.get(srcBase+sy*sourceRow+sx*sourcePixel));
            }
        }
    }
    public static void validateCrop(int width,int height,int left,int right,int top,int bottom) {
        if(left<0||right<0||top<0||bottom<0||((left|right|top|bottom)&1)!=0||(long)left+right>=width||(long)top+bottom>=height)
            throw new IllegalArgumentException("Crop margins must be nonnegative/even and leave a nonempty capture region");
    }
}
