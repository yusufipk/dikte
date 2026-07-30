# Dikte

`Ctrl+Space`'e bas, konuş, tekrar bas. Ses API anahtarı gerektirmeyen yerel
whisper.cpp ile veya OpenAI/OpenRouter üzerinden yazıya çevrilir; sonuç panoya
kopyalanır ve o an yazdığın pencereye yapıştırılır.

KDE Plasma 6 / Wayland ve macOS 13 veya üstünde çalışır. macOS portu ses için
AVFoundation, genel kısayol için yerleşik Carbon API'si ve sistem panosunu
kullanır.

*[English README](README.md)*

<p align="center">
  <img src="docs/settings-general.webp" width="820" alt="Dikte ayarları, Genel sekmesi">
</p>

|  |  |
|---|---|
| <img src="docs/settings-api.webp" width="410" alt="API ve modeller"> | <img src="docs/settings-cleanup.webp" width="410" alt="Temizleme kuralları"> |
| <img src="docs/settings-audio-file.webp" width="410" alt="Ses dosyası"> | <img src="docs/settings-history.webp" width="410" alt="Geçmiş"> |

## Linux kurulumu

```sh
sudo pacman -S --needed pipewire-audio wl-clipboard ydotool ffmpeg python-pyqt6
systemctl --user enable --now ydotool     # otomatik yapıştırma için

./install.sh                 # ya da:  ./install.sh "Ctrl+Alt+Space"
dikte                        # ilk açılışta ayarlar penceresi gelir
```

`install.sh` `dikte` komutunu, menü girdisini, oturum açılışında otomatik
başlatmayı ve KDE kısayolunu kurar.

## macOS kurulumu

macOS derlemesi Apple Silicon ve Intel Mac'leri destekler:

```sh
brew install python ffmpeg
chmod +x build-macos.sh
./build-macos.sh
open dist
```

`Dikte-macOS.zip` dosyasını açıp `Dikte.app` dosyasını `/Applications`
klasörüne sürükle. İlk kayıtta Mikrofon
izni, ilk otomatik yapıştırmada Erişilebilirlik/Otomasyon izni sorulur. Otomatik
açılmazsa **Sistem Ayarları → Gizlilik ve Güvenlik → Erişilebilirlik** altında
Dikte'yi etkinleştir.

Dikte macOS menü çubuğunda **🎙️** simgesiyle çalışır. Simgeye
tıklayınca menüsü açılır; **Ayarlar…** ilk sıradadır ve Dikte boştayken simgenin
doğrudan tetiklenmesi Ayarlar penceresini açar.

Uygulama yerel/ad-hoc imzalıdır. Apple Developer hesabıyla noterlenmemiş,
internetten indirilmiş bir derlemede ilk açılışta **Control-tık → Aç** gerekebilir.
Ayarlar ve geçmiş `~/Library/Application Support/Dikte` altında tutulur.
GitHub Actions her push ve pull request'te `Dikte-macOS.zip` üretir.

### API anahtarı olmadan kullanım

**Ayarlar → API ve modeller → Sesi yazıya çevirme** altında **Yerel Whisper —
API anahtarı yok** sağlayıcısını seçip **Yerel Whisper'ı kur** düğmesine bas.
macOS'te düğme gerekirse Homebrew `whisper-cpp` paketini kurar ve Türkçe için
önerilen çok dilli `large-v3-turbo-q5_0` modelini (574 MB) bir kez indirir.
Ses ve transkript yerel transkripsiyon sırasında bilgisayardan dışarı çıkmaz.
**Transkript temizleme** altında **Codex CLI — API anahtarı yok** seçilirse
Cleanup Rules giriş yaptığın Codex oturumunda çalışır; tamamen yerel ham
transkript için temizlemeyi kapatabilirsin.

Sesle soru sormak veya işlem yaptırmak için Ayarlar → Ajan sekmesi zaten giriş
yaptığın `codex exec` ya da `claude -p` terminal oturumunu destekler. Ayrı bir
API anahtarı yapıştırılmaz: Yerel Whisper sesi metne çevirir, Dikte metni seçilen
terminal aracına gönderir ve cevabı yapıştırır. Codex CLI ses dosyasını doğrudan
girdi olarak kabul etmez.

