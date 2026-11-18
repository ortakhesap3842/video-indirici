package com.videoindirici.app;

import org.junit.Test;
import java.io.File;
import static org.junit.Assert.*;

public class AndroidMediaUnitTest {
    @Test
    public void subtitleBurnerRejectsMissingInput() throws Exception {
        File missing = new File("nonexistent_video_12345.mp4");
        File out = new File("out_12345.mp4");
        assertNull(SubtitleBurner.burnSubtitles(missing, null, out));
    }

    @Test
    public void androidDubberRejectsMissingInput() throws Exception {
        File missing = new File("nonexistent_video_12345.mp4");
        File dir = new File(".");
        assertNull(AndroidDubber.dubVideo(null, missing, null, "tr", dir));
    }
}
