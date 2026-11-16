package com.videoindirici.app;

import android.app.Activity;
import android.app.DownloadManager;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Intent;
import android.media.MediaScannerConnection;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.webkit.JavascriptInterface;
import android.webkit.URLUtil;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private WebView webView;
    private final ExecutorService executor = Executors.newSingleThreadExecutor();

    public interface ProgressCallback {
        void onProgress(double percent, String text);
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(this));
        }

        webView = new WebView(this);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setGeolocationEnabled(false);

        webView.setWebViewClient(new WebViewClient());
        webView.setWebChromeClient(new WebChromeClient());
        webView.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) -> {
            try {
                Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
                startActivity(i);
            } catch (Exception e) {
                DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
                String fileName = URLUtil.guessFileName(url, contentDisposition, mimeType);
                request.setMimeType(mimeType);
                request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fileName);
                DownloadManager dm = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
                if (dm != null) {
                    dm.enqueue(request);
                    Toast.makeText(this, "İndiriliyor: " + fileName, Toast.LENGTH_SHORT).show();
                }
            }
        });
        webView.addJavascriptInterface(new AndroidBridge(), "AndroidApp");
        webView.loadUrl("file:///android_asset/index.html");
        setContentView(webView);
    }

    private void evalJs(String js) {
        runOnUiThread(() -> webView.evaluateJavascript(js, null));
    }

    private String saveToPublicDownloads(File sourceFile, String fileName) throws Exception {
        String mime = fileName.toLowerCase().endsWith(".m4a") || fileName.toLowerCase().endsWith(".mp3")
                ? "audio/mp4"
                : "video/mp4";

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ContentValues values = new ContentValues();
            values.put(MediaStore.Downloads.DISPLAY_NAME, fileName);
            values.put(MediaStore.Downloads.MIME_TYPE, mime);
            values.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/VideoIndirici");

            ContentResolver resolver = getContentResolver();
            Uri uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
            if (uri == null) {
                throw new Exception("Downloads klasörüne yazılamadı.");
            }
            try (InputStream in = new FileInputStream(sourceFile);
                 OutputStream out = resolver.openOutputStream(uri)) {
                byte[] buf = new byte[8192];
                int len;
                while ((len = in.read(buf)) > 0) {
                    out.write(buf, 0, len);
                }
            }
            sourceFile.delete();
            return "İndirilenler/VideoIndirici/" + fileName;
        } else {
            File dir = new File(
                    Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS),
                    "VideoIndirici"
            );
            if (!dir.exists()) dir.mkdirs();
            File dest = new File(dir, fileName);
            try (InputStream in = new FileInputStream(sourceFile);
                 OutputStream out = new FileOutputStream(dest)) {
                byte[] buf = new byte[8192];
                int len;
                while ((len = in.read(buf)) > 0) {
                    out.write(buf, 0, len);
                }
            }
            sourceFile.delete();
            MediaScannerConnection.scanFile(this, new String[]{dest.getAbsolutePath()}, null, null);
            return dest.getAbsolutePath();
        }
    }

    public class AndroidBridge {
        @JavascriptInterface
        public void openExternal(String url) {
            if (url == null || !url.startsWith("https://github.com/ortakhesap3842/video-indirici/")) {
                return;
            }
            runOnUiThread(() -> {
                try {
                    Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
                    startActivity(i);
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this, "Bağlantı açılamadı: " + e.getMessage(), Toast.LENGTH_SHORT).show();
                }
            });
        }

        @JavascriptInterface
        public void search(String query, int limit) {
            executor.execute(() -> {
                Python py = Python.getInstance();
                PyObject mod = py.getModule("downloader");
                String json = mod.callAttr("search_youtube", query, limit).toString();
                evalJs("window.onSearchResults(" + JSONObject.quote(json) + ")");
            });
        }

        @JavascriptInterface
        public void getInfo(String url) {
            executor.execute(() -> {
                Python py = Python.getInstance();
                PyObject mod = py.getModule("downloader");
                String json = mod.callAttr("get_info", url).toString();
                evalJs("window.onVideoInfo(" + JSONObject.quote(json) + ")");
            });
        }

        @JavascriptInterface
        public void download(String url, String quality) {
            executor.execute(() -> {
                try {
                    File tempDir = new File(getCacheDir(), "dl_temp");
                    Python py = Python.getInstance();
                    PyObject mod = py.getModule("downloader");
                    ProgressCallback cb = (percent, text) ->
                            evalJs("window.onDownloadProgress(" + percent + ", " + JSONObject.quote(text) + ")");

                    String json = mod.callAttr("download_video", url, quality, tempDir.getAbsolutePath(), cb).toString();
                    JSONObject obj = new JSONObject(json);
                    if (obj.optBoolean("ok", false)) {
                        String srcPath = obj.getString("filepath");
                        String fileName = obj.getString("filename");
                        String savedPath = saveToPublicDownloads(new File(srcPath), fileName);
                        obj.put("saved_path", savedPath);
                    }
                    evalJs("window.onDownloadDone(" + JSONObject.quote(obj.toString()) + ")");
                } catch (Exception e) {
                    try {
                        JSONObject err = new JSONObject();
                        err.put("ok", false);
                        err.put("error", e.getMessage());
                        evalJs("window.onDownloadDone(" + JSONObject.quote(err.toString()) + ")");
                    } catch (Exception ignored) {}
                }
            });
        }
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
