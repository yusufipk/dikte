# Dikte

`Ctrl+Space`'e bas, konuş, tekrar bas. Söylediğin yazıya çevrilir, temizlenir
ve o an yazdığın pencereye yapıştırılır. Linux'ta, macOS'ta ve
[Windows](README.windows.md)'ta çalışır, varsayılan olarak bu makinede.

*[English README](README.md)*

<p align="center">
  <img src="docs/home.webp" width="620" alt="Dikte, Nord teması">
  <br><sub>Nord (varsayılan)</sub>
</p>

| Dracula | Klasik karanlık | Klasik beyaz |
|---|---|---|
| <img src="docs/home-dracula.webp" width="270" alt="Dikte, Dracula"> | <img src="docs/home-dark.webp" width="270" alt="Dikte, Klasik karanlık"> | <img src="docs/home-light.webp" width="270" alt="Dikte, Klasik beyaz"> |

|  |  |
|---|---|
| <img src="docs/settings-general.webp" width="410" alt="Genel ve temalar"> | <img src="docs/settings-display.webp" width="410" alt="Nord, Dracula, dark, light"> |
| <img src="docs/settings-api.webp" width="410" alt="API ve modeller"> | <img src="docs/settings-cleanup.webp" width="410" alt="Temizleme kuralları"> |
| <img src="docs/settings-agent.webp" width="410" alt="Ajan"> | <img src="docs/settings-meeting.webp" width="410" alt="Toplantı"> |
| <img src="docs/settings-audio-file.webp" width="410" alt="Ses dosyası"> | <img src="docs/settings-shortcuts.webp" width="410" alt="Kısayollar"> |

## Özellikler

- **Sesi yazıya çevirme kendi bilgisayarında**, varsayılan olarak; modeli
  ayarlar penceresi senin yerine indirir. Temizleme de orada çalışabilir.
- **İstersen bulutta**, kendi anahtarınla OpenAI, Groq, OpenRouter, Google AI
  Studio ve diğerleriyle. Temizleme, başkasını seçene kadar OpenRouter'ı kullanır.
- **Temiz sonuç.** Dolgu sözcükleri, yarıda kalan cümleler, eksik noktalama ve
  modelin yanlış duyduğu isimler düzeltilir:

  ```
  ham    ıı bugün şey kuber netis üzerinde çalışan servisleri güncelledim
         yani sonra grafanada bir panel açtım hani ve pay kut ile arayüzü
         şey bitirdim işte

  sonuç  Bugün Kubernetes üzerinde çalışan servisleri güncelledim. Sonra
         Grafana'da bir panel açtım ve PyQt ile arayüzü bitirdim.
  ```
- **Yazım profilleri.** Her program için ayrı üslup, çeviri ve kullandığın
  isimlerin bir sözlüğü; istersen düzelttikçe öğrenir.
- **Sesli komut.** Claude Code'a, Codex'e ya da Antigravity'ye bir iş söyle,
  cevabı yapıştırılsın; ya da seçtiğin metin hakkında sor.
- **Toplantılar.** Mikrofonun ve karşı taraf birlikte kaydedilir, yazıya
  çevrilir ve tutanağa dönüşür. Windows'ta henüz yok.
- **Ses ve video dosyaları** metne ya da `.srt` altyazıya çevrilir.
- **Geçmiş** bütün diktelerini tutar, pencerenin yaptığı her şeyin bir de
  `dikte` komutu var.
- Türkçe ve İngilizce arayüz.

## Kurulum

[Sürümler sayfasından](../../releases) indir: Linux için AppImage, her Mac için
bir disk imajı, Windows için kurulum. AppImage yine de aşağıdaki sistem
paketlerini ister. Mac'te ilk açılış, Sistem Ayarları → Gizlilik ve Güvenlik
altından **Yine de Aç** diyene kadar reddedilir, kısayol da orada
`Ctrl+Option+Space`.

Arch'ta kaynaktan kurmak için:

```sh
sudo pacman -S --needed pipewire-audio wl-clipboard ydotool ffmpeg python-pyqt6
systemctl --user enable --now ydotool
./install.sh
dikte
```

Fedora, Ubuntu, macOS ve Windows'un adımları
[docs/install.tr.md](docs/install.tr.md)'de. KDE'de kısayollar bir sonraki
oturum açışında çalışmaya başlar; o zamana kadar Ayarlar → Kısayollar'dan
yerleşik dinleyiciyi aç, bunun için kullanıcının `input` grubunda olması gerekir.

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

Her parçanın nasıl çalıştığı ve kodunun nerede durduğu
[docs/how-it-works.tr.md](docs/how-it-works.tr.md)'de.

## Lisans

GPL-3.0, [LICENSE](LICENSE) dosyasına bak.
