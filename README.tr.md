# Dikte

`Ctrl+Space`'e (Windows'ta `Ctrl+Shift+Space`) bas, konuş, tekrar bas. Ses OpenAI'ye ya da OpenRouter'a gidip
yazıya çevrilir, OpenRouter'daki bir model transkripti temizler (ıı'lar,
tekrarlar, eksik noktalama), sonuç panoya kopyalanır ve o an yazdığın pencereye
yapıştırılır.

KDE Plasma 6 / Wayland ve Windows 10/11 için yazıldı. Sistem paketleri dışında
bağımlılığı yok: sadece Python standart kütüphanesi, PyQt6 ve FFmpeg.

*[English README](README.md)*

<p align="center">
  <img src="docs/settings-general.webp" width="820" alt="Dikte ayarları, Genel sekmesi">
</p>

|  |  |
|---|---|
| <img src="docs/settings-api.webp" width="410" alt="API ve modeller"> | <img src="docs/settings-cleanup.webp" width="410" alt="Temizleme kuralları"> |
| <img src="docs/settings-audio-file.webp" width="410" alt="Ses dosyası"> | <img src="docs/settings-history.webp" width="410" alt="Geçmiş"> |

## Kurulum

### Linux (Wayland/KDE)

```sh
sudo pacman -S --needed pipewire-audio wl-clipboard ydotool ffmpeg python-pyqt6
systemctl --user enable --now ydotool     # otomatik yapıştırma için

./install.sh                 # ya da:  ./install.sh "Ctrl+Alt+Space"
dikte                        # ilk açılışta ayarlar penceresi gelir
```

`install.sh` `dikte` komutunu, menü girdisini, oturum açılışında otomatik
başlatmayı ve KDE kısayolunu kurar.

### Windows

