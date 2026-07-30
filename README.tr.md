# Dikte

`Ctrl+Space`'e bas, konuş, tekrar bas. Ses kendi makinende whisper.cpp ile yazıya
çevrilir, OpenRouter'daki bir model transkripti temizler (ıı'lar, tekrarlar,
eksik noktalama), sonuç panoya kopyalanır ve o an yazdığın pencereye
yapıştırılır. Yazıya çevirme için OpenAI ve OpenRouter da seçenek olarak duruyor.

KDE Plasma 6 / Wayland için yazıldı. Sistem paketleri dışında bağımlılığı yok:
sadece Python standart kütüphanesi ve PyQt6.

*[English README](README.md)*

<p align="center">
  <img src="docs/settings-general.webp" width="820" alt="Dikte ayarları, Genel sekmesi">
</p>

|  |  |
|---|---|
| <img src="docs/settings-api.webp" width="410" alt="API ve modeller"> | <img src="docs/settings-cleanup.webp" width="410" alt="Temizleme kuralları"> |
| <img src="docs/settings-audio-file.webp" width="410" alt="Ses dosyası"> | <img src="docs/settings-history.webp" width="410" alt="Geçmiş"> |

## Kurulum

```sh
sudo pacman -S --needed pipewire-audio wl-clipboard ydotool ffmpeg python-pyqt6
sudo pacman -S --needed whisper-cpp      # yerel sesten yazıya
sudo pacman -S --needed cuda             # NVIDIA kartta ekran kartı arka ucu
systemctl --user enable --now ydotool    # otomatik yapıştırma için

./install.sh                 # ya da:  ./install.sh "Ctrl+Alt+Space"
dikte                        # ilk açılışta ayarlar penceresi gelir
```

`install.sh` `dikte` komutunu, menü girdisini, oturum açılışında otomatik
başlatmayı ve KDE kısayolunu kurar.

Sesi yazıya çevirme varsayılan olarak yerelde, whisper.cpp üzerinde çalışır.
Ayarlar → API ve modeller altından bir model seçip **İndir**'e bas: varsayılan
`large-v3-turbo` (1,5 GB), liste `tiny`'den `large-v3`'e kadar gidiyor. Modeller
`~/.local/share/dikte/models` altına iner. Ses makineden çıkmıyor ve dikte başına
bir maliyeti yok.

Temizleme, seçtiğine göre **DeepSeek** (`deepseek-v4-flash`) ya da **OpenRouter**
(`google/gemini-3.5-flash-lite`) üzerinde çalışır; aynı seçim toplantı tutanağını
da yazar. Temizlemeyi tamamen kapatabilirsin, o zaman ham transkript
yapıştırılır. Yazıya çevirmeyi aynı sekmeden **OpenAI** ya da **OpenRouter**'a
taşıyabilirsin — kendi üstünde model çalıştırmak istemeyen makine için.
Anahtarları boş bırakırsan `OPENAI_API_KEY`, `OPENROUTER_API_KEY` ve
`DEEPSEEK_API_KEY` kullanılır; `~/.config/dikte/config.json` içinde saklanır,
izinler 600.

DeepSeek hakkında bilinmesi gereken bir şey var: aksi söylenmedikçe düşünüyor, ve
temizleme düşünmeye değecek bir iş değil. Aşağıdaki örnekte ölçüldü: düşünme aynı
cümle için altı kat uzun sürdü, çıktı token'larının %95'ini akıl yürütmeye
harcadı ve zaman zaman yapıştıracak hiçbir şey döndürmedi. Bu yüzden Dikte,
DeepSeek'in temizlemesini **Düşünme: Kapalı** ile getiriyor; düşünmeye değen
tutanağı ise düşünmeye bırakıyor.

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

- **Yazıya çevirme bu makinede.** whisper.cpp, Dikte'nin yanında bir sunucu
  olarak ayakta tutuluyor ve bulut sağlayıcılarının kullandığı
  `/v1/audio/transcriptions` yoluna oturtuluyor; ikinci bir kod yolu değil de
  bir base URL daha olmasını sağlayan bu: dikte, dosya transkripsiyonu, altyazı
  ve toplantı hiç değişmeden bunun üzerinden geçiyor. Model ilk diktede değil
  Dikte açılırken yükleniyor, böylece birkaç saniyelik konuşma söylendiği kadar
  sürede geri geliyor — yavaş olan kısım yükleme, çevirme değil. Ekran kartı
  belleğini boş tutmak istersen Ayarlar → Yerel whisper altından kapat.
- **Sessizlik modele gitmez.** Sessize yakın bir ses verildiğinde model boş dize
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

## Global kısayol için bir kez oturum kapatmak gerekir

KWin `kglobalshortcutsrc` dosyasını yalnızca açılışta okur, yani `install.sh`'ın
yazdığı kısayol oturumu yeniden açana kadar tetiklenmez. O zamana kadar Ayarlar →
Kısayol → **yerleşik dinleyici** `/dev/input` üzerinden kombinasyonu kendisi
yakalar. Tek farkı: tuşu yutmaz, yani `Ctrl+Space` odaktaki uygulamaya da iletilir
(bazı editörlerde otomatik tamamlama açılabilir). Dinleyici kullanıcının `input`
grubunda olmasını gerektirir: `sudo usermod -aG input $USER`.

## Dosyalar

```
dikte.py          giriş noktası, tepsi simgesi, durum makinesi, IPC
audio.py          PCM kaydı: diktede pw-record, toplantıda ffmpeg
meeting.py        kanal ayırma, konuşmacı etiketi, temizleme, tutanak
assistant.py      dikteyi Claude Code, Codex ya da OpenRouter'dan geçirme
api.py            her sağlayıcıda transkript + OpenRouter temizleme (yalnız stdlib)
whispercpp.py     yerel whisper.cpp sunucusu ve model indirmeleri
worker.py         transkript → temizleme → pano → yapıştırma
vad.py            kayıtta gerçekten konuşma var mı kararı
filetranscribe.py dosyadan transkript: ffmpeg, parçalama, zaman damgaları
overlay.py        köşedeki gösterge
settings_ui.py    ayarlar penceresi
hotkey.py         KDE kısayol kurulumu ve evdev dinleyici
paste.py          wl-clipboard ve ydotool sarmalayıcıları
i18n.py           metin tablosu
```

Gösterge XWayland üzerinden çizilir; Wayland'da bir pencereyi belirli bir köşeye
yerleştirmenin yolu yok, `dikte.py` bu yüzden `QT_QPA_PLATFORM=xcb` ayarlar.

## Lisans

GPL-3.0, [LICENSE](LICENSE) dosyasına bak.
