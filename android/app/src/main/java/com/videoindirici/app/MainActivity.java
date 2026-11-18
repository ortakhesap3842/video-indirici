package com.videoindirici.app;

import android.app.Activity;
import android.app.DownloadManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageInstaller;
import android.media.MediaScannerConnection;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.provider.Settings;
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

import java.io.BufferedInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private static final String ACTION_INSTALL_STATUS = "com.videoindirici.app.INSTALL_STATUS";
    private WebView webView;
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final ExecutorService updateExecutor = Executors.newSingleThreadExecutor();
    private BroadcastReceiver installReceiver;

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

        installReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
                if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
                    Intent confirmIntent = intent.getParcelableExtra(Intent.EXTRA_INTENT);
                    if (confirmIntent != null) {
                        confirmIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                        startActivity(confirmIntent);
                    }
                    evalJs("window.onUpdateStatus && window.onUpdateStatus('installing', 'Yükleme onayı bekleniyor...')");
                } else if (status == PackageInstaller.STATUS_SUCCESS) {
                    evalJs("window.onUpdateStatus && window.onUpdateStatus('done', 'Güncelleme tamamlandı!')");
                } else {
                    String msg = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
                    evalJs("window.onUpdateStatus && window.onUpdateStatus('error', "
                            + JSONObject.quote("Yükleme başarısız: " + (msg != null ? msg : "Kod " + status)) + ")");
                }
            }
        };
        IntentFilter filter = new IntentFilter(ACTION_INSTALL_STATUS);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(installReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(installReceiver, filter);
        }
    }

    @Override
    protected void onDestroy() {
        if (installReceiver != null) {
            try {
                unregisterReceiver(installReceiver);
            } catch (Exception ignored) {}
            installReceiver = null;
        }
        super.onDestroy();
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
        public void installUpdate(String url) {
            if (url == null || !url.startsWith("https://github.com/ortakhesap3842/video-indirici/releases/")
                    || !url.toLowerCase().endsWith(".apk")) {
                evalJs("window.onUpdateStatus && window.onUpdateStatus('error', 'Geçersiz güncelleme adresi.')");
                return;
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !getPackageManager().canRequestPackageInstalls()) {
                evalJs("window.onUpdateStatus && window.onUpdateStatus('need_permission', 'Bilinmeyen kaynaklardan yükleme izni verin ve tekrar Güncelle\\'ye basın.')");
                runOnUiThread(() -> {
                    try {
                        Intent permIntent = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                                Uri.parse("package:" + getPackageName()));
                        startActivity(permIntent);
                    } catch (Exception e) {
                        Toast.makeText(MainActivity.this, "Ayarlar açılamadı: " + e.getMessage(), Toast.LENGTH_SHORT).show();
                    }
                });
                return;
            }

            updateExecutor.execute(() -> {
                File apkFile = new File(getCacheDir(), "update.apk");
                HttpURLConnection conn = null;
                try {
                    evalJs("window.onUpdateStatus && window.onUpdateStatus('downloading', 'Güncelleme indiriliyor...')");
                    String currentUrl = url;
                    boolean connected = false;
                    for (int hop = 0; hop < 5; hop++) {
                        if (!currentUrl.startsWith("https://")) {
                            throw new Exception("Güvenli olmayan yönlendirme engellendi.");
                        }
                        conn = (HttpURLConnection) new URL(currentUrl).openConnection();
                        conn.setInstanceFollowRedirects(false);
                        conn.setConnectTimeout(15000);
                        conn.setReadTimeout(60000);
                        conn.setRequestProperty("User-Agent", "VideoIndirici-Android");
                        int code = conn.getResponseCode();
                        if (code == HttpURLConnection.HTTP_MOVED_TEMP
                                || code == HttpURLConnection.HTTP_MOVED_PERM
                                || code == 307 || code == 308) {
                            String nextUrl = conn.getHeaderField("Location");
                            conn.disconnect();
                            conn = null;
                            if (nextUrl == null || nextUrl.isEmpty()) {
                                throw new Exception("Geçersiz yönlendirme.");
                            }
                            currentUrl = nextUrl;
                            continue;
                        }
                        if (code != HttpURLConnection.HTTP_OK) {
                            throw new Exception("İndirme hatası (HTTP " + code + ")");
                        }
                        connected = true;
                        break;
                    }
                    if (!connected || conn == null) {
                        throw new Exception("Güncelleme sunucusuna ulaşılamadı.");
                    }

                    long total = conn.getContentLengthLong();
                    long downloaded = 0;
                    long lastUiUpdate = 0;
                    try (InputStream in = new BufferedInputStream(conn.getInputStream());
                         OutputStream out = new FileOutputStream(apkFile)) {
                        byte[] buf = new byte[16384];
                        int len;
                        while ((len = in.read(buf)) > 0) {
                            out.write(buf, 0, len);
                            downloaded += len;
                            long now = System.currentTimeMillis();
                            if (now - lastUiUpdate >= 250) {
                                lastUiUpdate = now;
                                if (total > 0) {
                                    int pct = (int) Math.min(100, (downloaded * 100L) / total);
                                    evalJs("window.onUpdateProgress && window.onUpdateProgress(" + pct + ", 'İndiriliyor: %" + pct + "')");
                                } else {
                                    long mb = downloaded / (1024 * 1024);
                                    evalJs("window.onUpdateProgress && window.onUpdateProgress(50, 'İndiriliyor: " + mb + " MB')");
                                }
                            }
                        }
                    }

                    if (apkFile.length() < 10L * 1024L * 1024L) {
                        throw new Exception("İndirilen APK dosyası geçersiz (boyut çok küçük).");
                    }
                    byte[] magic = new byte[4];
                    try (InputStream checkIn = new FileInputStream(apkFile)) {
                        if (checkIn.read(magic) != 4
                                || magic[0] != 0x50 || magic[1] != 0x4B || magic[2] != 0x03 || magic[3] != 0x04) {
                            throw new Exception("İndirilen dosya geçerli bir APK değil.");
                        }
                    }

                    evalJs("window.onUpdateProgress && window.onUpdateProgress(100, 'Kurulum hazırlanıyor...')");
                    PackageInstaller packageInstaller = getPackageManager().getPackageInstaller();
                    PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(
                            PackageInstaller.SessionParams.MODE_FULL_INSTALL);
                    int sessionId = packageInstaller.createSession(params);
                    try (PackageInstaller.Session session = packageInstaller.openSession(sessionId);
                         InputStream in = new FileInputStream(apkFile);
                         OutputStream out = session.openWrite("base.apk", 0, apkFile.length())) {
                        byte[] buf = new byte[16384];
                        int len;
                        while ((len = in.read(buf)) > 0) {
                            out.write(buf, 0, len);
                        }
                        session.fsync(out);
                        Intent statusIntent = new Intent(ACTION_INSTALL_STATUS);
                        statusIntent.setPackage(getPackageName());
                        int flags = PendingIntent.FLAG_UPDATE_CURRENT
                                | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S ? PendingIntent.FLAG_MUTABLE : 0);
                        PendingIntent pi = PendingIntent.getBroadcast(MainActivity.this, sessionId, statusIntent, flags);
                        session.commit(pi.getIntentSender());
                    }
                } catch (Exception e) {
                    evalJs("window.onUpdateStatus && window.onUpdateStatus('error', "
                            + JSONObject.quote(e.getMessage() != null ? e.getMessage() : "Güncelleme hatası") + ")");
                } finally {
                    if (conn != null) {
                        conn.disconnect();
                    }
                    if (apkFile.exists()) {
                        apkFile.delete();
                    }
                }
            });
        }

        @JavascriptInterface
        public void search(String query, int limit) {
            executor.execute(() -> {
                try {
                    Python py = Python.getInstance();
                    PyObject mod = py.getModule("downloader");
                    String json = mod.callAttr("search_youtube", query, limit).toString();
                    evalJs("window.onSearchResults(" + JSONObject.quote(json) + ")");
                } catch (Exception e) {
                    try {
                        JSONObject err = new JSONObject();
                        err.put("ok", false);
                        err.put("error", e.getMessage() != null ? e.getMessage() : "Arama hatası.");
                        evalJs("window.onSearchResults(" + JSONObject.quote(err.toString()) + ")");
                    } catch (Exception ignored) {}
                }
            });
        }

        @JavascriptInterface
        public void getInfo(String url) {
            executor.execute(() -> {
                try {
                    Python py = Python.getInstance();
                    PyObject mod = py.getModule("downloader");
                    String json = mod.callAttr("get_info", url).toString();
                    evalJs("window.onVideoInfo(" + JSONObject.quote(json) + ")");
                } catch (Exception e) {
                    try {
                        JSONObject err = new JSONObject();
                        err.put("ok", false);
                        err.put("error", e.getMessage() != null ? e.getMessage() : "Bilgi alınamadı.");
                        evalJs("window.onVideoInfo(" + JSONObject.quote(err.toString()) + ")");
                    } catch (Exception ignored) {}
                }
            });
        }

        @JavascriptInterface
        public void download(String url, String quality) {
            downloadWithSubtitle(url, quality, "");
        }

        @JavascriptInterface
        public void downloadWithSubtitle(String url, String quality, String subtitleLang) {
            downloadWithOptions(url, quality, subtitleLang, "");
        }

        @JavascriptInterface
        public void downloadWithOptions(String url, String quality, String subtitleLang, String dubLang) {
            executor.execute(() -> {
                try {
                    File tempDir = new File(getCacheDir(), "dl_temp");
                    Python py = Python.getInstance();
                    PyObject mod = py.getModule("downloader");
                    ProgressCallback cb = (percent, text) ->
                            evalJs("window.onDownloadProgress(" + percent + ", " + JSONObject.quote(text) + ")");

                    String json = mod.callAttr(
                            "download_video", url, quality, tempDir.getAbsolutePath(), cb,
                            subtitleLang != null ? subtitleLang : "",
                            dubLang != null ? dubLang : ""
                    ).toString();
                    JSONObject obj = new JSONObject(json);
                    if (obj.optBoolean("ok", false)) {
                        String srcPath = obj.getString("filepath");
                        String fileName = obj.getString("filename");
                        String savedPath = saveToPublicDownloads(new File(srcPath), fileName);
                        String subPath = obj.optString("subtitle_path", "");
                        String subName = obj.optString("subtitle_name", "");
                        if (!subPath.isEmpty() && !subName.isEmpty()) {
                            File subFile = new File(subPath);
                            if (subFile.exists()) {
                                saveToPublicDownloads(subFile, subName);
                            }
                        }
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
