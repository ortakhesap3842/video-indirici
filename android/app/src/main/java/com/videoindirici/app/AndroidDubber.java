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
import android.speech.tts.Voice;

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

    private static float[] analyzeOriginalSpeaker(File inputMp4) {
        // Returns [pitchFactor, rateFactor, isFemaleFlag (0 or 1)]
        float[] profile = new float[]{1.0f, 1.0f, 0.0f};
        MediaExtractor ext = new MediaExtractor();
        MediaCodec dec = null;
        try {
            ext.setDataSource(inputMp4.getAbsolutePath());
            int audioIdx = -1;
            MediaFormat audioFmt = null;
            for (int i = 0; i < ext.getTrackCount(); i++) {
                MediaFormat f = ext.getTrackFormat(i);
                String mime = f.getString(MediaFormat.KEY_MIME);
                if (mime != null && mime.startsWith("audio/")) {
                    audioIdx = i;
                    audioFmt = f;
                    break;
                }
            }
            if (audioIdx == -1 || audioFmt == null) return profile;
            ext.selectTrack(audioIdx);
            ext.seekTo(2_000_000L, MediaExtractor.SEEK_TO_CLOSEST_SYNC);

            int sampleRate = audioFmt.containsKey(MediaFormat.KEY_SAMPLE_RATE)
                    ? audioFmt.getInteger(MediaFormat.KEY_SAMPLE_RATE) : 44100;
            int channels = audioFmt.containsKey(MediaFormat.KEY_CHANNEL_COUNT)
                    ? audioFmt.getInteger(MediaFormat.KEY_CHANNEL_COUNT) : 1;

            dec = MediaCodec.createDecoderByType(audioFmt.getString(MediaFormat.KEY_MIME));
            dec.configure(audioFmt, null, null, 0);
            dec.start();

            List<Short> mono = new ArrayList<>();
            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
            boolean sawInputEos = false;
            int targetSamples = sampleRate * 4;

            while (mono.size() < targetSamples) {
                if (!sawInputEos) {
                    int inIdx = dec.dequeueInputBuffer(5000);
                    if (inIdx >= 0) {
                        ByteBuffer inBuf = dec.getInputBuffer(inIdx);
                        int sz = inBuf != null ? ext.readSampleData(inBuf, 0) : -1;
                        if (sz < 0) {
                            dec.queueInputBuffer(inIdx, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM);
                            sawInputEos = true;
                        } else {
                            dec.queueInputBuffer(inIdx, 0, sz, ext.getSampleTime(), 0);
                            ext.advance();
                        }
                    }
                }
                int outIdx = dec.dequeueOutputBuffer(info, 5000);
                if (outIdx >= 0) {
                    ByteBuffer outBuf = dec.getOutputBuffer(outIdx);
                    if (outBuf != null && info.size > 0) {
                        outBuf.position(info.offset);
                        outBuf.limit(info.offset + info.size);
                        ByteBuffer le = outBuf.slice().order(ByteOrder.LITTLE_ENDIAN);
                        while (le.remaining() >= 2 * channels && mono.size() < targetSamples) {
                            short s = le.getShort();
                            for (int c = 1; c < channels; c++) le.getShort();
                            mono.add(s);
                        }
                    }
                    dec.releaseOutputBuffer(outIdx, false);
                    if ((info.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) break;
                } else if (outIdx == MediaCodec.INFO_TRY_AGAIN_LATER && sawInputEos) {
                    break;
                }
            }

            if (mono.size() >= sampleRate) {
                int minLag = Math.max(20, sampleRate / 280);
                int maxLag = Math.min(mono.size() / 2, sampleRate / 80);
                int bestLag = (minLag + maxLag) / 2;
                double bestCorr = -1;
                int limit = Math.min(mono.size() - maxLag, sampleRate * 2);
                for (int lag = minLag; lag <= maxLag; lag += 2) {
                    double corr = 0;
                    for (int i = 0; i < limit; i += 6) {
                        corr += (double) mono.get(i) * mono.get(i + lag);
                    }
                    if (corr > bestCorr) {
                        bestCorr = corr;
                        bestLag = lag;
                    }
                }
                double estHz = (double) sampleRate / bestLag;
                if (estHz >= 165.0) {
                    profile[2] = 1.0f;
                    profile[0] = estHz > 205.0 ? 1.08f : 1.02f;
                } else {
                    profile[2] = 0.0f;
                    profile[0] = estHz < 110.0 ? 0.90f : 0.96f;
                }
            }
        } catch (Exception ignored) {
        } finally {
            try { if (dec != null) { dec.stop(); dec.release(); } } catch (Exception ignored) {}
            try { ext.release(); } catch (Exception ignored) {}
        }
        return profile;
    }

    public static File dubVideo(Context context, File inputMp4, JSONArray cuesJson, String langCode, File workDir) throws Exception {
        if (cuesJson == null || cuesJson.length() == 0 || !inputMp4.exists()) {
            return null;
        }

        final float[] speaker = analyzeOriginalSpeaker(inputMp4);
        final AtomicBoolean ttsReady = new AtomicBoolean(false);
        final CountDownLatch initLatch = new CountDownLatch(1);
        final TextToSpeech[] ttsHolder = new TextToSpeech[1];

        ttsHolder[0] = new TextToSpeech(context.getApplicationContext(), status -> {
            if (status == TextToSpeech.SUCCESS) {
                Locale loc = Locale.forLanguageTag("zh".equals(langCode) ? "zh-CN" : langCode);
                int res = ttsHolder[0].setLanguage(loc);
                if (res != TextToSpeech.LANG_MISSING_DATA && res != TextToSpeech.LANG_NOT_SUPPORTED) {
                    try {
                        Voice bestVoice = null;
                        int bestScore = -1;
                        if (ttsHolder[0].getVoices() != null) {
                            for (Voice v : ttsHolder[0].getVoices()) {
                                if (!v.getLocale().getLanguage().equalsIgnoreCase(loc.getLanguage())) continue;
                                int score = v.getQuality();
                                if (v.isNetworkConnectionRequired()) score += 150;
                                String vName = v.getName().toLowerCase(Locale.US);
                                if (vName.contains("neural") || vName.contains("wavenet") || vName.contains("network")) score += 180;
                                boolean wantsFemale = speaker[2] > 0.5f;
                                if (wantsFemale && (vName.contains("female") || vName.contains("-f-") || vName.contains("emel"))) score += 100;
                                if (!wantsFemale && (vName.contains("male") || vName.contains("-m-") || vName.contains("ahmet"))) score += 100;
                                if (score > bestScore) {
                                    bestScore = score;
                                    bestVoice = v;
                                }
                            }
                        }
                        if (bestVoice != null) ttsHolder[0].setVoice(bestVoice);
                    } catch (Exception ignored) {}
                    ttsHolder[0].setPitch(speaker[0]);
                    ttsHolder[0].setSpeechRate(speaker[1]);
                    ttsReady.set(true);
                }
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
            int maxBuf = videoFormat.containsKey(MediaFormat.KEY_MAX_INPUT_SIZE)
                    ? Math.max(2 * 1024 * 1024, videoFormat.getInteger(MediaFormat.KEY_MAX_INPUT_SIZE))
                    : 2 * 1024 * 1024;
            ByteBuffer vBuf = ByteBuffer.allocate(maxBuf);
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

    public static File muxVideoAndAudio(File videoFile, File audioFile, File outMp4) throws Exception {
        MediaExtractor vExt = new MediaExtractor();
        MediaExtractor aExt = new MediaExtractor();
        MediaMuxer muxer = null;
        boolean started = false;
        try {
            vExt.setDataSource(videoFile.getAbsolutePath());
            aExt.setDataSource(audioFile.getAbsolutePath());
            int vIdx = -1;
            MediaFormat vFmt = null;
            for (int i = 0; i < vExt.getTrackCount(); i++) {
                MediaFormat f = vExt.getTrackFormat(i);
                String mime = f.getString(MediaFormat.KEY_MIME);
                if (mime != null && mime.startsWith("video/")) {
                    vIdx = i;
                    vFmt = f;
                    break;
                }
            }
            int aIdx = -1;
            MediaFormat aFmt = null;
            for (int i = 0; i < aExt.getTrackCount(); i++) {
                MediaFormat f = aExt.getTrackFormat(i);
                String mime = f.getString(MediaFormat.KEY_MIME);
                if (mime != null && mime.startsWith("audio/")) {
                    aIdx = i;
                    aFmt = f;
                    break;
                }
            }
            if (vIdx == -1 || aIdx == -1 || vFmt == null || aFmt == null) {
                throw new Exception("Video veya ses akışı bulunamadı.");
            }
            muxer = new MediaMuxer(outMp4.getAbsolutePath(), MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4);
            int outV = muxer.addTrack(vFmt);
            int outA = muxer.addTrack(aFmt);
            muxer.start();
            started = true;

            int maxV = vFmt.containsKey(MediaFormat.KEY_MAX_INPUT_SIZE)
                    ? Math.max(2 * 1024 * 1024, vFmt.getInteger(MediaFormat.KEY_MAX_INPUT_SIZE))
                    : 2 * 1024 * 1024;
            ByteBuffer buf = ByteBuffer.allocate(maxV);
            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();

            vExt.selectTrack(vIdx);
            while (true) {
                int sz = vExt.readSampleData(buf, 0);
                if (sz < 0) break;
                info.offset = 0;
                info.size = sz;
                info.presentationTimeUs = vExt.getSampleTime();
                info.flags = vExt.getSampleFlags();
                muxer.writeSampleData(outV, buf, info);
                vExt.advance();
            }

            aExt.selectTrack(aIdx);
            while (true) {
                int sz = aExt.readSampleData(buf, 0);
                if (sz < 0) break;
                info.offset = 0;
                info.size = sz;
                info.presentationTimeUs = aExt.getSampleTime();
                info.flags = aExt.getSampleFlags();
                muxer.writeSampleData(outA, buf, info);
                aExt.advance();
            }
            return outMp4.exists() && outMp4.length() > 0 ? outMp4 : null;
        } finally {
            try { vExt.release(); } catch (Exception ignored) {}
            try { aExt.release(); } catch (Exception ignored) {}
            if (muxer != null) {
                try { if (started) muxer.stop(); } catch (Exception ignored) {}
                try { muxer.release(); } catch (Exception ignored) {}
            }
        }
    }
}
