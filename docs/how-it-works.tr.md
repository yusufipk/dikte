# Dikte nasıl çalışıyor

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
  ayakta tutar ve on dakika kullanılmayan modelin belleğini geri verir. Model
  listesi dosya boyutuna değil modele göre gruplanır ve bu
  makinenin belleğine ve ekran kartına uyan satır işaretlenir. Derleme
  destekliyorsa ekran kartına CUDA, ROCm ya da Vulkan
  üzerinden ulaşılır. Anahtar yok, hesap yok, makineden çıkan bir şey yok.
  x86_64 Linux'ta ve ekran kartı sürücüsü kurulu Windows'ta aynı düğme,
  whisper-server'ın Dikte'nin kendi yayınladığı Vulkan derlemesini indirir;
  upstream ikisi için de Vulkan derlemesi yayınlamıyor.
- **Sessizlik API'ye gitmez.** Sessize yakın bir ses verildiğinde model boş dize
  döndürmez, bir cümle uydurur ("Altyazı M.K.", "Thanks for watching"). *O
  kaydın kendi* gürültü tabanının 10 dB üstüne en az 0,3 saniye çıkan bir şey
  yoksa kayıt atılır; ne kadar yüksek olursa olsun sabit fanı ya da cızırtıyı
  eleyen de budur. Kaydın gürültülü ucu -55 dBFS altındaysa da atılır. Gösterge
  ölçtüğü seviyeyi yazar, eşiği ona bakarak ayarlarsın.
- **Yanlış duyulan kelimeler düzeltilir.** Konuşma modelleri özel isimlerde sesçe
  benzer bir şeye kayıyor; temizleme modelinden bunları bağlamdan onarması,
  bağlam netleştirmiyorsa dokunmaması isteniyor. Sözlük sekmesine
  yazdığın isimler transkripsiyon modeline ipucu, temizleme modeline sözlük
  olarak gidiyor; "kuber netis"i tanımasını sağlayan da bu (programa göre üslup
  seçen ya da çeviri yapan yazım profilleri de aynı sözlüğü kullanıyor):

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
  olmanı sağlayan da budur. Codex (`codex exec`) ile Antigravity (`agy -p`) da
  aynı şekilde çalışır; ama Antigravity'ye Dikte bir izin kipi ya da sandbox
  veremiyor, sormadan ne yapabileceğini kendi allow-rule'ları belirliyor.
  OpenRouter, OpenCode Go ya da Requesty ise hiçbiri kurulu olmayan bir
  makinede düz soru
  cevap için duruyor. Sağlayıcı, model, izinler ve çalışma dizini, seçili metin
  hakkında soran isteğe bağlı kısayolla birlikte Ayarlar → Ajan sekmesinde; arka
  arkaya verilen komutlar tek bir konuşmada kalır.
- **Toplantılar** mikrofonla hoparlör çıkışından aynı anda kaydedilir; kimin ne
  dediği tahmin edilmez, sesin hangi kanaldan geldiğiyle belli olur. İki taraf
  ayrı ayrı yazıya çevrilip tek bir zaman damgalı transkriptte birleştirilir,
  ardından Ayarlar → Toplantı sekmesinden seçtiğin ikinci bir model kendi
  talimatıyla bunu tutanağa çevirir: kararlar, aksiyonlar, açık sorular. Sonuç
  `~/.local/share/dikte/meetings` altına ve Ayarlar → Tutanaklar sekmesine
  düşer. Yarıda kalan bir işlem ses kaydını saklar, yeniden denemede parası
  ödenmiş transkriptin üstünden devam eder.
- **Ses ve video dosyaları** Ayarlar → Ses dosyası sekmesinde aynı modellerden
  geçer; istersen `[dd:ss]` zaman damgalarıyla, konuşmacı ayırabilen bir modelde
  konuşmacı etiketleriyle, uzun dosyalar ffmpeg ile
  parçalanarak, sonuç `.txt` ya da `.srt` altyazı olarak kaydedilerek; temizleme
  burada kendi kurallarıyla, altyazı için yazılmış haliyle çalışır: satırlar
  yerinde kalır, hiçbir şey kısaltılmaz.
- **Geçmiş** Ayarlar → Geçmiş sekmesinde; boyut sınırı var, sağ tıklayıp
  silebilirsin.
- **Konuşma dili seçilmez, algılanır.** Varsayılan otomatiktir: bu makinedeki
  whisper ne duyduğunu söyler, bulut sağlayıcılar söylenmeden de hangi dilde
  konuşuluyorsa o dilde yazar; algılanan dil geçmişe düşer ve bir kaydın hangi
  temizleme promptunu alacağını belirler (Türkçe mi, dile duyarsız olanı mı).
  Sabit bir dil yine de bunun önüne geçer.
- **Türkçe ve İngilizce arayüz**, varsayılan olarak sistem dilini izler.

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
assistant.py      dikteyi Claude Code, Codex, agy ya da sohbet modelinden geçirme
api.py            transkript ve temizleme istekleri (yalnız stdlib)
cleanup.py        transkripti kim temizler: bulutta bir model, burası, bir CLI
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