1. [Python 3](https://www.python.org/downloads/) ve FFmpeg
   (`winget install Gyan.FFmpeg`) kur. İkisi de `PATH` üzerinde olmalı; FFmpeg
   olmadan Dikte hiç kayıt alamaz.
2. Bu klasördeki `install.ps1` dosyasına sağ tıklayıp **PowerShell ile Çalıştır**
   seçeneğini seç. PyQt6'yı kurar, Başlat menüsü ve açılış kısayollarını oluşturur.
3. Dikte'yi Başlat menüsünden aç. İlk açılışta ayarlar penceresi gelir.

Kısayol `Ctrl+Space` değil `Ctrl+Shift+Space`, çünkü Windows `Ctrl+Space`'i
klavye düzeni değiştirmek için kendine ayırır ve bırakmaz. Windows'un zaten
tuttuğu bir kombinasyon kabul edilmez; Dikte hiçbir şey yapmayan bir tuş
bağlamak yerine bunu tepsi bildirimiyle söyler.

Burada iki şey farklı çalışır. Mikrofonu DirectShow üzerinden açmak yaklaşık bir
saniye sürdüğü için gösterge, ilk ses gelene kadar *Mikrofon açılıyor…* der —
sayaç da kayda giren de oradan başlar. Toplantı kaydı ise hoparlörden çıkanı
yakalayan bir cihaz ister: *Ses ayarları → Diğer ses ayarları → Kayıt* altında
**Stereo Mix**'i aç (önce listeye sağ tıklayıp devre dışı cihazları göster) ya da
sanal ses kablosu kur. Diktenin kendisi bunların hiçbirine ihtiyaç duymaz.

Ayarlar penceresinde iki anahtar istenir: **OpenAI** ve **OpenRouter**. Sesi
yazıya çevirme ikisinden birinde çalışır (varsayılan `gpt-4o-transcribe`),
temizleme her zaman OpenRouter'da (`google/gemini-3.5-flash-lite`), yani tek bir
OpenRouter anahtarı ikisine de yeter. Boş bırakırsan `OPENAI_API_KEY` ve
`OPENROUTER_API_KEY` kullanılır; anahtarlar `~/.config/dikte/config.json`
içinde, izinler 600. Temizlemeyi tamamen kapatabilirsin, o zaman ham transkript
yapıştırılır; modelin yanındaki kutudan düşünme seviyesini de seçebilirsin.

## Kullanım

| Ne | Nasıl |
| --- | --- |
| Kaydı başlat / bitir | `Ctrl+Space` (Windows'ta `Ctrl+Shift+Space`), ya da tepsi simgesine tıkla |
| Kaydı iptal et | Tepsi menüsü → *Kaydı iptal et*, ya da `dikte cancel` |
| Ajana sesle komut ver | Tepsi menüsü → *Claude'a sor*, ya da `dikte ask` |
| Toplantıyı başlat / bitir | Tepsi menüsü → *Toplantı kaydet*, ya da `dikte meeting` |
| Ayarlar | Tepsi menüsü → *Ayarlar*, ya da `dikte settings` |
| Güncelleme sonrası yeniden yükle | Tepsi menüsü → *Yeniden başlat*, ya da `dikte restart` |
| Çık | Tepsi menüsü → *Çık*, ya da `dikte quit` |

Ekranın köşesindeki gösterge kırmızı kayıt noktasını, canlı ses dalgasını ve
süreyi, ardından hangi aşamada olduğunu gösterir. Odak almaz. Dikte çalışırken
`Ctrl+Space`'e tekrar basmak bir şey yapmaz, sıraya da girmez. Dikte ile ajana
verilen komut yalnızca mikrofon için birbirini bekler, o da tek aygıt olduğu
için; başka hiçbir şeyde beklemezler. Her birinin kendi göstergesi var, ikisi
birden ekrandayken ikincisi birincinin üstüne yerleşir.

## Neler yapıyor

- **Sessizlik API'ye gitmez.** Sessize yakın bir ses verildiğinde model boş dize
  döndürmez, bir cümle uydurur ("Altyazı M.K.", "Thanks for watching"). *O
  kaydın kendi* gürültü tabanının 10 dB üstüne en az 0,3 saniye çıkan bir şey
  yoksa kayıt atılır; ne kadar yüksek olursa olsun sabit fanı ya da cızırtıyı
  eleyen de budur. Kaydın gürültülü ucu -55 dBFS altındaysa da atılır. Gösterge
  ölçtüğü seviyeyi yazar, eşiği ona bakarak ayarlarsın.
- **Yanlış duyulan kelimeler düzeltilir.** Konuşma modelleri özel isimlerde sesçe
  benzer bir şeye kayıyor; temizleme modelinden bunları bağlamdan onarması,
  bağlam netleştirmiyorsa dokunmaması isteniyor. Temizleme kuralları sekmesine
  yazdığın isimler transkripsiyon modeline ipucu, temizleme modeline sözlük
  olarak gidiyor; "kuber netis"i tanımasını sağlayan da bu:

  ```
  ham    ıı bugün şey kuber netis üzerinde çalışan servisleri güncelledim
         yani sonra grafanada bir panel açtım hani ve pay kut ile arayüzü
         şey bitirdim işte

  sonuç  Bugün Kubernetes üzerinde çalışan servisleri güncelledim. Sonra
         Grafana'da bir panel açtım ve PyQt ile arayüzü bitirdim.
  ```
- **Başarısız temizleme sessizce geçmez.** Dikte kaybolmasın diye ham transkript
  yine yapıştırılır ama gösterge kehribar rengine döner ve nedenini söyler,
  normal bir çalışma gibi görünmez.
- **Dikte bunun yerine bir komut da olabilir.** Kendi kısayolu transkripti
  yapıştırmak yerine Claude Code'a (`claude -p`) gönderir ve oradan döneni
  yapıştırır: cevabı ya da ne yapıldığını söyleyen bir cümle. Kendi açacağın
  oturumun aynısıdır, yani skill'lerin ve bağlı servislerin oradadır; "bunu
  perşembe üçe takvime koy" cümlesini Claude olmayan bir pencerede söyleyebilir
  olmanı sağlayan da budur. Codex (`codex exec`) da aynı şekilde çalışır;
  OpenRouter ise ikisi de kurulu olmayan bir makinede düz soru cevap için
  duruyor. Sağlayıcı, model, izinler ve çalışma dizini Ayarlar → Ajan
  sekmesinde; arka arkaya verilen komutlar tek bir konuşmada kalır.
- **Toplantılar** mikrofonla hoparlör çıkışından aynı anda kaydedilir; kimin ne
  dediği tahmin edilmez, sesin hangi kanaldan geldiğiyle belli olur. İki taraf
  ayrı ayrı yazıya çevrilip tek bir zaman damgalı transkriptte birleştirilir,
  ardından Ayarlar → Toplantı sekmesinden seçtiğin ikinci bir model kendi
  talimatıyla bunu tutanağa çevirir: kararlar, aksiyonlar, açık sorular. Sonuç
  `~/.local/share/dikte/meetings` altına ve Ayarlar → Tutanaklar sekmesine
  düşer. Yarıda kalan bir işlem ses kaydını saklar, yeniden denemede parası
  ödenmiş transkriptin üstünden devam eder.
- **Ses ve video dosyaları** Ayarlar → Ses dosyası sekmesinde aynı modellerden
  geçer; istersen `[dd:ss]` zaman damgalarıyla, uzun dosyalar ffmpeg ile
  parçalanarak, sonuç `.txt` ya da `.srt` altyazı olarak kaydedilerek; temizleme
  burada kendi kurallarıyla, altyazı için yazılmış haliyle çalışır: satırlar
  yerinde kalır, hiçbir şey kısaltılmaz.
- **Geçmiş** Ayarlar → Geçmiş sekmesinde; boyut sınırı var, sağ tıklayıp
  silebilirsin.
- **Türkçe ve İngilizce arayüz**, varsayılan olarak sistem dilini izler.

## Global kısayol için bir kez oturum kapatmak gerekir (yalnızca Linux)

KWin `kglobalshortcutsrc` dosyasını yalnızca açılışta okur, yani `install.sh`'ın
yazdığı kısayol oturumu yeniden açana kadar tetiklenmez. O zamana kadar Ayarlar →
Kısayol → **yerleşik dinleyici** `/dev/input` üzerinden kombinasyonu kendisi
yakalar. Tek farkı: tuşu yutmaz, yani `Ctrl+Space` odaktaki uygulamaya da iletilir
(bazı editörlerde otomatik tamamlama açılabilir). Dinleyici kullanıcının `input`
grubunda olmasını gerektirir: `sudo usermod -aG input $USER`.

Windows'ta böyle bir bekleme yok: kısayol `RegisterHotKey` ile anında kaydedilir
ve tuşu yutar, odaktaki uygulama görmez. Karşılığında, Windows'un kendine ayırdığı
bir kombinasyon hiç verilmez — `Ctrl+Space` (klavye düzeni) ve `Win+H` (Windows
dikte) bunlardan. Böyle bir durumda Dikte tepsiden uyarır.

## Dosyalar

```
dikte.py          giriş noktası, tepsi simgesi, durum makinesi, IPC
audio.py          PCM kaydı: diktede pw-record, toplantıda ffmpeg
meeting.py        kanal ayırma, konuşmacı etiketi, temizleme, tutanak
assistant.py      dikteyi Claude Code, Codex ya da OpenRouter'dan geçirme
api.py            iki sağlayıcıda transkript + OpenRouter temizleme (yalnız stdlib)
worker.py         transkript → temizleme → pano → yapıştırma
vad.py            kayıtta gerçekten konuşma var mı kararı
filetranscribe.py dosyadan transkript: ffmpeg, parçalama, zaman damgaları
overlay.py        köşedeki gösterge
settings_ui.py    ayarlar penceresi
hotkey.py         KDE kısayol kurulumu ve evdev dinleyici
paste.py          pano ve tuş gönderimi: wl-clipboard/ydotool, Windows'ta Win32
platform_utils.py  platform farkları: dizinler, dil, konsolsuz alt süreç
i18n.py           metin tablosu
```

Gösterge XWayland üzerinden çizilir; Wayland'da bir pencereyi belirli bir köşeye
yerleştirmenin yolu yok, `dikte.py` bu yüzden `QT_QPA_PLATFORM=xcb` ayarlar.

## Lisans

GPL-3.0, [LICENSE](LICENSE) dosyasına bak.
