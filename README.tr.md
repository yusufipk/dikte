# Dikte

`Ctrl+Space`'e bas, konuş, tekrar bas. Ses varsayılan olarak bu makinede yazıya
çevrilir, bir model transkripti temizler (ıı'lar, tekrarlar, eksik noktalama),
sonuç panoya kopyalanır ve o an yazdığın pencereye yapıştırılır.

KDE Plasma 6 / Wayland için yazıldı, macOS'ta da çalışır. Sistem paketleri
dışında bağımlılığı yok: sadece Python standart kütüphanesi ve PyQt6.

*[English README](README.md)*

<p align="center">
  <img src="docs/settings-general.webp" width="820" alt="Dikte ayarları, Genel sekmesi">
</p>

|  |  |
|---|---|
| <img src="docs/settings-api.webp" width="410" alt="API ve modeller"> | <img src="docs/settings-cleanup.webp" width="410" alt="Temizleme kuralları"> |
| <img src="docs/settings-agent.webp" width="410" alt="Ajan"> | <img src="docs/settings-meeting.webp" width="410" alt="Toplantı"> |
| <img src="docs/settings-audio-file.webp" width="410" alt="Ses dosyası"> | <img src="docs/settings-shortcuts.webp" width="410" alt="Kısayollar"> |

## Kurulum

```sh
sudo pacman -S --needed pipewire-audio wl-clipboard ydotool ffmpeg python-pyqt6
systemctl --user enable --now ydotool     # otomatik yapıştırma için

./install.sh                 # ya da:  ./install.sh "Meta+Space" "Meta+Shift+Space"
dikte                        # ilk açılışta ayarlar penceresi gelir
```

Ubuntu/GNOME X11 için kayıt PulseAudio üzerinden, pano ve yapıştırma ise X11
araçlarıyla çalışır:

```sh
sudo apt install pulseaudio-utils xclip xdotool ffmpeg
```

macOS için:

```sh
brew install ffmpeg whisper-cpp python@3.13
python3.13 -m pip install PyQt6

./install.sh                 # ya da:  ./install.sh "Ctrl+Alt+Space" "Ctrl+Alt+D"
dikte
```

`install.sh` `dikte` komutunu, menü girdisini, oturum açılışında otomatik
başlatmayı ve iki global kısayolu kurar; tuşları da iki argümanı. `./update.sh`
son sürümü çeker ve bunları senin seçtiğin tuşlarla yerine koyar;
`./uninstall.sh` hepsini geri alır, `--purge` demedikçe ayarlarına ve
diktelerine dokunmaz. macOS'ta menü girdisi `~/Applications/Dikte.app`, otomatik
başlatma ise bir launch agent; PyQt6 bir sanal ortamdaysa üçünü de çalıştırmadan
önce `DIKTE_PYTHON` değişkenini o ortamın `python3`'üne yönlendir.

macOS'ta iki noktanın söylenmesi gerekiyor. **Otomatik yapıştırma** Cmd+V'yi
System Events üzerinden basar, bu yüzden Dikte'yi çalıştıran uygulamanın
bilgisayarı kontrol etme izni olmalı: Sistem Ayarları → Gizlilik ve Güvenlik →
Erişilebilirlik — ilk yapıştırmada zaten sorulur. Ve **varsayılan kısayol
`Ctrl+Space` değil `Ctrl+Alt+Space`**, çünkü orada `Ctrl+Space` girdi kaynağı
değiştirmeye ait; macOS kendi kısayollarından birinin kullandığı bir
kombinasyonu kabul eder ama hiç iletmez, yani hiçbir şey yapmayan bir tuş hata
değil, değiştirilecek bir tuştur.

Metnin yerine ulaşmasının ikinci bir yolu daha var, Ayarlar → Genel altında:
yapıştırmak yerine **yazdırmak**. Karakterler metni taşıyan tuş olayları olarak
gider; yani pano hiç ödünç alınmaz — kopyaladığın şey kopyalı kalır — ve o
pencerede herhangi bir kombinasyonun "yapıştır" anlamına gelmesi gerekmez, ki
terminal, uzak masaüstü ya da sanal makine bunda hemfikir olmayabilir. Uzun
metinde daha yavaş olduğu için varsayılan hâlâ yapıştırma. Üç platformda da var.

**Toplantı kaydı yalnızca Linux'ta.** Hoparlörden çıkan sesi gerektiriyor ve
macOS bunu kimseye vermiyor. Geri kalan her şey çalışır: dikte, ajan, ses ve
video dosyaları, başka yerde alınmış bir kaydın yazıya dökülmesi.

Sesi yazıya çevirme ve temizleme, ayarlar penceresinde ayrı ayrı sağlayıcı
seçer; ikisi de varsayılan olarak burada, kendi modellerinle çalışır. Bulutu
seçersen sesi yazıya çevirme **OpenAI**, **Groq** ya da **OpenRouter**'da
(varsayılan `gpt-4o-transcribe`), temizleme OpenRouter'da
(`google/gemini-3.5-flash-lite`) ya da kuruluysa Claude Code veya Codex'te
çalışır. Anahtarları boş bırakırsan `OPENAI_API_KEY`, `GROQ_API_KEY` ve
`OPENROUTER_API_KEY` kullanılır; anahtarlar `~/.config/dikte/config.json`
içinde, izinler 600. Temizlemeyi tamamen kapatabilirsin, o zaman ham transkript
yapıştırılır; modelin yanındaki kutudan düşünme seviyesini de seçebilirsin.

## Kullanım

| Ne | Nasıl |
| --- | --- |
| Kaydı başlat / bitir | `Ctrl+Space`, ya da tepsi simgesine tıkla |
| Kaydı iptal et | `Ctrl+Alt+Space`, tepsi menüsü, ya da `dikte cancel` |
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

Ayarlar penceresindeki her şeyin bir de komutu var; bir betik ya da bir ajan
yazılımın tamamını çalıştırabilsin diye: `dikte record --seconds 8` söyleneni
geri verir, `dikte transcribe konusma.mp4 --srt` altyazıyı yazar, ayarlar,
geçmiş ve toplantılar da yanlarında durur. Hepsini `dikte --help` sayar, hepsi
`--json` kabul eder, yalnızca mikrofona ihtiyacı olanlar uygulamanın açık
olmasını ister.

## Neler yapıyor

- **Her şey varsayılan olarak bu makinede çalışır.** Sesi yazıya çevirme
  whisper.cpp, temizleme llama.cpp üzerinde; ikisini de önceden kurman gerekmez:
  ayarlar penceresi programı ve modeli indirir, sha256'sını doğrular,
  checksum'suz yayınlanmış bir indirmeyi reddeder, sen dikte ettikçe sunucuyu
  ayakta tutar. Mac'te Metal, başka yerde derleme destekliyorsa CUDA, ROCm ya da Vulkan
  üzerinden ulaşılır. Anahtar yok, hesap yok, makineden çıkan bir şey yok.
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
- **Toplantılar** (yalnızca Linux) mikrofonla hoparlör çıkışından aynı anda kaydedilir; kimin ne
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

## Global kısayollar için bir kez oturum kapatmak gerekir

KWin `kglobalshortcutsrc` dosyasını yalnızca açılışta okur, yani `install.sh`'ın
yazdığı kısayollar oturumu yeniden açana kadar tetiklenmez. O zamana kadar Ayarlar →
Kısayollar → **yerleşik dinleyici** `/dev/input` üzerinden kombinasyonu kendisi
yakalar. Tek farkı: tuşu yutmaz, yani `Ctrl+Space` odaktaki uygulamaya da iletilir
(bazı editörlerde otomatik tamamlama açılabilir). Dinleyici kullanıcının `input`
grubunda olmasını gerektirir: `sudo usermod -aG input $USER`.

macOS'ta bunların hiçbiri geçerli değil: Carbon kombinasyonu çalışan
uygulamaya bağlar, Dikte açılır açılmaz etkindir, tuşu yutar ve hiçbir izin
istemez. Beklenecek bir şey ve açılacak bir dinleyici olmadığı için o kutu
orada sekmede yok.

## Dosyalar

```
dikte.py          giriş noktası, tepsi simgesi, durum makinesi
cli.py            komut satırı: bütün fiiller ve verdikleri cevap
ipc.py            yerel sokette bir istek, bir cevap
audio.py          PCM kaydı: pw-record ya da avfoundation, toplantıda ffmpeg
meeting.py        kanal ayırma, konuşmacı etiketi, temizleme, tutanak
assistant.py      dikteyi Claude Code, Codex ya da OpenRouter'dan geçirme
api.py            transkript ve temizleme istekleri (yalnız stdlib)
cleanup.py        transkripti kim temizler: OpenRouter, burası, Claude ya da Codex
ggml.py           whisper.cpp ve llama.cpp'yi indirip burada çalıştırma
hub.py            GitHub ve Hugging Face'te bugün ne olduğu
worker.py         transkript → temizleme → pano → yapıştırma
vad.py            kayıtta gerçekten konuşma var mı kararı
filetranscribe.py dosyadan transkript: ffmpeg, parçalama, zaman damgaları
overlay.py        köşedeki gösterge
macos.py          macOS'a söylenmesi gereken şeyler, ctypes ile
settings_ui.py    ayarlar penceresi
hotkey.py         KDE ve GNOME kısayolları, evdev dinleyici, Carbon
paste.py          wl-clipboard, xclip, pbcopy ve her birinin tuş basımı
i18n.py           metin tablosu
```

Gösterge XWayland üzerinden çizilir; Wayland'da bir pencereyi belirli bir köşeye
yerleştirmenin yolu yok, `dikte.py` bu yüzden `QT_QPA_PLATFORM=xcb` ayarlar.
macOS'ta buna gerek yok, orada o satır atlanır.

Üç platform var ve platforma özgü yarı beş dosyada: `audio.py`, `paste.py`,
`hotkey.py`, `macos.py` ve `ggml.py` içindeki dosya adları. Her biri platform başına bir
grup ve aralarından seçen tek bir satır tutuyor; her fonksiyonun içinde bir
dallanma yok. `tests/support.py` içinde bu yarılardan birini sabitleyen
testler için `linux_only` ve `macos_only` var; geri kalan her testin her
yerde geçmesi bekleniyor.

## Lisans

GPL-3.0, [LICENSE](LICENSE) dosyasına bak.