Bulut seçeneğinde **OpenAI** ve/veya **OpenRouter** anahtarı kullanılabilir.
Sesi yazıya çevirme ikisinden birinde, temizleme OpenRouter veya Codex CLI'da
çalışır; tek bir OpenRouter anahtarı iki bulut adımına da yeter. Boş bırakırsan
`OPENAI_API_KEY` ve
`OPENROUTER_API_KEY` kullanılır; anahtarlar Linux'ta
`~/.config/dikte/config.json`, macOS'te
`~/Library/Application Support/Dikte/config.json` içinde, izinler 600 tutulur.

## Kullanım

| Ne | Nasıl |
| --- | --- |
| Kaydı başlat / bitir | `Ctrl+Space`, ya da tepsi simgesine tıkla |
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
  ödenmiş transkriptin üstünden devam eder. macOS'te sistem çıkışını giriş
  aygıtı olarak göstermek için BlackHole, Loopback veya Soundflower gerekir;
  sonra Ayarlar → Toplantı'dan seçilir. Normal dikte için sanal ses aygıtı
  gerekmez.
- **Ses ve video dosyaları** Ayarlar → Ses dosyası sekmesinde aynı modellerden
  geçer; istersen `[dd:ss]` zaman damgalarıyla, uzun dosyalar ffmpeg ile
  parçalanarak, sonuç `.txt` ya da `.srt` altyazı olarak kaydedilerek; temizleme
  burada kendi kurallarıyla, altyazı için yazılmış haliyle çalışır: satırlar
  yerinde kalır, hiçbir şey kısaltılmaz.
- **Geçmiş** Ayarlar → Geçmiş sekmesinde; boyut sınırı var, sağ tıklayıp
  silebilirsin.
- **Türkçe ve İngilizce arayüz**, varsayılan olarak sistem dilini izler.

## Global kısayollar

macOS'te yerleşik dinleyici sistemin Carbon genel kısayol API'sini kullanır ve
ayarlar kaydedildiği anda çalışır. Otomatik yapıştırma ayrıca yukarıda anlatılan
Erişilebilirlik iznini ister.

Linux'ta KWin `kglobalshortcutsrc` dosyasını yalnızca açılışta okur, yani `install.sh`'ın
yazdığı kısayol oturumu yeniden açana kadar tetiklenmez. O zamana kadar Ayarlar →
Kısayol → **yerleşik dinleyici** `/dev/input` üzerinden kombinasyonu kendisi
yakalar. Tek farkı: tuşu yutmaz, yani `Ctrl+Space` odaktaki uygulamaya da iletilir
(bazı editörlerde otomatik tamamlama açılabilir). Dinleyici kullanıcının `input`
grubunda olmasını gerektirir: `sudo usermod -aG input $USER`.

## Dosyalar

```
dikte.py          giriş noktası, tepsi simgesi, durum makinesi, IPC
audio.py          PCM kaydı: Linux'ta PipeWire, macOS'te AVFoundation
meeting.py        kanal ayırma, konuşmacı etiketi, temizleme, tutanak
assistant.py      dikteyi Claude Code, Codex ya da OpenRouter'dan geçirme
api.py            bulut veya Yerel Whisper transkripti + OpenRouter temizleme
local_whisper.py  whisper.cpp kurulumu, doğrulanmış model indirme ve transkript
worker.py         transkript → temizleme → pano → yapıştırma
vad.py            kayıtta gerçekten konuşma var mı kararı
filetranscribe.py dosyadan transkript: ffmpeg, parçalama, zaman damgaları
overlay.py        köşedeki gösterge
settings_ui.py    ayarlar penceresi
hotkey.py         KDE/evdev ve yerleşik macOS genel kısayolları
paste.py          Wayland ve macOS pano/yapıştırma sarmalayıcıları
i18n.py           metin tablosu
```

Linux'ta gösterge XWayland üzerinden çizilir; Wayland'da bir pencereyi belirli
bir köşeye yerleştirmenin yolu yok, `dikte.py` bu yüzden
`QT_QPA_PLATFORM=xcb` ayarlar. macOS yerleşik yüzen araç penceresini kullanır.

## Lisans

GPL-3.0, [LICENSE](LICENSE) dosyasına bak.
