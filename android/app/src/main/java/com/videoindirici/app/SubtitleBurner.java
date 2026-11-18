package com.videoindirici.app;

import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.RectF;
import android.graphics.SurfaceTexture;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaExtractor;
import android.media.MediaFormat;
import android.media.MediaMuxer;
import android.opengl.EGL14;
import android.opengl.EGLConfig;
import android.opengl.EGLContext;
import android.opengl.EGLDisplay;
import android.opengl.EGLExt;
import android.opengl.EGLSurface;
import android.opengl.GLES11Ext;
import android.opengl.GLES20;
import android.opengl.GLUtils;
import android.text.Layout;
import android.text.StaticLayout;
import android.text.TextPaint;
import android.view.Surface;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.FloatBuffer;
import java.util.ArrayList;
import java.util.List;

public class SubtitleBurner {

    private static class SubCue {
        long startMs;
        long endMs;
        String text;
    }

    private static final String VERTEX_SHADER =
            "attribute vec4 aPosition;\n" +
            "attribute vec4 aTexCoord;\n" +
            "varying vec2 vTexCoord;\n" +
            "void main() {\n" +
            "  gl_Position = aPosition;\n" +
            "  vTexCoord = aTexCoord.xy;\n" +
            "}\n";

    private static final String OES_FRAGMENT_SHADER =
            "#extension GL_OES_EGL_image_external : require\n" +
            "precision mediump float;\n" +
            "varying vec2 vTexCoord;\n" +
            "uniform samplerExternalOES sTexture;\n" +
            "void main() {\n" +
            "  gl_FragColor = texture2D(sTexture, vTexCoord);\n" +
            "}\n";

    private static final String TEX_FRAGMENT_SHADER =
            "precision mediump float;\n" +
            "varying vec2 vTexCoord;\n" +
            "uniform sampler2D sTexture;\n" +
            "void main() {\n" +
            "  gl_FragColor = texture2D(sTexture, vTexCoord);\n" +
            "}\n";

    private static final float[] FULL_QUAD = {
            -1f, -1f, 0f, 1f,
             1f, -1f, 1f, 1f,
            -1f,  1f, 0f, 0f,
             1f,  1f, 1f, 0f,
    };

    public static File burnSubtitles(File inputMp4, JSONArray cuesJson, File outMp4) throws Exception {
        if (cuesJson == null || cuesJson.length() == 0 || !inputMp4.exists()) {
            return null;
        }
        List<SubCue> cues = new ArrayList<>();
        for (int i = 0; i < cuesJson.length(); i++) {
            JSONObject obj = cuesJson.getJSONObject(i);
            String txt = obj.optString("text", "").trim();
            if (txt.isEmpty()) continue;
            SubCue c = new SubCue();
            c.startMs = obj.optLong("start_ms", 0);
            c.endMs = Math.max(c.startMs + 800, obj.optLong("end_ms", c.startMs + 2500));
            c.text = txt;
            cues.add(c);
        }
        if (cues.isEmpty()) return null;

        MediaExtractor vExt = new MediaExtractor();
        MediaExtractor aExt = new MediaExtractor();
        MediaCodec decoder = null;
        MediaCodec encoder = null;
        MediaMuxer muxer = null;
        Surface decoderSurface = null;
        SurfaceTexture surfaceTexture = null;
        EGLDisplay eglDisplay = EGL14.EGL_NO_DISPLAY;
        EGLContext eglContext = EGL14.EGL_NO_CONTEXT;
        EGLSurface eglSurface = EGL14.EGL_NO_SURFACE;
        boolean muxerStarted = false;

        try {
            vExt.setDataSource(inputMp4.getAbsolutePath());
            int vTrack = -1;
            MediaFormat vFmt = null;
            for (int i = 0; i < vExt.getTrackCount(); i++) {
                MediaFormat f = vExt.getTrackFormat(i);
                String mime = f.getString(MediaFormat.KEY_MIME);
                if (mime != null && mime.startsWith("video/")) {
                    vTrack = i;
                    vFmt = f;
                    break;
                }
            }
            if (vTrack == -1 || vFmt == null) return null;

            int width = vFmt.getInteger(MediaFormat.KEY_WIDTH);
            int height = vFmt.getInteger(MediaFormat.KEY_HEIGHT);
            if (width % 2 != 0) width--;
            if (height % 2 != 0) height--;
            int fps = vFmt.containsKey(MediaFormat.KEY_FRAME_RATE) ? vFmt.getInteger(MediaFormat.KEY_FRAME_RATE) : 30;
            if (fps <= 0 || fps > 60) fps = 30;

            aExt.setDataSource(inputMp4.getAbsolutePath());
            int aTrack = -1;
            MediaFormat aFmt = null;
            for (int i = 0; i < aExt.getTrackCount(); i++) {
                MediaFormat f = aExt.getTrackFormat(i);
                String mime = f.getString(MediaFormat.KEY_MIME);
                if (mime != null && mime.startsWith("audio/")) {
                    aTrack = i;
                    aFmt = f;
                    break;
                }
            }

            MediaFormat encFmt = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height);
            encFmt.setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
            encFmt.setInteger(MediaFormat.KEY_BIT_RATE, Math.max(2_000_000, Math.min(8_000_000, width * height * 4)));
            encFmt.setInteger(MediaFormat.KEY_FRAME_RATE, fps);
            encFmt.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1);

            encoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC);
            encoder.configure(encFmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
            Surface encoderSurface = encoder.createInputSurface();
            encoder.start();

            // Setup EGL14 on encoderSurface
            eglDisplay = EGL14.eglGetDisplay(EGL14.EGL_DEFAULT_DISPLAY);
            int[] vers = new int[2];
            EGL14.eglInitialize(eglDisplay, vers, 0, vers, 1);
            int[] cfgAttribs = {
                    EGL14.EGL_RED_SIZE, 8, EGL14.EGL_GREEN_SIZE, 8, EGL14.EGL_BLUE_SIZE, 8, EGL14.EGL_ALPHA_SIZE, 8,
                    EGL14.EGL_RENDERABLE_TYPE, EGL14.EGL_OPENGL_ES2_BIT, 0x3142, 1, EGL14.EGL_NONE
            };
            EGLConfig[] configs = new EGLConfig[1];
            int[] numCfg = new int[1];
            EGL14.eglChooseConfig(eglDisplay, cfgAttribs, 0, configs, 0, 1, numCfg, 0);
            eglContext = EGL14.eglCreateContext(eglDisplay, configs[0], EGL14.EGL_NO_CONTEXT,
                    new int[]{EGL14.EGL_CONTEXT_CLIENT_VERSION, 2, EGL14.EGL_NONE}, 0);
            eglSurface = EGL14.eglCreateWindowSurface(eglDisplay, configs[0], encoderSurface,
                    new int[]{EGL14.EGL_NONE}, 0);
            EGL14.eglMakeCurrent(eglDisplay, eglSurface, eglSurface, eglContext);

            int[] texIds = new int[2];
            GLES20.glGenTextures(2, texIds, 0);
            int oesTexId = texIds[0];
            int subTexId = texIds[1];

            GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, oesTexId);
            GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR);
            GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR);

            GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, subTexId);
            GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR);
            GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR);
            GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE);
            GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE);

            int oesProg = createProgram(VERTEX_SHADER, OES_FRAGMENT_SHADER);
            int texProg = createProgram(VERTEX_SHADER, TEX_FRAGMENT_SHADER);

            FloatBuffer quadBuf = ByteBuffer.allocateDirect(FULL_QUAD.length * 4)
                    .order(ByteOrder.nativeOrder()).asFloatBuffer();
            quadBuf.put(FULL_QUAD).position(0);

            surfaceTexture = new SurfaceTexture(oesTexId);
            final Object frameSync = new Object();
            final boolean[] frameAvail = new boolean[]{false};
            surfaceTexture.setOnFrameAvailableListener(st -> {
                synchronized (frameSync) {
                    frameAvail[0] = true;
                    frameSync.notifyAll();
                }
            });
            decoderSurface = new Surface(surfaceTexture);

            decoder = MediaCodec.createDecoderByType(vFmt.getString(MediaFormat.KEY_MIME));
            decoder.configure(vFmt, decoderSurface, null, 0);
            decoder.start();

            muxer = new MediaMuxer(outMp4.getAbsolutePath(), MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4);
            int outVideoTrack = -1;
            int outAudioTrack = aTrack != -1 && aFmt != null ? muxer.addTrack(aFmt) : -1;

            vExt.selectTrack(vTrack);
            MediaCodec.BufferInfo decInfo = new MediaCodec.BufferInfo();
            MediaCodec.BufferInfo encInfo = new MediaCodec.BufferInfo();
            boolean sawInputEos = false;
            boolean sawOutputEos = false;
            String lastSubText = "";
            int subW = width;
            int subH = Math.max(120, height / 4);
            Bitmap subBmp = Bitmap.createBitmap(subW, subH, Bitmap.Config.ARGB_8888);

            while (!sawOutputEos) {
                if (!sawInputEos) {
                    int inIdx = decoder.dequeueInputBuffer(5000);
                    if (inIdx >= 0) {
                        ByteBuffer inBuf = decoder.getInputBuffer(inIdx);
                        int sz = inBuf != null ? vExt.readSampleData(inBuf, 0) : -1;
                        if (sz < 0) {
                            decoder.queueInputBuffer(inIdx, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM);
                            sawInputEos = true;
                        } else {
                            decoder.queueInputBuffer(inIdx, 0, sz, vExt.getSampleTime(), 0);
                            vExt.advance();
                        }
                    }
                }

                int decIdx = decoder.dequeueOutputBuffer(decInfo, 5000);
                if (decIdx >= 0) {
                    boolean doRender = decInfo.size > 0;
                    decoder.releaseOutputBuffer(decIdx, doRender);
                    if (doRender) {
                        synchronized (frameSync) {
                            while (!frameAvail[0]) {
                                frameSync.wait(500);
                                if (!frameAvail[0]) break;
                            }
                            frameAvail[0] = false;
                        }
                        surfaceTexture.updateTexImage();
                        GLES20.glViewport(0, 0, width, height);
                        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT);
                        drawQuad(oesProg, oesTexId, GLES11Ext.GL_TEXTURE_EXTERNAL_OES, quadBuf, false);

                        long ptsMs = decInfo.presentationTimeUs / 1000L;
                        String activeTxt = findSubtitleAt(cues, ptsMs);
                        if (!activeTxt.isEmpty()) {
                            if (!activeTxt.equals(lastSubText)) {
                                drawSubtitleBitmap(subBmp, activeTxt);
                                GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, subTexId);
                                GLUtils.texImage2D(GLES20.GL_TEXTURE_2D, 0, subBmp, 0);
                                lastSubText = activeTxt;
                            }
                            GLES20.glEnable(GLES20.GL_BLEND);
                            GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE_MINUS_SRC_ALPHA);
                            GLES20.glViewport(0, 0, subW, subH);
                            drawQuad(texProg, subTexId, GLES20.GL_TEXTURE_2D, quadBuf, true);
                            GLES20.glDisable(GLES20.GL_BLEND);
                        }

                        EGLExt.eglPresentationTimeANDROID(eglDisplay, eglSurface, decInfo.presentationTimeUs * 1000L);
                        EGL14.eglSwapBuffers(eglDisplay, eglSurface);
                    }
                    if ((decInfo.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) {
                        encoder.signalEndOfInputStream();
                    }
                }

                while (true) {
                    int encIdx = encoder.dequeueOutputBuffer(encInfo, 0);
                    if (encIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                        if (!muxerStarted) {
                            outVideoTrack = muxer.addTrack(encoder.getOutputFormat());
                            muxer.start();
                            muxerStarted = true;
                        }
                    } else if (encIdx >= 0) {
                        ByteBuffer outBuf = encoder.getOutputBuffer(encIdx);
                        if (outBuf != null && encInfo.size > 0 && (encInfo.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) == 0 && muxerStarted) {
                            outBuf.position(encInfo.offset);
                            outBuf.limit(encInfo.offset + encInfo.size);
                            muxer.writeSampleData(outVideoTrack, outBuf, encInfo);
                        }
                        boolean eos = (encInfo.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0;
                        encoder.releaseOutputBuffer(encIdx, false);
                        if (eos) {
                            sawOutputEos = true;
                            break;
                        }
                    } else {
                        break;
                    }
                }
            }

            if (muxerStarted && outAudioTrack != -1) {
                aExt.selectTrack(aTrack);
                ByteBuffer aBuf = ByteBuffer.allocate(512 * 1024);
                MediaCodec.BufferInfo aInfo = new MediaCodec.BufferInfo();
                while (true) {
                    int sz = aExt.readSampleData(aBuf, 0);
                    if (sz < 0) break;
                    aInfo.offset = 0;
                    aInfo.size = sz;
                    aInfo.presentationTimeUs = aExt.getSampleTime();
                    aInfo.flags = aExt.getSampleFlags();
                    muxer.writeSampleData(outAudioTrack, aBuf, aInfo);
                    aExt.advance();
                }
            }
            subBmp.recycle();
            return outMp4.exists() && outMp4.length() > 0 ? outMp4 : null;
        } finally {
            try { if (decoder != null) { decoder.stop(); decoder.release(); } } catch (Exception ignored) {}
            try { if (encoder != null) { encoder.stop(); encoder.release(); } } catch (Exception ignored) {}
            try { if (decoderSurface != null) decoderSurface.release(); } catch (Exception ignored) {}
            try { if (surfaceTexture != null) surfaceTexture.release(); } catch (Exception ignored) {}
            try { vExt.release(); } catch (Exception ignored) {}
            try { aExt.release(); } catch (Exception ignored) {}
            if (muxer != null) {
                try { if (muxerStarted) muxer.stop(); } catch (Exception ignored) {}
                try { muxer.release(); } catch (Exception ignored) {}
            }
            if (eglDisplay != EGL14.EGL_NO_DISPLAY) {
                EGL14.eglMakeCurrent(eglDisplay, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_CONTEXT);
                if (eglSurface != EGL14.EGL_NO_SURFACE) EGL14.eglDestroySurface(eglDisplay, eglSurface);
                if (eglContext != EGL14.EGL_NO_CONTEXT) EGL14.eglDestroyContext(eglDisplay, eglContext);
                EGL14.eglTerminate(eglDisplay);
            }
        }
    }

    private static String findSubtitleAt(List<SubCue> cues, long ms) {
        for (SubCue c : cues) {
            if (ms >= c.startMs && ms <= c.endMs) return c.text;
        }
        return "";
    }

    private static void drawSubtitleBitmap(Bitmap bmp, String text) {
        bmp.eraseColor(Color.TRANSPARENT);
        Canvas canvas = new Canvas(bmp);
        TextPaint tp = new TextPaint(Paint.ANTI_ALIAS_FLAG);
        tp.setColor(Color.WHITE);
        tp.setTextSize(Math.max(24f, bmp.getHeight() * 0.22f));
        tp.setFakeBoldText(true);
        int maxW = (int) (bmp.getWidth() * 0.86f);
        StaticLayout layout = StaticLayout.Builder.obtain(text, 0, text.length(), tp, maxW)
                .setAlignment(Layout.Alignment.ALIGN_CENTER)
                .setMaxLines(3)
                .build();
        float totalH = layout.getHeight();
        float padX = 24f;
        float padY = 12f;
        float top = bmp.getHeight() - totalH - padY - 16f;
        float left = (bmp.getWidth() - maxW) / 2f;
        Paint bg = new Paint(Paint.ANTI_ALIAS_FLAG);
        bg.setColor(Color.argb(185, 10, 14, 26));
        canvas.drawRoundRect(new RectF(left - padX, top - padY, left + maxW + padX, top + totalH + padY), 16f, 16f, bg);
        canvas.save();
        canvas.translate(left, top);
        layout.draw(canvas);
        canvas.restore();
    }

    private static void drawQuad(int prog, int texId, int target, FloatBuffer quad, boolean flipY) {
        GLES20.glUseProgram(prog);
        int aPos = GLES20.glGetAttribLocation(prog, "aPosition");
        int aTex = GLES20.glGetAttribLocation(prog, "aTexCoord");
        quad.position(0);
        GLES20.glEnableVertexAttribArray(aPos);
        GLES20.glVertexAttribPointer(aPos, 2, GLES20.GL_FLOAT, false, 16, quad);
        quad.position(2);
        GLES20.glEnableVertexAttribArray(aTex);
        GLES20.glVertexAttribPointer(aTex, 2, GLES20.GL_FLOAT, false, 16, quad);
        GLES20.glActiveTexture(GLES20.GL_TEXTURE0);
        GLES20.glBindTexture(target, texId);
        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4);
    }

    private static int createProgram(String vsSrc, String fsSrc) {
        int vs = loadShader(GLES20.GL_VERTEX_SHADER, vsSrc);
        int fs = loadShader(GLES20.GL_FRAGMENT_SHADER, fsSrc);
        int prog = GLES20.glCreateProgram();
        GLES20.glAttachShader(prog, vs);
        GLES20.glAttachShader(prog, fs);
        GLES20.glLinkProgram(prog);
        return prog;
    }

    private static int loadShader(int type, String src) {
        int s = GLES20.glCreateShader(type);
        GLES20.glShaderSource(s, src);
        GLES20.glCompileShader(s);
        return s;
    }
}
