# Video İndirici (YouTube & Instagram)

YouTube ve Instagram (Reels, Post, Video) bağlantılarını kolayca MP4 veya MP3 olarak indirmenizi sağlayan yerel web uygulaması.

## Özellikler
- **YouTube & Instagram** desteği (`yt-dlp` + `ffmpeg` tabanlı)
- **Kalite Seçenekleri:** En İyi MP4, 1080p, 720p, 480p veya Sadece Ses (MP3)
- **Canlı İlerleme Takibi:** İndirme yüzdesi, hız ve kalan süre
- **Önizleme:** İndirmeden önce video başlığı, kanal adı ve kapak görseli
- **Kolay Erişim:** İndirilen dosyalar `İndirilenler\VideoIndirici` klasörüne kaydedilir

## Çalıştırma
`baslat.bat` dosyasına çift tıklayın veya terminalden:
```powershell
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765
```
Ardından tarayıcıda `http://127.0.0.1:8765` adresini açın.
