# Dikte Windows Portu — Mimari ve Uygulama Planı

> Durum: uygulandı. Aşama 1-10 tamamlandı, kalan iş elle doğrulama matrisi.
> Tarih: 2026-08-02, uygulama 2026-08-03
> Kapsam: Linux'a bağlı Dikte uygulamasının Windows'ta yerel ve kurulabilir bir ürün hâline getirilmesi

Ne durumda olduğu aşağıdaki "Tamamlanmış sayılma ölçütü" bölümünde satır satır
yazıyor. Kısaca: kod yazıldı, 972 test Windows'ta yeşil (86'sı Linux'a özgü
olduğu için atlanıyor), paketlenmiş sürüm bu makinede kuruldu ve çalıştırıldı;
gerçek donanım gerektiren maddeler (bir saatlik toplantı, Bluetooth mikrofon,
çoklu monitör, temiz bir Windows kurulumu) hâlâ elle doğrulanmayı bekliyor.

İnceleme sonucundaki öneri net: uygulama C/C++ ile baştan yazılmayacak. Python/PyQt çekirdeği korunup Linux'a bağlı parçalar Linux ve Windows adaptörlerine ayrılacak. Bu, mevcut iş mantığını ve test yatırımını korurken Windows'ta yerel davranış sağlamanın en güvenli yolu.

---

## Mevcut projenin yapısı

Uygulama yaklaşık şu akışla çalışıyor:

```text
Global kısayol / tepsi
        ↓
Mikrofon kaydı
        ↓
Sessizlik ve halüsinasyon kontrolü
        ↓
Whisper.cpp veya bulut transkripsiyonu
        ↓
Temizleme modeli
        ↓
Windows panosu
        ↓
Odaktaki uygulamaya yapıştırma
```

Ana parçalar:

| Bölüm | Dosyalar | Windows durumu |
|---|---|---|
| Uygulama durum makinesi ve tepsi | [dikte.py](../dikte.py) | Büyük ölçüde korunacak |
| Transkripsiyon ve API | [api.py](../api.py) | Korunacak, birkaç Windows hatası düzeltilecek |
| Dikte işlem hattı | [worker.py](../worker.py) | Korunacak |
| Sessizlik algılama | [vad.py](../vad.py) | Tamamen korunabilir |
| Temizleme ve ajanlar | [cleanup.py](../cleanup.py), [assistant.py](../assistant.py) | Korunacak, süreç yönetimi uyarlanacak |
| Dosya transkripsiyonu | [filetranscribe.py](../filetranscribe.py) | FFmpeg ile çalışmaya devam edecek |
| Toplantı işleme | [meeting.py](../meeting.py) | İş mantığı korunacak |
| Yerel modeller | [ggml.py](../ggml.py) | Windows ikilileri ve süreç yönetimi eklenecek |
| Ayarlar ve veri | [config.py](../config.py) | Windows dizinleri ve güvenli anahtar saklama eklenecek |
| Mikrofon ve sistem sesi | [audio.py](../audio.py) | Windows adaptörü yazılacak |
| Pano ve tuş gönderme | [paste.py](../paste.py) | Win32 adaptörü yazılacak |
| Global kısayollar | [hotkey.py](../hotkey.py) | Win32 adaptörü yazılacak |
| IPC | [ipc.py](../ipc.py) | Kullanıcı kimliği ve tek örnek kontrolü değişecek |
| Arayüz | [settings_ui.py](../settings_ui.py), [overlay.py](../overlay.py) | Platforma göre metin ve pencere bayrakları değişecek |

---

## Şu anda Windows'ta kırılan noktalar

Mevcut kodun Linux'a doğrudan bağlı olduğu alanlar:

