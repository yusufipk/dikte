# Dikte

`Ctrl+Space`'e bas, konuş, tekrar bas. Ses varsayılan olarak bu makinede yazıya
çevrilir, bir model transkripti temizler (ıı'lar, tekrarlar, eksik noktalama),
sonuç panoya kopyalanır ve o an yazdığın pencereye yapıştırılır.

KDE Plasma 6 / Wayland için yazıldı; GNOME X11'de, macOS'ta,
[Windows](README.windows.md)'ta ve klavyeyi okumasına izin veren diğer Linux
masaüstlerinde de çalışır. Sistem paketleri dışında bağımlılığı yok: sadece
Python standart kütüphanesi (3.11 veya üstü) ve PyQt6.

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

[Sürümler sayfasında](../../releases) bir AppImage, her Mac mimarisi için
birer disk imajı, bir de Windows kurulumu var. İlk ikisi ilk çalıştıklarında
kendi menü girdisini, oturum açılışını ve `dikte` komutunu yazar, makinede
zaten duran bir kuruluma dokunmazlar; `dikte integrate --remove` yazdıklarını
geri alır. AppImage yine de aşağıdaki sistem paketlerini ister: ses sunucusu,
pano ve klavye onlardan gelir. Disk imajı bir Apple sertifikasıyla imzalı
değil, bu yüzden ilk açılış reddedilir, Sistem Ayarları → Gizlilik ve Güvenlik
altından **Yine de Aç** demek gerekir; macOS her güncellemeden sonra mikrofonu
ve Erişilebilirliği yeniden sorar, checkout'tan kurmak bundan kurtarır.
Windows kurulumu yalnızca kendi hesabına kurar ve yanında bir ffmpeg taşır.

```sh
sudo pacman -S --needed pipewire-audio wl-clipboard ydotool ffmpeg python-pyqt6
systemctl --user enable --now ydotool     # otomatik yapıştırma için

./install.sh                 # ya da:  ./install.sh "Meta+Space" "Meta+Shift+Space"
dikte                        # ilk açılışta ayarlar penceresi gelir
```

Fedora'da paket adları farklı, Fedora'nın kendi depolarındaki `ffmpeg-free`
yetiyor çünkü Dikte video dosyasının yalnızca ses izini alıyor, `ydotool` da
bir adım fazla istiyor: sistem servisi olarak geliyor, soketi de root'a ait
kalıp oturumun erişemediği yerde durduğu için servis çalışırken bile otomatik
yapıştırma tutmuyor. Soketi istemcinin zaten baktığı yola al ve sahipliğini
devret:

```sh
sudo dnf install pipewire-utils wl-clipboard ydotool ffmpeg-free python3-pyqt6
sudo mkdir -p /etc/systemd/system/ydotool.service.d
printf '[Service]\nExecStart=\nExecStart=/usr/bin/ydotoold --socket-path=%s/.ydotool_socket --socket-own=%s:%s\n' \
  "$XDG_RUNTIME_DIR" "$(id -u)" "$(id -g)" \
  | sudo tee /etc/systemd/system/ydotool.service.d/override.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now ydotool
```

Ubuntu/GNOME X11 için kayıt PulseAudio üzerinden, pano ve yapıştırma ise X11
araçlarıyla çalışır:

```sh
sudo apt install pulseaudio-utils xclip xdotool ffmpeg
```

macOS'ta da aynı `./install.sh` çalışır, işi `scripts/install-mac.sh`'a devreder;
`~/Applications` içine bir `Dikte.app`, `dikte` komutunu ve bir LaunchAgent
kurar:

```sh
brew install pyqt ffmpeg   # pyqt Apple'ın 3.9'undan yeni bir Python'ı da getirir

./install.sh               # ya da:  ./install.sh "Ctrl+Option+Space" "Ctrl+Option+D"
open -a Dikte
```

macOS **Mikrofon** ve **Erişilebilirlik** izinlerini uygulama paketinin kimliğine
yazıyor, ikisini de ilk gerektiğinde soruyor. Varsayılan kısayol orada
`Ctrl+Option+Space`, çünkü `Ctrl+Space` giriş kaynağını değiştirmeye ayrılmış; ve
hiçbir şey için oturumu kapatman gerekmez, kombinasyonu Dikte çalışırken kendisi
tutar. PyQt6 pip'ten değil brew'dan geliyor, Homebrew'un Python'u içine
kurulmayı reddediyor; sanal ortam kullanacaksan
`DIKTE_PYTHON=…/venv/bin/python ./install.sh`.

Orada elle derlenmesi gereken tek parça yerel transkripsiyon: whisper.cpp'nin
macOS sürümü yok, Homebrew'unki de `WHISPER_BUILD_SERVER=OFF` ile derleniyor,
yani `whisper-cli` kuruluyor, Dikte'nin konuştuğu sunucu değil. Kendin derle
(`cmake -B build -DWHISPER_BUILD_SERVER=ON -DGGML_METAL=ON && cmake --build
build -j`) ve yolunu Ayarlar → API'ye yaz, ya da buluta çevir. Toplantı için
BlackHole veya Loopback gerekiyor (`brew install blackhole-2ch`); dikte için
gerekmiyor.

Windows da aynı şekilde çalışıyor, Dikte açıkken kombinasyonu sistemin kendi
kısayol servisi üzerinden tutuyor. Sürümler sayfasındaki kurulum kaydın
istediği ffmpeg'i de taşıyor ve yönetici istemiyor; checkout'tan ise `winget
install Gyan.FFmpeg`, `pip install PyQt6`, sonra `python -m dikte`, Başlat
Menüsü girdisi ve `dikte` komutu için de isteğe bağlı `install.ps1`. Orada
toplantı kaydı henüz yok, ayrıntılar [Windows README](README.windows.md)'sinde.

`install.sh` `dikte` komutunu, menü girdisini, oturum açılışında otomatik
başlatmayı ve iki global kısayolu kurar; tuşları iki argümanı, argüman
verilmezse ayarlarında duranlar. `./scripts/update.sh` son sürümü çeker ve
bunları yerine koyar; `./scripts/uninstall.sh` hepsini geri alır, `--purge`
demedikçe ayarlarına ve diktelerine dokunmaz. Dikte sürüm sayfasına günde bir
kez bakar ve yeni sürüm çıkmışsa tepsi menüsüne bir satır koyar; o satır bir şey
kurmaz, sayfayı açar. Genel sekmesi bu denetimi kapatır ya da anında çalıştırır.

Sesi yazıya çevirme ve temizleme, ayarlar penceresinde ayrı ayrı sağlayıcı
seçer; ikisi de varsayılan olarak burada, kendi modellerinle çalışır. Bulutu
seçersen sesi yazıya çevirme **OpenAI**, **Groq** ya da **OpenRouter**'da
(varsayılan `gpt-4o-transcribe`), temizleme OpenRouter'da
(`google/gemini-3.5-flash-lite`) ya da kuruluysa Claude Code veya Codex'te
çalışır. Anahtarları boş bırakırsan `OPENAI_API_KEY`, `GROQ_API_KEY` ve
`OPENROUTER_API_KEY` kullanılır; anahtarlar `~/.config/dikte/config.json`
içinde, izinler 600, Mac'te ise `~/Library/Application Support/Dikte` altında.
Temizlemeyi tamamen kapatabilirsin, o zaman ham transkript yapıştırılır; modelin
yanındaki kutudan düşünme seviyesini de seçebilirsin.

## Kullanım

| Ne | Nasıl |
| --- | --- |
| Kaydı başlat / bitir | `Ctrl+Space`, ya da tepsi simgesine tıkla |
| Kaydı duraklat / sürdür | Tepsi menüsü, `dikte pause`, ya da atadığın bir tuş |
| Kaydı iptal et | `Ctrl+Alt+Space`, tepsi menüsü, ya da `dikte cancel` |
| Ajana sesle komut ver | Tepsi menüsü → *Claude'a sor*, ya da `dikte ask` |
| Toplantıyı başlat / bitir | Tepsi menüsü → *Toplantı kaydet*, ya da `dikte meeting` |
| Ayarlar | Tepsi menüsü → *Ayarlar*, ya da `dikte settings` |
| Yeni sürüm var mı bak | Genel sekmesi → *Şimdi bak*, ya da `dikte update` |
| Güncelleme sonrası yeniden yükle | Tepsi menüsü → *Yeniden başlat*, ya da `dikte restart` |
| Çık | Tepsi menüsü → *Çık*, ya da `dikte quit` |

Ekranın köşesindeki gösterge kırmızı kayıt noktasını, canlı ses dalgasını ve
süreyi, ardından hangi aşamada olduğunu gösterir. Odak almaz. Önceki dikte daha
temizlenirken `Ctrl+Space`'e tekrar basmak yenisini başlatır; o da sırası
gelince, öndekinin ardından yazılıp yapıştırılır. Dikte ile ajana
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
  ayakta tutar. Derleme destekliyorsa ekran kartına CUDA, ROCm ya da Vulkan
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

## Global kısayollar ve KDE'nin istediği oturum kapatma

KWin `kglobalshortcutsrc` dosyasını yalnızca açılışta okur, yani `install.sh`'ın
yazdığı kısayollar oturumu yeniden açana kadar tetiklenmez. O zamana kadar Ayarlar →
Kısayollar → **yerleşik dinleyici** `/dev/input` üzerinden kombinasyonu kendisi
yakalar. Tek farkı: tuşu yutmaz, yani `Ctrl+Space` odaktaki uygulamaya da iletilir
(bazı editörlerde otomatik tamamlama açılabilir). Dinleyici kullanıcının `input`
grubunda olmasını gerektirir: `sudo usermod -aG input $USER`. GNOME'da kısayol
kurulduğu anda çalışır; hiç kayıt defteri tutmayan masaüstlerinde (i3, XFCE,
sway ve çoğu diğeri) dinleyici mekanizmanın kendisidir: hiçbir şey kurulmaz,
oturum kapatmak gerekmez, tuşları masaüstünün sahiplenmesini istersen Ayarlar →
Kısayollar sekmesi bağlanacak komutu gösterir.

## Dosyalar

Aşağıdakilerin hepsi `dikte` paketinin içinde: `python3 -m dikte` bunu çalıştırır,
içindeki `__main__.py` de her başlatıcının ve kısayolun adlandırdığı dosyadır.
`scripts/` altında install-mac.sh, update.sh, uninstall.sh ve release.sh var;
`packaging/` release.sh'ın attığı etiketin yayımladığı AppImage'i, disk imajını
ve Windows kurulumunu derler; install.sh en üstte kalır, `tests/` içinde de her
modülün bir dosyası.

```
app.py            giriş noktası, tepsi simgesi, durum makinesi
cli.py            komut satırı: bütün fiiller ve verdikleri cevap
ipc.py            yerel sokette bir istek, bir cevap
audio.py          PCM kaydı: diktede pw-record, toplantıda ffmpeg
meeting.py        kanal ayırma, konuşmacı etiketi, temizleme, tutanak
assistant.py      dikteyi Claude Code, Codex ya da OpenRouter'dan geçirme
api.py            transkript ve temizleme istekleri (yalnız stdlib)
cleanup.py        transkripti kim temizler: OpenRouter, burası, Claude ya da Codex
ggml.py           whisper.cpp ve llama.cpp'yi indirip burada çalıştırma
hub.py            GitHub ve Hugging Face'te bugün ne olduğu
update.py         yeni sürüm çıkmış mı, çıkmışsa hangi sayfada
worker.py         transkript → temizleme → pano → yapıştırma
vad.py            kayıtta gerçekten konuşma var mı kararı
filetranscribe.py dosyadan transkript: ffmpeg, parçalama, zaman damgaları
overlay.py        köşedeki gösterge
settings_ui.py    ayarlar penceresi
hotkey.py         masaüstünün kısayol kaydı, evdev dinleyici, Mac'te Carbon
paste.py          wl-clipboard ve ydotool sarmalayıcıları, pbcopy ve CoreGraphics
trayicon.py       tepsi simgeleri, ikon teması olmayan yerler için çizilmiş
integrate.py      indirilen bir yapının indiği masaüstüne yazdıkları
i18n.py           metin tablosu
```

Gösterge XWayland üzerinden çizilir; Wayland'da bir pencereyi belirli bir köşeye
yerleştirmenin yolu yok, `app.py` bu yüzden `QT_QPA_PLATFORM=xcb` ayarlar.

## Lisans

GPL-3.0, [LICENSE](LICENSE) dosyasına bak.
