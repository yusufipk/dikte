# Dikte'yi kurmak

[Sürümler sayfasında](https://github.com/yusufipk/dikte/releases) bir AppImage, her Mac mimarisi için
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

Yerel transkripsiyon için whisper.cpp'nin sunucusu gerekiyor, onu
`brew install whisper.cpp` kuruyor ve Dikte Finder'dan açılsa bile buluyor.
Toplantı için BlackHole veya Loopback gerekiyor
(`brew install blackhole-2ch`); dikte için gerekmiyor.

Windows da aynı şekilde çalışıyor, Dikte açıkken kombinasyonu sistemin kendi
kısayol servisi üzerinden tutuyor. Sürümler sayfasındaki kurulum kaydın
istediği ffmpeg'i de taşıyor ve yönetici istemiyor; checkout'tan ise `winget
install Gyan.FFmpeg`, `pip install PyQt6`, sonra `python -m dikte`, Başlat
Menüsü girdisi ve `dikte` komutu için de isteğe bağlı `install.ps1`. Orada
toplantı kaydı henüz yok, ayrıntılar [Windows README](../README.windows.md)'sinde.

`install.sh` `dikte` komutunu, menü girdisini, oturum açılışında otomatik
başlatmayı ve iki global kısayolu kurar; tuşları iki argümanı, argüman
verilmezse ayarlarında duranlar. `./scripts/update.sh` son sürümü çeker ve
bunları yerine koyar; `./scripts/uninstall.sh` hepsini geri alır, `--purge`
demedikçe ayarlarına ve diktelerine dokunmaz. Dikte sürüm sayfasına günde bir
kez bakar ve yeni sürüm çıkmışsa tepsi menüsüne bir satır koyar; o satır bir şey
kurmaz, sayfayı açar. Genel sekmesi bu denetimi kapatır ya da anında çalıştırır.

## Sağlayıcılar ve anahtarlar

Sesi yazıya çevirme ve temizleme sağlayıcılarını Ayarlar'dan ayrı ayrı seç.
Sesi yazıya çevirme varsayılan olarak yerelde, temizleme ise bulutta çalışır.
Temizleme için yerel bir model veya kurulu bir CLI da kullanabilirsin.
Temizlemeyi kapatırsan ham transkript yapıştırılır.

| İşlem | Bulut sağlayıcıları | Diğer seçenekler |
| --- | --- | --- |
| Sesi yazıya çevirme | OpenAI, Groq, OpenRouter, Requesty, Google AI Studio, OpenAI uyumlu herhangi bir sunucu | Yerel model |
| Temizleme | OpenRouter, Requesty, Google AI Studio, DeepSeek, OpenCode Go, OpenAI uyumlu herhangi bir sunucu | Yerel model; Claude Code, Codex veya Antigravity CLI |

Modelleri ve temizleme için düşünme seviyesini Ayarlar'dan seçebilirsin. API
anahtarlarını da buraya girebilirsin; boş bırakılan anahtarlar için
`OPENAI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `REQUESTY_API_KEY`,
`GEMINI_API_KEY`, `DEEPSEEK_API_KEY` ve `OPENCODE_API_KEY` kullanılır.

Ayarlar ve uygulamaya girdiğin anahtarlar `config.json` dosyasına kaydedilir:
Linux'ta `~/.config/dikte` (tanımlıysa `$XDG_CONFIG_HOME/dikte`), macOS'ta
`~/Library/Application Support/Dikte`, Windows'ta `%APPDATA%\Dikte` altında.
Linux ve macOS'ta dosya 600 izinleriyle yazılır.

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
