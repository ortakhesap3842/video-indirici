package com.videoindirici.app;

import android.content.Context;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.junit.Test;
import org.junit.runner.RunWith;

import static org.junit.Assert.*;

@RunWith(AndroidJUnit4.class)
public class MediaPipelineInstrumentedTest {
    @Test
    public void hardwareAvcAndAacEncodersAvailable() throws Exception {
        Context ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        assertNotNull(ctx);

        MediaCodec avcEnc = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC);
        assertNotNull(avcEnc);
        avcEnc.release();

        MediaCodec aacEnc = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC);
        assertNotNull(aacEnc);
        aacEnc.release();
    }
}