- Mikrofon `parec` veya `pw-record` ile kaydediliyor.
- Toplantı sesi `ffmpeg -f pulse` ile mikrofon ve PipeWire/PulseAudio monitor aygıtından alınıyor.
- Aygıtlar `pactl` ile listeleniyor.
- Pano `wl-copy`, `wl-paste` veya `xclip` ile yönetiliyor.
- Yapıştırma `ydotool` veya `xdotool` ile yapılıyor.
- Global kısayollar KDE/GNOME dosyaları ve `/dev/input` üzerinden kuruluyor.
- Kullanıcıya özel IPC adı `os.getuid()` kullanıyor; Windows'ta bu fonksiyon yok.
- Veri dizinleri `~/.config`, `~/.local/share` ve `~/.cache`.
- Yerel model yöneticisi yalnızca Ubuntu `.tar.gz` varlıklarını arıyor.
- Yetim model sunucularını `/proc/<pid>/cmdline` üzerinden tanıyor.
- Kurulum, güncelleme ve kaldırma tamamen Bash/Linux masaüstü düzenine bağlı.
- Tepsi ikonu yalnızca Linux ikon temasından alınıyor; Windows için paketlenmiş ikon yok.
- Yeniden başlatma, kaynak dosyası ve Python yorumlayıcısı varsayıyor; paketlenmiş `.exe` için çalışmaz.

---

## Windows'ta yapılan başlangıç testi

Bundled Python 3.12 ile testler çalıştırıldı. PyQt6 mevcut olmadığı için arayüz ve Qt kullanan modüllerin bir bölümü yüklenemedi. Buna rağmen 468 test çalıştı; 49 Linux testi atlandı.

Windows'a özgü dört gerçek problem şimdiden ortaya çıktı:

- `.wav` MIME türü Windows'ta `audio/wav`, testin beklediği Linux değeriyse `audio/x-wav`.
- İptal edilen HTTP isteği WinSock üzerinde bekleyen okuma işlemini zamanında kesmiyor.
- `chmod(0600)` Windows'ta API anahtarlarını korumuyor.
- İptal edilen model indirmesi, dosya hâlâ açıkken `.part` dosyasını silmeye çalıştığı için `WinError 32` alıyor.

Dolayısıyla çalışma yalnızca Linux araçlarını değiştirmekten ibaret olmayacak; Windows dosya kilitleme ve süreç davranışları da ele alınacak.

Ayrıca klasör bir Git çalışma kopyası değil; `.git` dizini bulunmuyor. Bu nedenle mevcut `update.sh` bu kopyada Linux'ta bile çalışamaz. Uygulamaya başlamadan önce güvenli bir sürüm geçmişi oluşturulması gerekecek.

---

## Önerilen mimari

Yeni yapı kabaca şöyle olacak:

```text
Ortak uygulama çekirdeği
├── API, VAD, cleanup, geçmiş, toplantı, CLI, arayüz
└── platform adaptörleri
    ├── Linux
    │   ├── PipeWire/PulseAudio
    │   ├── wl-clipboard/X11
    │   └── KDE/GNOME/evdev
    └── Windows
        ├── WASAPI
        ├── Win32 Clipboard + SendInput
        ├── RegisterHotKey
        ├── Named pipe + mutex
        └── AppData + DPAPI + Job Object
```

Örneğin mevcut `audio.Recorder`, `paste.copy()` ve `hotkey` dış arayüzleri mümkün olduğunca değişmeyecek. İçeride çalışan uygulama işletim sistemine göre Linux veya Windows uygulamasını seçecek. Böylece `worker.py`, `meeting.py`, `dikte.py` gibi yüksek seviyeli parçalar hangi işletim sisteminde olduklarını bilmek zorunda kalmayacak.

---

## Aşama 1: Platform sınırlarını ayırma

Önce davranış değiştirmeden aşağıdaki adaptör yapısı oluşturulacak:

```text
platforms/
├── common/
├── linux/
│   ├── audio.py
│   ├── clipboard.py
│   ├── hotkeys.py
│   └── runtime.py
└── windows/
    ├── audio.py
    ├── clipboard.py
    ├── hotkeys.py
    └── runtime.py
```

Bu aşamada:

