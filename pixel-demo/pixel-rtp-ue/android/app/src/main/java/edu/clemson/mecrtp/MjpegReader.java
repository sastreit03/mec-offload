package edu.clemson.mecrtp;
import java.io.*;
import java.nio.charset.StandardCharsets;
/** Reads one JPEG from the server's existing Content-Length multipart endpoint. */
public final class MjpegReader {
    private MjpegReader() { }
    private static String line(InputStream input)throws IOException{
        ByteArrayOutputStream text=new ByteArrayOutputStream();int c;
        while((c=input.read())!=-1){if(c=='\n')break;if(c!='\r')text.write(c);
            if(text.size()>8192)throw new IOException("Oversized multipart header");}
        if(c==-1&&text.size()==0)return null;return text.toString(StandardCharsets.US_ASCII.name());
    }
    public static byte[] firstFrame(InputStream raw)throws IOException{
        InputStream input=new BufferedInputStream(raw);String text;int lines=0,length=-1;
        while((text=line(input))!=null&&!text.startsWith("--")){if(++lines>32)throw new IOException("No multipart boundary");}
        if(text==null)throw new EOFException("No JPEG frame yet");
        while((text=line(input))!=null&&!text.isEmpty()){
            if(++lines>64)throw new IOException("Oversized multipart headers");
            if(text.toLowerCase(java.util.Locale.ROOT).startsWith("content-length:")){
                try{length=Integer.parseInt(text.substring(text.indexOf(':')+1).trim());}catch(NumberFormatException e){throw new IOException("Invalid JPEG length",e);}
            }
        }
        if(length<2||length>8_000_000)throw new IOException("Invalid/missing JPEG Content-Length");
        byte[] jpeg=new byte[length];int offset=0;
        while(offset<length){int n=input.read(jpeg,offset,length-offset);if(n<0)throw new EOFException("Truncated JPEG");offset+=n;}
        if((jpeg[0]&255)!=255||(jpeg[1]&255)!=216)throw new IOException("Multipart payload is not JPEG");return jpeg;
    }
}
