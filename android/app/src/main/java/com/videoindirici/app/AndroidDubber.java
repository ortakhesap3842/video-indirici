package com.videoindirici.app;

import android.content.Context;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaExtractor;
import android.media.MediaFormat;
import android.media.MediaMuxer;
import android.os.Bundle;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

public class AndroidDubber {

    private static class CueAudio {
        long startMs;
        File wavFile;
        int sampleRate;
        int channels;
        int dataOffset;
        int dataSize;
    }

    public static File dubVideo(Context context, File inputMp4, JSONArray cuesJson, String langCode, File workDir) throws Exception {
        if (cuesJson == null || cuesJson.length() == 0 || !inputMp4.exists()) {
            return null;
        }

        final AtomicBoolean ttsReady = new AtomicBoolean(false);
        final CountDownLatch initLatch = new CountDownLatch(1);
        final TextToSpeech[] ttsHolder = new TextToSpeech[1];

        ttsHolder[0] = new TextToSpeech(context.getApplicationContext(), status -> {
            if (status == TextToSpeech.SUCCESS) {
                Locale loc = Locale.forLanguageTag("zh".equals(langCode) ? "zh-CN" : langCode);
                int res = ttsHolder[0].setLanguage(loc);
                ttsReady.set(res != TextToSpeech.LANG_MISSING_DATA && res != TextToSpeech.LANG_NOT_SUPPORTED);
            }
            initLatch.countDown();
        });

        initLatch.await(10, TimeUnit.SECONDS);
        TextToSpeech tts = ttsHolder[0];
        if (!ttsReady.get()) {
            try { tts.shutdown(); } catch (Exception ignored) {}
            return null;
        }

        List<CueAudio> synthesized = new ArrayList<>();
        try {
            int limit = Math.min(cuesJson.length(), 60);
            for (int i = 0; i < limit; i++) {
                JSONObject item = cuesJson.getJSONObject(i);
                long startMs = item.optLong("start_ms", 0);
                String text = item.optString("text", "").trim();
                if (text.isEmpty()) continue;

                File wavFile = new File(workDir, "tts_" + i + ".wav");
                CountDownLatch uttLatch = new CountDownLatch(1);
                String uttId = "utt_" + i;
                tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                    @Override public void onStart(String utteranceId) {}
                    @Override public void onDone(String utteranceId) {
                        if (uttId.equals(utteranceId)) uttLatch.countDown();
                    }
                    @Override public void onError(String utteranceId) {
                        if (uttId.equals(utteranceId)) uttLatch.countDown();
                    }
                });
                Bundle params = new Bundle();
                int queued = tts.synthesizeToFile(text, params, wavFile, uttId);
                if (queued == TextToSpeech.SUCCESS) {
                    uttLatch.await(15, TimeUnit.SECONDS);
                }
                if (wavFile.exists() && wavFile.length() > 44) {
                    CueAudio ca = parseWavHeader(wavFile, startMs);
                    if (ca != null) synthesized.add(ca);
                }
            }
        } finally {
            try { tts.shutdown(); } catch (Exception ignored) {}
        }

        if (synthesized.isEmpty()) {
            return null;
        }

        File outMp4 = new File(workDir, "dubbed_" + System.currentTimeMillis() + ".mp4");
        try {
            muxVideoWithTtsAudio(inputMp4, outMp4, synthesized);
            return outMp4.exists() && outMp4.length() > 0 ? outMp4 : null;
        } finally {
            for (CueAudio ca : synthesized) {
                ca.wavFile.delete();
            }
        }
    }

    private static CueAudio parseWavHeader(File wav, long startMs) {
        try (FileInputStream fis = new FileInputStream(wav)) {
            byte[] hdr = new byte[44];
            if (fis.read(hdr) < 44) return null;
            ByteBuffer bb = ByteBuffer.wrap(hdr).order(ByteOrder.LITTLE_ENDIAN);
            int channels = bb.getShort(22) & 0xFFFF;
            int sampleRate = bb.getInt(24);
            int bits = bb.getShort(34) & 0xFFFF;
            if (bits != 16 || sampleRate <= 0 || channels <= 0) return null;

            CueAudio ca = new CueAudio();
            ca.startMs = startMs;
            ca.wavFile = wav;
            ca.sampleRate = sampleRate;
            ca.channels = channels;
            ca.dataOffset = 44;
            ca.dataSize = (int) Math.max(0, wav.length() - 44);
            return ca;
        } catch (Exception e) {
            return null;
        }
    }

    private static void muxVideoWithTtsAudio(File inputMp4, File outMp4, List<CueAudio> cues) throws Exception {
        MediaExtractor extractor = new MediaExtractor();
        extractor.setDataSource(inputMp4.getAbsolutePath());

        int videoTrackIdx = -1;
        MediaFormat videoFormat = null;
        long durationUs = 0;
        for (int i = 0; i < extractor.getTrackCount(); i++) {
            MediaFormat fmt = extractor.getTrackFormat(i);
            String mime = fmt.getString(MediaFormat.KEY_MIME);
            if (mime != null && mime.startsWith("video/")) {
                videoTrackIdx = i;
                videoFormat = fmt;
                if (fmt.containsKey(MediaFormat.KEY_DURATION)) {
                    durationUs = fmt.getLong(MediaFormat.KEY_DURATION);
                }
                break;
            }
        }
        if (videoTrackIdx == -1 || videoFormat == null) {
            extractor.release();
            throw new Exception("Video akışı bulunamadı.");
        }

        int sampleRate = cues.get(0).sampleRate;
        int channels = cues.get(0).channels;
        MediaFormat aacFormat = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_AAC, sampleRate, channels);
        aacFormat.setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC);
        aacFormat.setInteger(MediaFormat.KEY_BIT_RATE, 96000);
        aacFormat.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 16384);

        MediaCodec encoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC);
        encoder.configure(aacFormat, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
        encoder.start();

        MediaMuxer muxer = new MediaMuxer(outMp4.getAbsolutePath(), MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4);
        int outVideoTrack = muxer.addTrack(videoFormat);
        int outAudioTrack = -1;
        boolean muxerStarted = false;

        // First encode all audio into temp list or start muxer once audio format is known
        List<byte[]> aacPackets = new ArrayList<>();
        List<Long> aacPts = new ArrayList<>();
        List<Integer> aacFlags = new ArrayList<>();

        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        long currentSample = 0;
        int bytesPerSample = 2 * channels;
        byte[] silenceBuf = new byte[4096];

        for (CueAudio ca : cues) {
            long targetSample = (ca.startMs * sampleRate) / 1000L;
            while (currentSample < targetSample) {
                long needSamples = Math.min(silenceBuf.length / bytesPerSample, targetSample - currentSample);
                int byteLen = (int) (needSamples * bytesPerSample);
                if (byteLen <= 0) break;
                long ptsUs = (currentSample * 1_000_000L) / sampleRate;
                feedEncoder(encoder, silenceBuf, byteLen, ptsUs, false);
                outAudioTrack = drainEncoder(encoder, info, muxer, outAudioTrack, aacPackets, aacPts, aacFlags);
                currentSample += needSamples;
            }

            try (FileInputStream fis = new FileInputStream(ca.wavFile)) {
                fis.skip(ca.dataOffset);
                byte[] buf = new byte[4096];
                int read;
                while ((read = fis.read(buf)) > 0) {
                    int aligned = read - (read % bytesPerSample);
                    if (aligned <= 0) continue;
                    long ptsUs = (currentSample * 1_000_000L) / sampleRate;
                    feedEncoder(encoder, buf, aligned, ptsUs, false);
                    outAudioTrack = drainEncoder(encoder, info, muxer, outAudioTrack, aacPackets, aacPts, aacFlags);
                    currentSample += aligned / bytesPerSample;
                }
            }
        }

        // Pad trailing silence up to video duration if needed
        long totalTargetSamples = (durationUs * sampleRate) / 1_000_000L;
        if (totalTargetSamples > currentSample) {
            long padEnd = Math.min(totalTargetSamples, currentSample + sampleRate * 300L);
            while (currentSample < padEnd) {
                long needSamples = Math.min(silenceBuf.length / bytesPerSample, padEnd - currentSample);
                int byteLen = (int) (needSamples * bytesPerSample);
                if (byteLen <= 0) break;
                long ptsUs = (currentSample * 1_000_000L) / sampleRate;
                feedEncoder(encoder, silenceBuf, byteLen, ptsUs, false);
                outAudioTrack = drainEncoder(encoder, info, muxer, outAudioTrack, aacPackets, aacPts, aacFlags);
                currentSample += needSamples;
            }
        }

        feedEncoder(encoder, silenceBuf, 0, (currentSample * 1_000_000L) / sampleRate, true);
        while (true) {
            int outIdx = encoder.dequeueOutputBuffer(info, 10000);
            if (outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                if (outAudioTrack == -1) {
                    outAudioTrack = muxer.addTrack(encoder.getOutputFormat());
                }
            } else if (outIdx >= 0) {
                ByteBuffer outBuf = encoder.getOutputBuffer(outIdx);
                if (outBuf != null && info.size > 0 && (info.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) == 0) {
                    byte[] copy = new byte[info.size];
                    outBuf.position(info.offset);
                    outBuf.get(copy);
                    aacPackets.add(copy);
                    aacPts.add(info.presentationTimeUs);
                    aacFlags.add(info.flags);
                }
                boolean eos = (info.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0;
                encoder.releaseOutputBuffer(outIdx, false);
                if (eos) break;
            } else if (outIdx == MediaCodec.INFO_TRY_AGAIN_LATER) {
                break;
            }
        }

        encoder.stop();
        encoder.release();

        if (outAudioTrack != -1) {
            muxer.start();
            muxerStarted = true;

            // Write video samples
            extractor.selectTrack(videoTrackIdx);
            ByteBuffer vBuf = ByteBuffer.allocate(1024 * 1024);
            MediaCodec.BufferInfo vInfo = new MediaCodec.BufferInfo();
            while (true) {
                int sampleSize = extractor.readSampleData(vBuf, 0);
                if (sampleSize < 0) break;
                vInfo.offset = 0;
                vInfo.size = sampleSize;
                vInfo.presentationTimeUs = extractor.getSampleTime();
                vInfo.flags = extractor.getSampleFlags();
                muxer.writeSampleData(outVideoTrack, vBuf, vInfo);
                extractor.advance();
            }

            // Write encoded AAC audio packets
            MediaCodec.BufferInfo aInfo = new MediaCodec.BufferInfo();
            for (int i = 0; i < aacPackets.size(); i++) {
                byte[] pkt = aacPackets.get(i);
                ByteBuffer ab = ByteBuffer.wrap(pkt);
                aInfo.offset = 0;
                aInfo.size = pkt.length;
                aInfo.presentationTimeUs = aacPts.get(i);
                aInfo.flags = aacFlags.get(i);
                muxer.writeSampleData(outAudioTrack, ab, aInfo);
            }
        }

        extractor.release();
        if (muxerStarted) {
            muxer.stop();
        }
        muxer.release();
    }

    private static void feedEncoder(MediaCodec encoder, byte[] data, int len, long ptsUs, boolean eos) {
        int inIdx = encoder.dequeueInputBuffer(10000);
        if (inIdx >= 0) {
            ByteBuffer inBuf = encoder.getInputBuffer(inIdx);
            if (inBuf != null) {
                inBuf.clear();
                if (len > 0) inBuf.put(data, 0, len);
            }
            encoder.queueInputBuffer(inIdx, 0, len, ptsUs, eos ? MediaCodec.BUFFER_FLAG_END_OF_STREAM : 0);
        }
    }

    private static int drainEncoder(
            MediaCodec encoder, MediaCodec.BufferInfo info, MediaMuxer muxer,
            int outAudioTrack, List<byte[]> packets, List<Long> pts, List<Integer> flags
    ) {
        while (true) {
            int outIdx = encoder.dequeueOutputBuffer(info, 0);
            if (outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                if (outAudioTrack == -1) {
                    outAudioTrack = muxer.addTrack(encoder.getOutputFormat());
                }
            } else if (outIdx >= 0) {
                ByteBuffer outBuf = encoder.getOutputBuffer(outIdx);
                if (outBuf != null && info.size > 0 && (info.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) == 0) {
                    byte[] copy = new byte[info.size];
                    outBuf.position(info.offset);
                    outBuf.get(copy);
                    packets.add(copy);
                    pts.add(info.presentationTimeUs);
                    flags.add(info.flags);
                }
                encoder.releaseOutputBuffer(outIdx, false);
            } else {
                break;
            }
        }
        return outAudioTrack;
    }
}