- Mevcut Linux kodu korunup Linux adaptörlerine taşınacak.
- `audio.py`, `paste.py`, `hotkey.py` dışarıya aynı sözleşmeyi sunan cepheler olacak.
- Testlere `windows_only` karşılığı eklenecek.
- Linux CI çalışmaya devam edecek.
- Windows CI başlangıçta yalnızca platformdan bağımsız testleri çalıştıracak.

**Kabul şartı:** Linux davranışında gerileme olmaması ve ortak testlerin iki platformda geçmesi.

---

## Aşama 2: Windows ses adaptörü

En kritik bölüm budur.

### Dikte kaydı

Windows adaptörü için öneri `PyAudioWPatch` üzerinden WASAPI kullanmak. Bu paket Windows giriş aygıtlarını ve WASAPI loopback aygıtlarını sunuyor; Python 3.11–3.13 için Windows wheel'leri mevcut. Windows'un kendisi WASAPI loopback ile fiziksel bir "Stereo Mix" aygıtı gerektirmeden sistem çıkışını kaydedebiliyor.
Kaynaklar: [Microsoft WASAPI loopback belgeleri](https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording), [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch).

Yeni `WindowsRecorder`:

- Varsayılan veya seçilmiş mikrofonu açacak.
- PCM bloklarını mevcut `chunk_levels()` fonksiyonuna verecek.
- Canlı waveform sinyalini koruyacak.
- 16 kHz, mono, signed 16-bit WAV üretecek.
- `stop`, `cancel`, maksimum kayıt süresi ve aygıtın kaybolması davranışlarını mevcut sınıfla aynı tutacak.
- Mikrofon gizlilik izni kapalıysa kullanıcıyı Windows ayarlarına yönlendiren açık hata gösterecek.

### Toplantı kaydı

Toplantıda iki ayrı akış açılacak:

- Mikrofon
- Seçilen çıkış aygıtının WASAPI loopback akışı

Bu akışlar doğrudan yan yana yazılmayacak. Her ses bloğu zaman damgasıyla kuyruğa girecek; bir birleştirici:

- İki tarafı ortak 16 kHz zaman çizgisine çevirecek.
- Eksik bloklara sessizlik ekleyecek.
- Başlangıç farkını düzeltecek.
- Uzun kayıtlardaki saat kaymasını sınırlı biçimde telafi edecek.
- Sol kanala mikrofonu, sağ kanala sistem sesini yazacak.
- Çökme hâlinde o ana kadarki WAV dosyasını geçerli bırakacak.

İlk teknik prototipte 30 ve 60 dakikalık yapay kayıtlarla kanallar arası kayma ölçülecek. **Hedef: bir saatte en fazla yaklaşık 250 ms kayma.**

PyAudioWPatch bu kaliteyi sağlayamazsa yalnızca ses yakalama için küçük bir yerel WASAPI yardımcı programına geçilir. Bu, tüm uygulamayı C/C++ ile yeniden yazmak anlamına gelmez; Python uygulamasının çağırdığı dar kapsamlı bir ses bileşeni olur.

---

## Aşama 3: Pano ve otomatik yapıştırma

Windows adaptörü:

- Unicode metni Win32 Clipboard API ile yazacak.
- Pano başka bir uygulama tarafından kısa süreli kilitliyse artan gecikmeyle tekrar deneyecek.
- `Ctrl+V`, `Ctrl+Shift+V` ve `Shift+Insert` kombinasyonlarını `SendInput` ile gönderecek.
- Tuşları basma sırasıyla bırakma sırasını doğru yönetecek.
- Önceki pano içeriğini geri yükleme seçeneği için metin, HTML/RTF ve desteklenen standart formatlardan bir snapshot alacak.

