package edu.clemson.mecrtp;
import android.content.Context;
import android.graphics.ImageFormat;
import android.hardware.camera2.*;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.*;
import android.os.*;
import android.util.Range;
import android.util.Size;
import android.view.Surface;
import java.net.*;
import java.nio.ByteBuffer;
import java.util.*;
import org.json.JSONObject;

/** Raw camera -> sample -> crop/resize -> AVC -> RTP and local AVC decoder. */
public final class RtpCamera {
    public interface Listener { void event(JSONObject event); }
    private final Context context;
    private final Listener listener;
    private final HandlerThread worker=new HandlerThread("rtp-camera");
    private final Handler handler;
    private CameraDevice camera;
    private CameraCaptureSession session;
    private MediaCodec codec,decoder;
    private ImageReader reader;
    private Surface preview;
    private DatagramSocket socket;
    private H264Rtp rtp;
    private boolean active;
    private int generation,bitrate,width,height,fps,gop,left,right,top,bottom;
    private JSONObject video;
    private long previousBytes,previousFrames,previousPackets,previousNs;
    private long nextSampleNs,firstFrameNs,submitted,discardedRaw,lastIdrFrame,maxObservedGop;
    public RtpCamera(Context c,Listener l){context=c;listener=l;worker.start();handler=new Handler(worker.getLooper());}
    private void event(String type,String detail){try{listener.event(new JSONObject().put("type",type).put("detail",detail));}catch(Exception ignored){}}
    public void start(String host,JSONObject settings,Surface surface){handler.post(()->{
        stopInternal();preview=surface;int run=generation;
        try{
            width=settings.getInt("width");height=settings.getInt("height");fps=settings.getInt("fps");
            bitrate=settings.getInt("bitrate_kbps")*1000;gop=settings.getInt("gop_frames");
            left=settings.getInt("crop_left");right=settings.getInt("crop_right");top=settings.getInt("crop_top");bottom=settings.getInt("crop_bottom");
            CameraManager cm=(CameraManager)context.getSystemService(Context.CAMERA_SERVICE);
            String id=null;CameraCharacteristics characteristics=null;
            for(String candidate:cm.getCameraIdList()){
                CameraCharacteristics c=cm.getCameraCharacteristics(candidate);Integer facing=c.get(CameraCharacteristics.LENS_FACING);
                if(facing!=null&&facing==CameraCharacteristics.LENS_FACING_BACK){id=candidate;characteristics=c;break;}
            }
            if(id==null)throw new IllegalStateException("No rear camera");
            StreamConfigurationMap map=characteristics.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
            Size[] sizes=map==null?null:map.getOutputSizes(ImageFormat.YUV_420_888);Size capture=null;
            for(Size wanted:new Size[]{new Size(1280,720),new Size(640,480)})
                if(sizes!=null&&Arrays.asList(sizes).contains(wanted)){capture=wanted;break;}
            if(capture==null)throw new IllegalArgumentException("No supported 1280x720/640x480 YUV capture size");
            YuvScaler.validateCrop(capture.getWidth(),capture.getHeight(),left,right,top,bottom);
            Range<Integer>[] ranges=characteristics.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES);
            Range<Integer> selected=null;
            if(ranges!=null)for(Range<Integer> range:ranges)
                if(range.getUpper()>=30&&(selected==null||range.getUpper()<selected.getUpper()||
                    range.getUpper().equals(selected.getUpper())&&range.getLower()>selected.getLower()))selected=range;
            if(selected==null)throw new IllegalArgumentException("Camera has no capture range supporting 30 FPS");
            final Range<Integer> ae=selected;
            MediaFormat format=MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC,width,height);
            format.setInteger(MediaFormat.KEY_COLOR_FORMAT,MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible);
            format.setInteger(MediaFormat.KEY_BIT_RATE,bitrate);format.setInteger(MediaFormat.KEY_FRAME_RATE,fps);
            format.setFloat(MediaFormat.KEY_I_FRAME_INTERVAL,(float)gop/fps);
            format.setInteger(MediaFormat.KEY_PROFILE,MediaCodecInfo.CodecProfileLevel.AVCProfileBaseline);
            if(Build.VERSION.SDK_INT>=29)format.setInteger(MediaFormat.KEY_MAX_B_FRAMES,0);
            String encoder=null;
            for(MediaCodecInfo info:new MediaCodecList(MediaCodecList.REGULAR_CODECS).getCodecInfos()){
                if(!info.isEncoder()||(Build.VERSION.SDK_INT>=29&&!info.isHardwareAccelerated()))continue;
                try{
                    MediaCodecInfo.CodecCapabilities caps=info.getCapabilitiesForType(MediaFormat.MIMETYPE_VIDEO_AVC);
                    if(!caps.getVideoCapabilities().areSizeAndRateSupported(width,height,fps))continue;
                    boolean flexible=false;for(int color:caps.colorFormats)if(color==MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible)flexible=true;
                    if(!flexible)continue;
                    int mode=caps.getEncoderCapabilities().isBitrateModeSupported(MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR)
                        ?MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR:MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_VBR;
                    format.setInteger(MediaFormat.KEY_BITRATE_MODE,mode);
                    if(caps.isFormatSupported(format)){encoder=info.getName();break;}
                }catch(IllegalArgumentException ignored){}
            }
            if(encoder==null)throw new IllegalArgumentException("Hardware encoder rejected YUV/settings; try 640x360 at 5 FPS");
            socket=new DatagramSocket();socket.setSendBufferSize(256*1024);socket.connect(InetAddress.getByName(host),settings.getInt("video_port"));
            final DatagramSocket transport=socket;
            rtp=new H264Rtp(p->transport.send(new DatagramPacket(p,p.length)),96,settings.getInt("rtp_mtu"));
            codec=MediaCodec.createByCodecName(encoder);codec.configure(format,null,null,MediaCodec.CONFIGURE_FLAG_ENCODE);codec.start();
            video=new JSONObject(settings.toString()).put("codec","H264").put("rtp_payload_type",96).put("b_frames",0)
                .put("source","rear_camera").put("capture_width",capture.getWidth()).put("capture_height",capture.getHeight())
                .put("camera_ae_fps_range",ae.toString()).put("encoder",encoder).put("gop_semantics","target; verify observed IDR gap");
            reader=ImageReader.newInstance(capture.getWidth(),capture.getHeight(),ImageFormat.YUV_420_888,3);
            reader.setOnImageAvailableListener(r->sample(run,r),handler);
            active=true;nextSampleNs=firstFrameNs=submitted=discardedRaw=lastIdrFrame=maxObservedGop=0;
            final Surface input=reader.getSurface();
            cm.openCamera(id,new CameraDevice.StateCallback(){
                @Override public void onOpened(CameraDevice d){
                    if(run!=generation){d.close();return;}camera=d;
                    try{d.createCaptureSession(Collections.singletonList(input),new CameraCaptureSession.StateCallback(){
                        @Override public void onConfigured(CameraCaptureSession s){
                            if(run!=generation){s.close();return;}session=s;
                            try{
                                CaptureRequest.Builder request=camera.createCaptureRequest(CameraDevice.TEMPLATE_RECORD);request.addTarget(input);
                                request.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE,ae);
                                request.set(CaptureRequest.CONTROL_AF_MODE,CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO);
                                s.setRepeatingRequest(request.build(),null,handler);
                                listener.event(new JSONObject().put("type","stream_started").put("video",video));
                                previousBytes=previousFrames=previousPackets=0;previousNs=System.nanoTime();stats(run);pump(run);
                            }catch(Exception e){fail(run,e);}
                        }
                        @Override public void onConfigureFailed(CameraCaptureSession s){fail(run,new Exception("Camera configuration failed"));}
                    },handler);}catch(Exception e){fail(run,e);}
                }
                @Override public void onDisconnected(CameraDevice d){d.close();fail(run,new Exception("Camera disconnected"));}
                @Override public void onError(CameraDevice d,int error){d.close();fail(run,new Exception("Camera error "+error));}
            },handler);
        }catch(Exception e){fail(run,e);}
    });}
    private void sample(int run,ImageReader source){
        Image image=null;
        try{
            image=source.acquireLatestImage();if(image==null||run!=generation||!active)return;
            long stamp=image.getTimestamp(),interval=1_000_000_000L/fps;
            if(nextSampleNs>0&&stamp+1_000_000L<nextSampleNs){discardedRaw++;return;}
            int index=codec.dequeueInputBuffer(0);if(index<0){discardedRaw++;return;}
            ByteBuffer buffer=codec.getInputBuffer(index);int capacity=buffer==null?width*height*3/2:buffer.capacity();
            Image target=codec.getInputImage(index);
            if(target==null)throw new IllegalStateException("Encoder does not expose flexible YUV input images");
            Image.Plane[] src=image.getPlanes(),dst=target.getPlanes();
            int cropWidth=image.getWidth()-left-right,cropHeight=image.getHeight()-top-bottom;
            for(int p=0;p<3;p++){
                int scale=p==0?1:2;
                YuvScaler.plane(src[p].getBuffer(),src[p].getRowStride(),src[p].getPixelStride(),
                    dst[p].getBuffer(),dst[p].getRowStride(),dst[p].getPixelStride(),left/scale,top/scale,
                    cropWidth/scale,cropHeight/scale,width/scale,height/scale);
            }
            if(firstFrameNs==0)firstFrameNs=stamp;
            if(submitted%gop==0){Bundle b=new Bundle();b.putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME,0);codec.setParameters(b);}
            codec.queueInputBuffer(index,0,capacity,(stamp-firstFrameNs)/1000,0);submitted++;
            if(nextSampleNs==0)nextSampleNs=stamp+interval;
            else nextSampleNs+=Math.max(1,(stamp-nextSampleNs)/interval+1)*interval;
            drain();
        }catch(Exception e){fail(run,e);}finally{if(image!=null)image.close();}
    }
    private void pump(int run){handler.postDelayed(()->{
        if(run!=generation||!active)return;try{drain();pump(run);}catch(Exception e){fail(run,e);}
    },10);}
    private void drain()throws Exception{
        MediaCodec.BufferInfo info=new MediaCodec.BufferInfo();
        while(active){
            int index=codec.dequeueOutputBuffer(info,0);
            if(index==MediaCodec.INFO_TRY_AGAIN_LATER)break;
            if(index==MediaCodec.INFO_OUTPUT_FORMAT_CHANGED){
                MediaFormat format=codec.getOutputFormat();
                for(String key:new String[]{"csd-0","csd-1"}){
                    ByteBuffer b=format.getByteBuffer(key);if(b!=null){ByteBuffer d=b.duplicate();byte[] a=new byte[d.remaining()];d.get(a);rtp.configure(a);}
                }
                if(decoder!=null){decoder.stop();decoder.release();}
                MediaFormat decodeFormat=MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC,width,height);
                decodeFormat.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE,width*height*3/2);
                for(String key:new String[]{"csd-0","csd-1"}){ByteBuffer b=format.getByteBuffer(key);if(b!=null)decodeFormat.setByteBuffer(key,b.duplicate());}
                decoder=MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC);decoder.configure(decodeFormat,preview,null,0);decoder.start();continue;
            }
            if(index<0)continue;ByteBuffer data=codec.getOutputBuffer(index);
            if(data!=null&&info.size>0){
                data.position(info.offset);data.limit(info.offset+info.size);byte[] a=new byte[info.size];data.get(a);
                if((info.flags&MediaCodec.BUFFER_FLAG_CODEC_CONFIG)!=0)rtp.configure(a);
                else{
                    if((info.flags&MediaCodec.BUFFER_FLAG_KEY_FRAME)!=0){
                        if(lastIdrFrame>0)maxObservedGop=Math.max(maxObservedGop,rtp.accessUnits+1-lastIdrFrame);lastIdrFrame=rtp.accessUnits+1;
                    }
                    rtp.accessUnit(a,info.presentationTimeUs);decodeLocal(a,info.presentationTimeUs);
                }
            }
            codec.releaseOutputBuffer(index,false);
        }
        drainDecoder();
    }
    private void decodeLocal(byte[] bytes,long pts)throws Exception{
        if(decoder==null)return;drainDecoder();int index=decoder.dequeueInputBuffer(1000);if(index<0)return;
        ByteBuffer input=decoder.getInputBuffer(index);
        if(input==null||input.capacity()<bytes.length){decoder.queueInputBuffer(index,0,0,pts,0);return;}
        input.clear();input.put(bytes);decoder.queueInputBuffer(index,0,bytes.length,pts,0);
    }
    private void drainDecoder(){
        if(decoder==null)return;MediaCodec.BufferInfo info=new MediaCodec.BufferInfo();
        while(true){int index=decoder.dequeueOutputBuffer(info,0);if(index==MediaCodec.INFO_TRY_AGAIN_LATER)break;
            if(index>=0)decoder.releaseOutputBuffer(index,true);}
    }
    private void fail(int run,Exception e){if(run!=generation)return;event("native_error",e.toString());stopInternal();}
    private void stats(int run){handler.postDelayed(()->{
        if(run!=generation||!active)return;long now=System.nanoTime();double dt=(now-previousNs)/1e9;
        try{listener.event(new JSONObject().put("type","sender_stats").put("rtp_kbps",(rtp.bytes-previousBytes)*8/dt/1000)
            .put("encoded_fps",(rtp.accessUnits-previousFrames)/dt).put("packets_per_s",(rtp.packets-previousPackets)/dt)
            .put("packets",rtp.packets).put("bytes",rtp.bytes).put("discarded_raw_frames",discardedRaw)
            .put("max_observed_gop",maxObservedGop).put("target_bitrate_kbps",bitrate/1000));}catch(Exception ignored){}
        previousBytes=rtp.bytes;previousFrames=rtp.accessUnits;previousPackets=rtp.packets;previousNs=now;stats(run);
    },1000);}
    public void stop(){handler.post(this::stopInternal);}
    private void stopInternal(){
        generation++;boolean was=active;active=false;
        if(session!=null){session.close();session=null;}if(camera!=null){camera.close();camera=null;}if(reader!=null){reader.close();reader=null;}
        for(MediaCodec c:new MediaCodec[]{codec,decoder})if(c!=null){try{c.stop();}catch(Exception ignored){}c.release();}
        codec=decoder=null;if(preview!=null){preview.release();preview=null;}if(socket!=null){socket.close();socket=null;}
        if(was)event("stream_stopped","Media stopped");
    }
    public void destroy(){handler.post(()->{stopInternal();worker.quitSafely();});}
}