Windows'ta `SendInput`, daha yüksek yetki seviyesinde çalışan bir programa tuş gönderemez. Yani Dikte normal çalışırken yönetici olarak açılmış bir uygulamaya otomatik yapıştırma engellenebilir; metin yine panoya kopyalanacak ve kullanıcıya açıklama gösterilecek. Dikte sürekli yönetici olarak çalıştırılmayacak.
Kaynaklar: [SendInput ve UIPI sınırı](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput), [Windows pano işlemleri](https://learn.microsoft.com/en-us/windows/win32/dataxchg/clipboard-operations).

---

## Aşama 4: Global kısayollar

Windows'ta KDE/GNOME dosyaları veya `/dev/input` kullanılmayacak.

- Görünmeyen küçük bir Qt/Win32 pencere handle'ı oluşturulacak.
- Her kombinasyon `RegisterHotKey` ile sisteme kaydedilecek.
- `WM_HOTKEY` mesajları mevcut `toggle`, `cancel`, `ask` ve `meeting` işlemlerine çevrilecek.
- `MOD_NOREPEAT` ile tuşa basılı tutulması tekrarlı tetiklemeyecek.
- Çakışan veya Windows tarafından ayrılmış kombinasyonlar kullanıcıya bildirilecek.
- Ayar değiştiğinde önce yeni kombinasyon denenecek; başarısızsa eski çalışan kombinasyon korunacak.
- Uygulama kapanırken hepsi `UnregisterHotKey` ile bırakılacak.

`RegisterHotKey` doğrudan sistem çapında kısayol sağlar: [Microsoft RegisterHotKey belgeleri](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey).

Windows'ta uygulama oturum açılışında çalışacağı için kısayollar her zaman hazır olacak. Ayarlardaki KDE, KWin ve `/dev/input` metinleri Windows'ta gösterilmeyecek.

---

## Aşama 5: IPC, tek örnek ve süreç yaşam döngüsü

Qt'nin `QLocalServer` sınıfı Windows'ta named pipe kullanabiliyor. Ancak Windows'ta aynı pipe adına iki sunucu bağlanabildiği için mevcut kod tek örnek garantisi vermiyor: [Qt QLocalServer belgeleri](https://doc.qt.io/qt-6/qlocalserver.html).

Yapılacaklar:

- `os.getuid()` yerine Windows kullanıcı SID'sinin hash'i kullanılacak.
- IPC pipe adı kullanıcıya özel olacak.
- Uygulama başında kullanıcıya özel named mutex oluşturulacak.
- İkinci örnek açılırsa yeni tepsi uygulaması oluşturmak yerine mevcut örneğe komut gönderip kapanacak.
- Kaynak koddan çalışma ile paketlenmiş `.exe` çalışma yolu ayrılacak.
- `restart` artık `dikte.py` dosyasını varsaymayacak.
- Unix'e özgü `SIGHUP` ve wakeup-fd kodu platform adaptörüne taşınacak.
- Oturum kapatma ve Windows kapanışında toplantı WAV başlığı kapatılacak.

Whisper, llama, FFmpeg, Claude ve Codex alt süreçleri Windows'ta konsol penceresi açmadan çalıştırılacak. Model sunucuları Windows Job Object içine alınacak; ana uygulama çöker veya zorla kapanırsa yetim model süreçleri bellekte kalmayacak.

---

## Aşama 6: Windows dizinleri ve API anahtarları

Önerilen dizinler:

- Ayarlar: `%APPDATA%\Dikte\config.json`
- Modeller ve uygulama verisi: `%LOCALAPPDATA%\Dikte`
- Cache: `%LOCALAPPDATA%\Dikte\Cache`
- Geçmiş: `%LOCALAPPDATA%\Dikte\history.jsonl`
- Toplantılar: `%LOCALAPPDATA%\Dikte\Meetings`
- Korunan kayıtlar: `%LOCALAPPDATA%\Dikte\Recordings`

Linux XDG yolları değişmeyecek.

`chmod(0600)` Windows'ta güvenlik sağlamadığından API anahtarları DPAPI `CryptProtectData` ile mevcut Windows kullanıcı profiline bağlı şekilde şifrelenecek. Config dosyasında düz anahtar yerine sürümlü bir `dpapi:` değeri bulunacak. Eski düz metin anahtar görülürse okunacak ve bir sonraki kayıtta şifreli biçime geçirilecek.

---

## Aşama 7: Yerel whisper.cpp ve llama.cpp

Mevcut kod yalnızca Ubuntu `.tar.gz` varlıklarını seçiyor. Windows için:

- `.zip` arşivleri desteklenerek zip-slip koruması eklenecek.
- `whisper-server.exe` ve `llama-server.exe` aranacak.
- Windows x64 CPU varlığı temel ve güvenilir seçenek olacak.
- Kullanıcıya `Auto`, `CPU`, `CUDA` ve uygun olduğunda `Vulkan` backend seçimi verilecek.
- Başarısız GPU başlatmasında açık hata gösterilecek; sessizce yanlış backend kullanılmayacak.
- DLL'ler `.exe` yanında tutulacak.
- GitHub tarafından yayımlanan SHA-256 değerleri doğrulanmaya devam edecek.

Güncel whisper.cpp sürümleri Windows x64, BLAS ve CUDA paketleri yayımlıyor; llama.cpp de Windows CPU, CUDA, Vulkan, SYCL ve HIP seçenekleri sunuyor.
Kaynaklar: [whisper.cpp güncel yayın varlıkları](https://api.github.com/repos/ggml-org/whisper.cpp/releases/latest), [llama.cpp yayınları](https://github.com/ggml-org/llama.cpp/releases).

Burada ayrıca Windows testinde yakalanan açık `.part` dosyasını silme problemi düzeltilecek: dosya önce kapatılacak, sonra silinecek veya atomik olarak yerine taşınacak.

---

## Aşama 8: Arayüz düzenlemeleri

Ayarlar penceresinde:

- Linux/KDE/GNOME metinleri platforma göre değişecek.
- `/dev/input` seçeneği Windows'ta bulunmayacak.
- Mikrofon ve sistem çıkışı gerçek Windows aygıt adlarıyla gösterilecek.
- "Windows mikrofon erişimi kapalı" gibi eyleme dönük hatalar eklenecek.
- Veri ve model klasörlerini Explorer'da açma düğmeleri eklenecek.
- Yerel backend seçimi CPU/CUDA/Vulkan şeklinde görünür olacak.
- `doctor` komutu Windows bileşenlerini denetleyecek.
- Yeni tüm metinlerin Türkçe karşılıkları [i18n.py](../i18n.py) içine eklenecek.

Overlay için:

- `X11BypassWindowManagerHint` Windows'ta kullanılmayacak.
- Pencere `topmost`, `no-activate` ve `tool window` davranışıyla açılacak.
- Windows ölçekleme, çoklu monitör ve görev çubuğu konumu test edilecek.
- Tema ikonuna bağlı kalmamak için `.ico` ve PNG kaynakları paketlenecek.

---

## Aşama 9: Paketleme ve kurulum

Windows kullanıcısından Python, PyQt veya ayrı komut satırı araçları kurması istenmeyecek.

Önerilen dağıtım:

- PyInstaller "one-folder" paket
- `DikteApp.exe`: tepsi uygulaması, konsolsuz
- `dikte.exe`: PowerShell/Terminal CLI
- Inno Setup ile tek kullanıcıya kurulan installer
- Başlat menüsü kısayolu
- İsteğe bağlı PATH ekleme
- `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` üzerinden otomatik başlatma
- Program Files yerine kullanıcıya özel kurulum; yönetici izni gerektirmeyecek
- Kaldırırken ayarlar ve kayıtlar varsayılan olarak korunacak
- "Tüm verileri sil" ayrı ve açık bir seçenek olacak

Windows, `Run` anahtarını kullanıcı oturum açtığında uygulamayı çalıştırmak için destekliyor: [Microsoft Run anahtarı belgeleri](https://learn.microsoft.com/en-us/windows/win32/setupapi/run-and-runonce-registry-keys).

PyInstaller PyQt uygulamalarını ve bağımlılıklarını paketleyebiliyor; Windows paketi Windows makinesinde üretilmek zorunda: [PyInstaller belgeleri](https://pyinstaller.org/en/stable/).

FFmpeg'in sabitlenmiş ve hash'i doğrulanmış Windows sürümü pakete eklenecek; lisans bildirimleri installer'a konacak. FFmpeg özellikle ses/video dosyası transkripsiyonunda kalmaya devam edecek. DirectShow yalnızca mikrofon tarafını çözebildiği için toplantı loopback kaydının temel yöntemi olmayacak: [FFmpeg Windows DirectShow belgeleri](https://www.ffmpeg.org/ffmpeg-devices.html#dshow).

İlk sürümde güncelleme `update.sh` benzeri `git pull` olmayacak. Yeni imzalı installer eskisinin üzerine kurulum yapacak. Otomatik güncelleyici daha sonra, sürüm manifesti ve imza doğrulamasıyla eklenebilir.

---

## Aşama 10: Test ve CI

CI matrisi:

- Ubuntu + Python 3.11, 3.12, 3.13
- Windows + Python 3.11, 3.12, 3.13
- PyInstaller paketleme smoke testi
- Windows installer smoke testi

Yeni Windows testleri:

- WASAPI aygıt listeleme ve varsayılan aygıt çözümleme
- Mikrofon başlangıç/stop/cancel
- Loopback ve mikrofonun iki kanala doğru yerleşmesi
- Uzun toplantıda drift ve eksik blok doldurma
- Clipboard UTF-16 ve Türkçe karakterler
- Pano kilitliyken retry
- `SendInput` tuş basma/bırakma sırası
- Hotkey kayıt, çakışma, kaldırma ve rollback
- IPC named pipe ve tek örnek mutex
- DPAPI şifreleme/migrasyon
- Windows `.zip` model varlığı seçimi
- Zip-slip koruması
- `.exe` ve DLL keşfi
- İndirme iptali sırasında Windows dosya kilitlemesi
- WinSock üzerinde HTTP iptali
- Paketlenmiş uygulamada restart ve CLI→GUI başlatma

Manuel doğrulama matrisi:

- Windows 10 22H2 ve Windows 11 x64
- Dahili, USB ve Bluetooth mikrofon
- Dahili, HDMI, USB ve Bluetooth çıkışı
- Kulaklık çıkarma/takma ve varsayılan aygıt değiştirme
- Uyku/uyanma ve kilit ekranı
- Notepad, Chrome, VS Code, Word ve Windows Terminal'e yapıştırma
- Yönetici olarak çalışan hedef uygulama
- %100, %150 ve %200 ölçekleme
- Tek ve çoklu monitör
- Bir saatlik toplantı
- Tamamen çevrimdışı whisper.cpp kullanımı
- Unicode/Türkçe dosya ve kullanıcı adları

---

## Tamamlanmış sayılma ölçütü

Kodun ve testlerin karşıladığı maddeler işaretli. Yanındaki not, o maddenin
neyle doğrulandığını söylüyor: bir test mi, bu makinede yapılan bir deneme mi,
yoksa hâlâ elle bakılması gereken bir şey mi.

- [x] Temiz Windows sistemine Python kurmadan yüklenebilmesi — PyInstaller
      paketi ve Inno Setup kurulum dosyası üretiliyor, paketlenmiş `dikte.exe`
      bu makinede Python'suz çalıştı. **Temiz bir makinede kurulum denenmedi.**
- [x] Tepsi ikonunun ve ayarlar penceresinin düzgün açılması — paketlenmiş
      `DikteApp.exe` başladı ve IPC'ye cevap verdi; simgeler Windows'ta simge
      teması olmadığı için [icons.py](../icons.py) tarafından çiziliyor.
      **Pencerenin görünümü gözle kontrol edilmedi.**
- [x] Global kısayolun farklı uygulamalarda çalışması — paketlenmiş sürümde
      başka bir pencere odaktayken gönderilen `Ctrl+Space` kaydı başlattı.
- [x] Türkçe konuşmanın doğru kaydedilip transkribe edilmesi — WASAPI kaydı
      16 kHz mono WAV üretiyor (gerçek mikrofonla denendi). **Transkripsiyon
      tarafı değişmedi; model/anahtar gerektirdiği için burada denenmedi.**
- [x] Sonucun panoya kopyalanması ve normal yetkili hedeflere yapıştırılması —
      Win32 pano gerçek panoya karşı test ediliyor (Türkçe karakterler dahil);
      `SendInput` tuş sırası ve UIPI reddi ayrı testlerde.
- [x] Mikrofon seçiminin kalıcı olması — aygıtlar indeksle değil adla saklanıyor,
      indeksler her takıp çıkarmada değiştiği için.
- [x] Yerel whisper.cpp modelinin doğrulanarak indirilmesi ve çevrimdışı
      çalışması — Windows `.zip` varlıkları, zip-slip koruması, sha256 kontrolü
      ve CUDA runtime eşlikçisi testli. **Gerçek bir model indirilip
      çalıştırılmadı.**
- [x] Bulut sağlayıcılarının mevcut davranışını koruması — tüm sağlayıcı
      testleri iki platformda da geçiyor; `.wav` MIME türü artık makineye değil
      Dikte'ye ait.
- [x] Dosya transkripsiyonu ve SRT üretiminin çalışması — testler geçiyor;
      ffmpeg kurulum paketine sha256'sı sabitlenmiş hâlde konuyor.
- [x] Toplantının mikrofon ve sistem sesini ayrı kanallarda tutması — karıştırıcı
      duvar saatine yazıyor, susan tarafa sessizlik dolduruyor; kanal ayrımı ve
      sessizlik doldurma testli, örnekleme dönüştürücüsü tasarımı gereği
      kaymıyor. **Bir saatlik gerçek toplantı denenmedi.**
- [x] Çökme veya kapanma sonrası yetim model süreçlerinin kalmaması — model
      sunucuları kill-on-close Job Object içine alınıyor, pid dosyası ve süpürme
      Windows'ta komut satırını PEB üzerinden okuyor.
- [x] İkinci uygulama örneğinin açılmaması — adlandırılmış mutex; paketlenmiş
      sürümde ikinci `DikteApp.exe` "already running" deyip çıktı.
- [x] Kaldırmanın kullanıcı verisini varsayılan olarak silmemesi — kurulum
      betiği soruyor ve varsayılan cevap "hayır". **Kaldırma denenmedi.**
- [x] Windows ve Linux testlerinin birlikte yeşil olması — Windows'ta 972 test
      geçiyor, 86'sı Linux'a özgü olduğu için atlanıyor. CI matrisi iki
      platformu da 3.11/3.12/3.13 ile çalıştırıyor. **Linux tarafı bu makinede
      çalıştırılamadı; CI'nin söylemesi gerekiyor.**

Kalan iş, plandaki elle doğrulama matrisi: Windows 10 22H2, Bluetooth ve USB
mikrofonlar, kulaklık takıp çıkarma, uyku/uyanma, %150 ve %200 ölçekleme,
çoklu monitör, bir saatlik toplantı, tamamen çevrimdışı whisper.cpp kullanımı.

Özetle ilk hedef "Windows'ta açılıyor" değil; Linux sürümündeki dikte, toplantı, yerel model, CLI, geçmiş ve ajan özelliklerinin Windows'ta yerel ve kurulabilir bir ürün olarak eşdeğer çalışması. En büyük teknik kapı WASAPI toplantı senkronizasyonu; onu ilk prototipte doğruladıktan sonra kalan çalışma oldukça kontrollü biçimde adaptörlere ayrılabilir.
