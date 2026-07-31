"""Tiny translation helper.

Source strings are English; Turkish translations live in the TR table below.
No gettext, no .mo files; the string table is small enough to keep in code.
"""

from platform_utils import resolve_locale

_lang = "en"


def resolve(code):
    """'auto' -> the language the system is set to."""
    return resolve_locale(code)


def set_language(code):
    global _lang
    _lang = resolve(code)


def language():
    return _lang


def t(text, **kwargs):
    out = TR.get(text, text) if _lang == "tr" else text
    return out.format(**kwargs) if kwargs else out


# Turkish suffixes follow the vowels of the word they attach to, so "Claude'a"
# but "Codex'e". A name dropped into a sentence through t() cannot be inflected
# by the sentence, so it arrives already inflected. English takes the name as it
# is and puts the preposition in the sentence, where it belongs.
_TR_CASES = {
    "dative": {"Claude": "Claude'a", "Codex": "Codex'e", "OpenRouter": "OpenRouter'a"},
    "accusative": {"Claude": "Claude'u", "Codex": "Codex'i", "OpenRouter": "OpenRouter'ı"},
}


def name(text, case=""):
    if _lang != "tr" or not case:
        return text
    return _TR_CASES.get(case, {}).get(text, text)


TR = {
    # --- tray ---------------------------------------------------------
    "Start recording": "Kaydı başlat",
    "Stop and transcribe": "Kaydı bitir ve yaz",
    "Working…": "İşleniyor…",
    "Cancel recording": "Kaydı iptal et",
    "Settings…": "Ayarlar…",
    "Restart": "Yeniden başlat",
    "Quit": "Çık",
    "Dikte: ready": "Dikte: hazır",
    "Dikte: recording": "Dikte: kaydediyor",
    "Dikte: working": "Dikte: işleniyor",

    # --- overlay / pipeline -------------------------------------------
    "Transcribing…": "Yazıya çevriliyor…",
    "Cleaning up…": "Temizleniyor…",
    "Pasting…": "Yapıştırılıyor…",
    "Pasted": "Yapıştırıldı",
    "Copied": "Panoya kopyalandı",
    "{action}: {preview}": "{action}: {preview}",
    "Cleanup skipped: {error}": "Temizleme atlandı: {error}",
    "Pasted raw, cleanup failed: {error}": "Ham metin yapıştırıldı, temizleme başarısız: {error}",
    "Dikte: cleanup failed": "Dikte: temizleme başarısız",
    "{service} rejected the API key (HTTP {code}). Open Settings and check it.":
        "{service} API anahtarını reddetti (HTTP {code}). Ayarlar'ı açıp kontrol et.",
    "{service} says the account is out of credit (HTTP 402).":
        "{service} hesapta kredi kalmadığını söylüyor (HTTP 402).",
    "{service} is rate limiting you (HTTP 429). Try again in a moment.":
        "{service} hız sınırı uyguluyor (HTTP 429). Birazdan tekrar dene.",
    "The KDE shortcut is live now, so the built-in listener has been "
    "turned off. It was doubling every key press.":
        "KDE kısayolu artık çalışıyor, bu yüzden dahili dinleyici kapatıldı. "
        "Her tuşa basışı ikiye katlıyordu.",
    "No speech detected": "Ses algılanmadı",
    "No speech detected ({level} dB)": "Ses algılanmadı ({level} dB)",
    "Discarded a stock phrase: “{phrase}”": "Kalıp cümle atıldı: “{phrase}”",
    "Opening the microphone…": "Mikrofon açılıyor…",
    "Discard stock phrases models invent for near-silent audio":
        "Sessize yakın seste modelin uydurduğu kalıp cümleleri at",
    "Whisper answers silence with things like “Thanks for watching”.":
        "Whisper sessizliğe “Altyazı M.K.” gibi şeylerle karşılık verir.",
    "Speech also has to rise {margin} dB above the recording's own noise "
    "floor, so this absolute floor rarely needs touching. Lower it if quiet "
    "speech gets dropped; raise it if noise still gets through.":
        "Konuşmanın ayrıca kaydın kendi gürültü tabanının {margin} dB üstüne "
        "çıkması gerekir; bu mutlak taban nadiren değiştirilir. Kısık konuşma "
        "eleniyorsa düşür, gürültü hâlâ geçiyorsa yükselt.",
    "Recording too short, speak for at least 0.3 s": "Ses çok kısa, en az 0,3 saniye konuş",
    "Unexpected error: {error}": "Beklenmeyen hata: {error}",

    # --- audio / paste errors -----------------------------------------
    "pw-record not found. Is pipewire-audio installed?":
        "pw-record bulunamadı. pipewire-audio kurulu mu?",
    "Could not start recording: {error}": "Kayıt başlatılamadı: {error}",
    "wl-copy not found. Install wl-clipboard.":
        "wl-copy bulunamadı. wl-clipboard paketini kur.",
    "Could not copy to clipboard: {error}": "Panoya kopyalanamadı: {error}",
    "wl-copy exited with code {code}.": "wl-copy {code} koduyla çıktı.",
    "ydotool not found, cannot paste automatically.":
        "ydotool bulunamadı, otomatik yapıştırma yapılamıyor.",
    "Unknown key: {key}": "Bilinmeyen tuş: {key}",
    "Could not run ydotool: {error}": "ydotool çalıştırılamadı: {error}",
    "ydotool failed: {error}\nIs ydotoold running? (systemctl --user status ydotool)":
        "ydotool hatası: {error}\nydotoold çalışıyor mu? (systemctl --user status ydotool)",

    # --- audio / paste errors: Windows ---------------------------------
    "ffmpeg not found. Install it to record audio.":
        "ffmpeg bulunamadı. Ses kaydı için kur.",
    "No microphone found. Check that one is plugged in and allowed under "
    "Windows Settings → Privacy → Microphone.":
        "Mikrofon bulunamadı. Takılı olduğundan ve Windows Ayarlar → Gizlilik → "
        "Mikrofon altında izinli olduğundan emin ol.",
    "Windows has no device that records what the speakers are playing. Turn "
    "on \"Stereo Mix\": right-click the speaker icon → Sound settings → More "
    "sound settings → Recording, right-click in the list, show disabled "
    "devices, and enable it.":
        "Windows'ta hoparlörden çıkanı kaydeden bir cihaz yok. \"Stereo Mix\"i "
        "aç: hoparlör simgesine sağ tıkla → Ses ayarları → Diğer ses ayarları → "
        "Kayıt sekmesi, listede sağ tıklayıp devre dışı cihazları göster ve "
        "etkinleştir.",
    "Could not open clipboard.": "Pano açılamadı.",
    "Could not allocate memory for clipboard.": "Pano için bellek ayrılamadı.",
    "Could not lock clipboard memory.": "Pano belleği kilitlenemedi.",
    "Could not set clipboard data.": "Pano içeriği yazılamadı.",
    "Windows blocked the paste. The focused window is probably running as "
    "administrator; press {shortcut} yourself, the text is on the clipboard.":
        "Windows yapıştırmayı engelledi. Odaktaki pencere büyük ihtimalle "
        "yönetici olarak çalışıyor; {shortcut} tuşuna kendin bas, metin panoda.",

    # --- api errors ----------------------------------------------------
    "{service} API key is empty. Add it in Settings.":
        "{service} API anahtarı boş. Ayarlar'dan gir.",
    "Transcript came back empty.": "Transkript boş döndü.",
    "The cleanup model returned an empty reply.": "Temizleme modeli boş yanıt döndü.",
    "Could not connect: {reason}": "Bağlantı kurulamadı: {reason}",
    "Could not parse the response: {error}": "Yanıt çözümlenemedi: {error}",

    # --- settings: tabs and general ------------------------------------
    "Dikte Settings": "Dikte Ayarları",
    "General": "Genel",
    "API and models": "API ve modeller",
    "Cleanup rules": "Temizleme kuralları",
    "Audio file": "Ses dosyası",
    "Shortcut": "Kısayol",
    "History": "Geçmiş",
    "Save": "Kaydet",
    "Saved successfully.": "Başarıyla kaydedildi.",
    "Interface language": "Arayüz dili",
    "Automatic (system)": "Otomatik (sistem)",
    "Turkish": "Türkçe",
    "English": "İngilizce",
    "Restart Dikte for the language change to reach every window.":
        "Dil değişikliğinin her pencereye işlemesi için Dikte'yi yeniden başlat.",
    "Microphone": "Mikrofon",
    "Default microphone": "Varsayılan mikrofon",
    "Speech language": "Konuşma dili",
    "Detect automatically": "Otomatik algıla",
    "German": "Almanca",
    "French": "Fransızca",
    "Spanish": "İspanyolca",
    "Arabic": "Arapça",
    "Paste the text into the focused window": "Metni odaktaki pencereye yapıştır",
    "Paste key": "Yapıştırma tuşu",
    "Terminals usually want ctrl+shift+v. Change this if pasting does nothing.":
        "Terminaller genelde ctrl+shift+v ister. Yapıştırma çalışmıyorsa bunu değiştir.",
    "Restore the previous clipboard after pasting":
        "Yapıştırdıktan sonra eski pano içeriğini geri koy",
    "Indicator corner": "Gösterge köşesi",
    "bottom-left": "sol-alt",
    "bottom-right": "sağ-alt",
    "top-left": "sol-üst",
    "top-right": "sağ-üst",
    "Longest recording": "En uzun kayıt",
    " s": " sn",
    "Skip silent recordings (don't call the API)":
        "Sessiz kayıtları atla (API'ye gönderme)",
    "Silence threshold": "Sessizlik eşiği",
    "Keep audio files ({path})": "Ses kayıtlarını sakla ({path})",

    # --- settings: api --------------------------------------------------
    "Keys": "Anahtarlar",
    "Speech to text": "Sesi yazıya çevirme",
    "Transcript cleanup": "Transkripti temizleme",
    "API key": "API anahtarı",
    "Model": "Model",
    "Provider": "Sağlayıcı",
    "sk-… (falls back to OPENAI_API_KEY)": "sk-… (boşsa OPENAI_API_KEY kullanılır)",
    "sk-or-… (falls back to OPENROUTER_API_KEY)": "sk-or-… (boşsa OPENROUTER_API_KEY kullanılır)",
    "Test": "Test et",
    "Trying…": "Deneniyor…",
    "Runs on OpenRouter.": "OpenRouter üzerinde çalışır.",
    "Connection works. {count} audio models visible.":
        "Bağlantı tamam. {count} ses modeli görünüyor.",
    "Clean the transcript with a model": "Transkripti bir modelle temizle",
    "Thinking": "Düşünme",
    "Model's own default": "Modelin kendi varsayılanı",
    "Off": "Kapalı",
    "Minimal": "En az",
    "Low": "Düşük",
    "Medium": "Orta",
    "High": "Yüksek",
    "Very high": "Çok yüksek",
    "Maximum": "En yüksek",
    "How long a thinking model may reason before it answers. Cleanup is a light "
    "job, so more thinking mostly costs time and tokens. Models that cannot "
    "think ignore this.":
        "Düşünebilen bir modelin yanıtlamadan önce ne kadar düşüneceği. Temizleme "
        "hafif bir iş, fazla düşünmenin çoğunlukla getirisi süre ve token. "
        "Düşünemeyen modeller bunu yok sayar.",
    "Fetch model list": "Model listesini çek",
    "Fetching model list…": "Model listesi çekiliyor…",
    "Could not fetch the list: {error}": "Liste alınamadı: {error}",
    "{count} models loaded.": "{count} model yüklendi.",
    "Key works, no spending limit set.": "Anahtar çalışıyor, harcama sınırı yok.",
    "Key works. Used {usage} of {limit}.":
        "Anahtar çalışıyor. {limit} sınırının {usage} kadarı kullanılmış.",

    # --- settings: prompt ------------------------------------------------
    "System instruction given to the cleanup model. This is where you decide "
    "how much it may touch your words.":
        "Temizleme modeline verilen sistem talimatı. Ne kadar müdahale edeceğini "
        "burada belirlersin.",
    "Dictation": "Dikte",
    "Used instead when an audio or video file is cleaned up. It is written for "
    "subtitles: lines stay where they are, nothing is shortened, and misheard "
    "words are repaired from the context.":
        "Bir ses ya da video dosyası temizlenirken bunun yerine bu kullanılır. "
        "Altyazı için yazılmıştır: satırlar yerinde kalır, hiçbir şey kısaltılmaz, "
        "yanlış duyulan kelimeler bağlamdan düzeltilir.",
    "Reset to default": "Varsayılana döndür",
    "Names and terms you say often (optional). They go to the transcription "
    "model as a hint, and to the cleanup model as a glossary, so it can repair "
    "the ones that still come out wrong.":
        "Sık kullandığın isimler ve terimler (isteğe bağlı). Transkripsiyon "
        "modeline ipucu, temizleme modeline sözlük olarak gider; böylece yanlış "
        "çıkanları düzeltebilir.",

    # --- settings: audio file --------------------------------------------
    "Transcribe an existing audio or video file with the same models.":
        "Var olan bir ses ya da video dosyasını aynı modellerle yazıya çevir.",
    "Choose file…": "Dosya seç…",
    "No file selected": "Dosya seçilmedi",
    "Select an audio file": "Bir ses dosyası seç",
    "Audio and video files": "Ses ve video dosyaları",
    "All files": "Tüm dosyalar",
    "Add timestamps": "Zaman damgası ekle",
    "Prefixes every segment with [mm:ss]. Uses whisper-1 on whichever provider "
    "you picked, the only model that returns segment times.":
        "Her bölümün başına [dd:ss] koyar. Bölüm zamanı döndüren tek model olan "
        "whisper-1, seçtiğin sağlayıcı üzerinden kullanılır.",
    "Run the cleanup model afterwards": "Sonrasında temizleme modelinden geçir",
    "With its own rules, under Cleanup rules: written for subtitles, so the "
    "lines keep their place and nothing is shortened.":
        "Kendi kurallarıyla, Temizleme kuralları sekmesinin altında: altyazı için "
        "yazılmıştır, satırlar yerinde kalır ve hiçbir şey kısaltılmaz.",
    "Transcribe": "Yazıya çevir",
    "Stop": "Durdur",
    "Copy": "Panoya kopyala",
    "Save as .txt": "'.txt' olarak kaydet",
    "Save as .srt": "'.srt' olarak kaydet",
    "Subtitles, timed from the segments. Needs the timestamps option.":
        "Altyazı; zamanlaması bölüm damgalarından gelir. Zaman damgası seçeneği "
        "işaretliyken çalışır.",
    "No timestamped lines to turn into subtitles.":
        "Altyazıya çevrilecek zaman damgalı satır yok.",
    "Save transcript": "Transkripti kaydet",
    "Text files": "Metin dosyaları",
    "Subtitle files": "Altyazı dosyaları",
    "Converting audio…": "Ses dönüştürülüyor…",
    "Splitting into {count} chunks…": "{count} parçaya bölünüyor…",
    "Transcribing chunk {index}/{count}…": "{index}/{count} parça yazıya çevriliyor…",
    "Done: {chars} characters.": "Bitti: {chars} karakter.",
    "Stopped.": "Durduruldu.",
    "Failed: {error}": "Başarısız: {error}",
    "ffmpeg not found. Install it to transcribe files.":
        "ffmpeg bulunamadı. Dosya çevirmek için kur.",
    "Could not read the file: {error}": "Dosya okunamadı: {error}",
    "Saved: {path}": "Kaydedildi: {path}",

    # --- settings: shortcut ------------------------------------------------
    "Install as a KDE shortcut": "KDE kısayolu olarak kur",
    "Remove": "Kaldır",
    "Registered in KDE: {shortcut}": "KDE'de kayıtlı: {shortcut}",
    "No KDE shortcut installed.": "KDE kısayolu kurulu değil.",
    "Use the built-in listener (/dev/input), for when the KDE shortcut is not active yet":
        "Yerleşik dinleyici kullan (/dev/input), KDE kısayolu henüz etkin değilken",
    "Works immediately, no session restart. The only difference: the key "
    "combination also reaches the focused application.":
        "Anında çalışır, oturum yenilemek gerekmez. Tek farkı: tuş kombinasyonu "
        "odaktaki uygulamaya da iletilir.",
    "KWin only reads shortcut settings at startup. After 'Install' the shortcut "
    "shows up under System Settings → Shortcuts, but it will not fire until you "
    "log out and back in. Until then, use the built-in listener.":
        "KWin, kısayol ayarlarını yalnızca açılışta okur. 'Kur' dedikten sonra kısayol "
        "Sistem Ayarları → Kısayollar altında görünür ama oturumu yeniden açana kadar "
        "tetiklenmez. O zamana kadar yerleşik dinleyiciyi kullanabilirsin.",
    "Shortcut conflict": "Kısayol çakışması",
    "{shortcut} is also used by:\n\n{list}\n\nInstall anyway?":
        "{shortcut} şu girdilerde de kullanılıyor:\n\n{list}\n\nYine de kurulsun mu?",
    "Shortcut saved: {shortcut}\nKWin only reads this file at startup, so it "
    "will not fire until you log out and back in. To use it right away, turn on "
    "the built-in listener.":
        "Kısayol kaydedildi: {shortcut}\nKWin bu dosyayı yalnızca açılışta okuduğu için "
        "oturumu yeniden açana kadar tetiklenmez. Hemen kullanmak istersen "
        "yerleşik dinleyiciyi aç.",
    "Could not write the desktop file: {error}": "Desktop dosyası yazılamadı: {error}",
    "Could not write kglobalshortcutsrc: {error}": "kglobalshortcutsrc yazılamadı: {error}",
    "Could not parse the shortcut: {shortcut}": "Kısayol çözümlenemedi: {shortcut}",
    "Cannot read /dev/input. Your user needs to be in the 'input' group:\n"
    "  sudo usermod -aG input $USER   (then log out and back in)":
        "/dev/input okunamıyor. Kullanıcının 'input' grubunda olması gerekir:\n"
        "  sudo usermod -aG input $USER   (sonra oturumu yeniden aç)",

    # --- settings: shortcut, Windows ----------------------------------------
    "Not available on Windows": "Windows'ta kullanılamaz",
    "kwriteconfig6 not found, so the shortcut could not be registered with KDE.":
        "kwriteconfig6 bulunamadı, kısayol KDE'ye kaydedilemedi.",
    "{shortcut} is already taken by another program, so Dikte cannot use it. "
    "Pick a different combination.":
        "{shortcut} başka bir program tarafından kullanılıyor, Dikte bunu "
        "alamıyor. Başka bir kombinasyon seç.",
    "Use the built-in global hotkey listener":
        "Yerleşik global kısayol dinleyicisini kullan",
    "Windows reserves the combination for Dikte while it runs, so the focused "
    "application never sees it.":
        "Dikte çalışırken Windows kombinasyonu ona ayırır, odaktaki uygulama "
        "tuşu hiç görmez.",
    "{shortcut} is live as soon as you save.":
        "{shortcut} kaydeder kaydetmez çalışır.",
    "{shortcut} will do nothing until the global hotkey listener below is "
    "turned on.":
        "Aşağıdaki global kısayol dinleyicisi açılmadan {shortcut} hiçbir şey "
        "yapmaz.",
    "No shortcut set. The tray menu does the same job.":
        "Kısayol atanmamış. Tepsi menüsü de aynı işi görür.",
    "No shortcut set. The tray menu starts a meeting too.":
        "Kısayol atanmamış. Toplantıyı tepsi menüsünden de başlatabilirsin.",
    "No shortcut set. The tray menu asks it too.":
        "Kısayol atanmamış. Tepsi menüsünden de sorabilirsin.",

    # --- settings: history --------------------------------------------------
    "Copy selected to clipboard": "Seçiliyi panoya kopyala",
    "Delete selected": "Seçiliyi sil",
    "Clear history": "Geçmişi temizle",
    "Reload": "Yenile",
    "{ts}  ({duration} s)": "{ts}  ({duration} sn)",
    "Keep at most": "En fazla",
    " entries": " kayıt",
    "no limit": "sınırsız",
    "Once the history passes this many entries, the oldest one is dropped "
    "every time a new one arrives. Set it to 0 to keep everything.":
        "Geçmiş bu sayıyı aştıktan sonra, her yeni kayıt geldiğinde en eski kayıt "
        "silinir. Hepsini tutmak için 0 yaz.",
    "Delete the {count} selected entries?": "Seçili {count} kayıt silinsin mi?",
    "Delete the whole history? This cannot be undone.":
        "Geçmişin tamamı silinsin mi? Bu geri alınamaz.",

    # --- asking Claude Code -------------------------------------------------
    "Ask {name}": "{name} sor",
    "Stop and ask {name}": "Kaydı bitir ve {name} sor",
    "Start a new conversation": "Yeni konuşma başlat",
    "Start a new conversation now": "Şimdi yeni konuşma başlat",
    "Stop {name}": "{name} durdur",
    "Stopping…": "Durduruluyor…",
    "Stopped.": "Durduruldu.",
    "{name} starts fresh next time.": "{name} bir sonrakine sıfırdan başlayacak.",
    "Dikte: talking to Claude": "Dikte: ajanla konuşuyor",
    "Dikte: recording for Claude": "Dikte: ajan için kaydediyor",
    "Asking {name}…": "{name} soruluyor…",
    "{name}: {preview}": "{name}: {preview}",
    "{name} answered, but: {error}": "{name} cevapladı, ama: {error}",
    "Dikte: {name} could not do all of it": "Dikte: {name} her şeyi yapamadı",
    "Running a command…": "Komut çalıştırıyor…",
    "Reading…": "Okuyor…",
    "Looking through files…": "Dosyalara bakıyor…",
    "Searching the files…": "Dosyalarda arıyor…",
    "Editing a file…": "Dosya düzenliyor…",
    "Writing a file…": "Dosya yazıyor…",
    "Searching the web…": "İnternette arıyor…",
    "Reading a web page…": "Web sayfası okuyor…",
    "Handing it to a subagent…": "Alt ajana devrediyor…",
    "Planning…": "Planlıyor…",
    "Using {name}…": "{name} kullanıyor…",
    "Thinking…": "Düşünüyor…",
    "{binary} not found. Install it, or pick another provider under "
    "Settings → Agent.":
        "{binary} bulunamadı. Kur ya da Ayarlar → Ajan sekmesinden başka bir "
        "sağlayıcı seç.",
    "Could not run {binary}: {error}": "{binary} çalıştırılamadı: {error}",
    "{service} exited with code {code}.": "{service} {code} koduyla çıktı.",
    "It did not finish within {seconds} seconds.":
        "{seconds} saniye içinde bitmedi.",
    "Claude ended with an error.": "Claude bir hatayla sonlandı.",
    "Codex ended with an error.": "Codex bir hatayla sonlandı.",
    "{service} answered with nothing.": "{service} boş cevap verdi.",
    "It was not allowed to use: {tools}": "Şunları kullanmasına izin yoktu: {tools}",
    "The model returned an empty reply.": "Model boş cevap döndürdü.",

    # --- settings: the agent ------------------------------------------------
    "Agent": "Ajan",
    "This shortcut records the same way dictation does, but the transcript is "
    "not what gets pasted. It goes to an agent as a command, and what comes "
    "back is pasted instead: the answer to a question, or a sentence saying "
    "what was done. Claude Code and Codex run as the session you would have "
    "opened yourself, with your skills, your connected services and your "
    "account.":
        "Bu kısayol dikte ile aynı şekilde kaydeder, ama yapıştırılan şey "
        "transkript değildir. Transkript bir ajana komut olarak gider ve yerine "
        "oradan döneni yapıştırılır: bir sorunun cevabı ya da ne yapıldığını "
        "söyleyen bir cümle. Claude Code ve Codex, kendi açacağın oturumun "
        "aynısı olarak çalışır: skill'lerinle, bağlı servislerinle ve kendi "
        "hesabınla.",
    "How it runs": "Nasıl çalışıyor",
    "Runs on": "Şunun üstünde çalışır",
    "More thinking is slower, and you are standing in front of the screen while "
    "it happens. Worth it for a job that has to be worked out rather than "
    "looked up.":
        "Daha çok düşünmek daha yavaştır ve bu sırada ekranın başında bekliyor "
        "olursun. Bakılıp bulunacak değil, çözülmesi gereken işler için değer.",
    "Claude Code": "Claude Code",
    "Codex": "Codex",
    "Codex's own default": "Codex'in kendi varsayılanı",
    "Sandbox": "Kum havuzu",
    "Read anything, write in the working directory":
        "Her şeyi okusun, çalışma dizinine yazsın",
    "Read only": "Yalnızca okusun",
    "No sandbox at all": "Kum havuzu hiç olmasın",
    "A plain question and a plain answer, over the OpenRouter key you already "
    "have. It runs no commands, opens no files and reaches none of your "
    "services, so it can tell you what the capital of Peru is but not what is "
    "in your calendar. Working directory and permissions above mean nothing "
    "here.":
        "Elindeki OpenRouter anahtarı üzerinden düz bir soru ve düz bir cevap. "
        "Komut çalıştırmaz, dosya açmaz, servislerinin hiçbirine erişmez; yani "
        "Peru'nun başkentini söyler ama takviminde ne olduğunu söyleyemez. "
        "Yukarıdaki çalışma dizini ve izinler burada bir şey ifade etmez.",
    "Needs no program installed, only the OpenRouter key.":
        "Kurulu bir programa değil, yalnızca OpenRouter anahtarına ihtiyaç duyar.",
    "{binary} is not on your PATH, so this cannot run yet. Install it, or pick "
    "another one above.":
        "{binary} PATH'te değil, dolayısıyla bu henüz çalışamaz. Kur ya da "
        "yukarıdan başka birini seç.",
    "The conversation": "Konuşma",
    "The answer": "Cevap",
    "Found: {path}": "Bulundu: {path}",
    "No KDE shortcut installed. The tray menu asks it too.":
        "Kurulu KDE kısayolu yok. Tepsi menüsünden de sorulabilir.",
    "A name like “sonnet” always means the newest model of that line. Opus "
    "thinks harder and answers slower, which is felt here more than anywhere "
    "else: you are standing in front of the screen.":
        "“sonnet” gibi bir ad her zaman o serinin en yenisini seçer. Opus daha "
        "çok düşünür ve daha geç cevaplar; bu da en çok burada hissedilir, "
        "çünkü ekranın başında bekliyorsun.",
    "Permissions": "İzinler",
    "Decide on its own, with the safety checks on":
        "Kendi karar versin, güvenlik denetimleri açık",
    "Allow everything": "Her şeye izin ver",
    "Only what needs no permission": "Yalnızca izin gerektirmeyenler",
    "Working directory": "Çalışma dizini",
    "Choose…": "Seç…",
    "The directory the command runs in, which decides which project's "
    "instructions and files it can see. Your own skills and services are there "
    "whichever one it is.":
        "Komutun içinde çalıştığı dizin; hangi projenin talimatlarını ve "
        "dosyalarını göreceğini bu belirler. Kendi skill'lerin ve servislerin "
        "hangi dizin olursa olsun oradadır.",
    "Give up after": "Şu süreden sonra vazgeç",
    "A command still running after this is given up on. The tray menu can stop "
    "one earlier.":
        "Bu süreden sonra hâlâ süren komuttan vazgeçilir. Tepsi menüsünden daha "
        "erken de durdurulabilir.",
    "Carry on for": "Şu kadar süre sürsün",
    "every command on its own": "her komut ayrı",
    "Commands within this long of each other are one conversation, so “and move "
    "that to Thursday” knows what “that” is. After it, the next command starts "
    "fresh.":
        "Birbirinden bu kadar süre içinde gelen komutlar tek bir konuşmadır; "
        "böylece “onu perşembeye al” dediğinde “o”nun ne olduğu bilinir. Bu "
        "sürenin ardından bir sonraki komut sıfırdan başlar.",
    "No conversation going.": "Süren bir konuşma yok.",
    "Last used {minutes} min ago.": "En son {minutes} dk önce kullanıldı.",
    "Paste it into the focused window": "Odaktaki pencereye yapıştır",
    "It is copied to the clipboard either way.": "Panoya her hâlükârda kopyalanır.",
    "Clean the transcript up before sending it": "Göndermeden önce transkripti temizle",
    "Off by default: Claude reads through “erm” and “you know” without help, "
    "and cleanup costs an API call and a second or two.":
        "Varsayılan olarak kapalı: Claude “eee” ve “hani”yi yardımsız da okur, "
        "temizlik ise bir API çağrısına ve bir iki saniyeye mal olur.",
    "Told to the agent alongside every command, on top of whatever your own "
    "configuration already says.":
        "Her komutla birlikte ajana söylenir, kendi yapılandırmanın zaten "
        "söylediklerinin üstüne eklenir.",
    "  ·  asked Claude: {question}": "  ·  Claude'a soruldu: {question}",

    # --- meetings: tray and pipeline ---------------------------------------
    "Record a meeting": "Toplantı kaydet",
    "End the meeting and write it up": "Toplantıyı bitir ve tutanağı çıkar",
    "Writing the meeting up…": "Tutanak çıkarılıyor…",
    "Discard the meeting": "Toplantıyı iptal et",
    "Ending the meeting…": "Toplantı bitiriliyor…",
    "Dikte: in a meeting": "Dikte: toplantıda",
    "Dikte: in a meeting ({time})": "Dikte: toplantıda ({time})",
    "Dikte: writing the meeting up": "Dikte: tutanak çıkarıyor",
    "Meeting recorded, writing it up…": "Toplantı kaydedildi, tutanak çıkarılıyor…",
    "Meeting written up: {title}": "Tutanak hazır: {title}",
    "Dikte: the meeting is written up": "Dikte: tutanak hazır",
    "Meeting failed: {error}": "Toplantı başarısız: {error}",
    "Dikte: the meeting could not be written up": "Dikte: tutanak çıkarılamadı",
    "{error}\n\nThe recording has been kept. Settings → Minutes can try again.":
        "{error}\n\nSes kaydı duruyor. Ayarlar → Tutanaklar üzerinden yeniden "
        "denenebilir.",
    "Recording saved. The previous meeting is still being written up, so start "
    "this one from Settings → Minutes when it is done.":
        "Kayıt saklandı. Önceki toplantının tutanağı hâlâ çıkarılıyor; bu kaydı o "
        "bitince Ayarlar → Tutanaklar üzerinden başlat.",
    "The recording stopped on its own; the sound device may have gone away. "
    "Keeping what was captured.":
        "Kayıt kendiliğinden durdu, ses aygıtı çekilmiş olabilir. O ana kadar "
        "kaydedilen saklanıyor.",
    "ffmpeg not found. Install it to record a meeting.":
        "ffmpeg bulunamadı. Toplantı kaydı için kur.",
    "Could not work out which speaker output to record. Pick one in "
    "Settings → Meeting.":
        "Hangi ses çıkışının kaydedileceği anlaşılamadı. Ayarlar → Toplantı "
        "sekmesinden seç.",
    "Nothing was recorded: {error}": "Hiçbir şey kaydedilmedi: {error}",
    "Transcribing {side}: {index}/{count}…":
        "{side} yazıya çevriliyor: {index}/{count}…",
    "you": "sen",
    "the others": "karşı taraf",
    "Cleaning up {index}/{count}…": "Temizleniyor {index}/{count}…",
    "Writing the minutes…": "Tutanak yazılıyor…",
    "Neither side of the recording had any speech in it.":
        "Kaydın iki tarafında da konuşma yok.",
    "This recording is not a two-channel meeting.":
        "Bu kayıt iki kanallı bir toplantı kaydı değil.",
    "The recording is gone: {path}": "Ses kaydı yerinde yok: {path}",
    "Meeting": "Toplantı",
    "Transcript": "Transkript",
    "{minutes} min": "{minutes} dk",
    "{hours} h {minutes} min": "{hours} sa {minutes} dk",

    # --- settings: meeting --------------------------------------------------
    "Minutes": "Tutanaklar",
    "A meeting is recorded from two devices at once: your microphone and "
    "whatever comes out of your speakers. Nothing has to guess who was "
    "speaking, because the two never share a channel.":
        "Toplantı iki aygıttan aynı anda kaydedilir: mikrofonun ve hoparlöründen "
        "çıkan ses. Kimin konuştuğunun tahmin edilmesi gerekmez, çünkü ikisi hiç "
        "aynı kanala girmez.",
    "Sound": "Ses",
    "Same as dictation": "Diktedekiyle aynı",
    "Current output": "Geçerli çıkış",
    "The other participants": "Karşı tarafın sesi",
    "Wear headphones if you can. Through speakers your microphone hears the "
    "other side as well, and although a line that lands on both channels at "
    "once is dropped again, the repair is never as clean as not needing it.":
        "Yapabiliyorsan kulaklık tak. Hoparlörde mikrofonun karşı tarafı da "
        "duyar; aynı anda iki kanala birden düşen satır ayıklanıyor ama bu "
        "onarım, hiç gerekmemesi kadar temiz olmuyor.",
    "Who is talking": "Kimler konuşuyor",
    "Me": "Ben",
    "Other side": "Karşı taraf",
    "You": "Sen",
    "The other end": "Karşı taraf",
    "Expected": "Beklenen kişiler",
    "One name per line": "Her satıra bir isim",
    "Everyone on the far end shares one label: they reach you as a single mixed "
    "signal. The names go to the transcription model so they come out spelled "
    "right, and to the minutes, which may use one for a line only when the "
    "conversation itself makes clear who was speaking.":
        "Karşı taraftaki herkes tek bir etiketi paylaşır; sana tek bir karışım "
        "olarak gelirler. İsimler, doğru yazılsınlar diye transkripsiyon modeline "
        "ve tutanağa gider; tutanak bir satıra ancak konuşmanın kendisi kimin "
        "konuştuğunu açık ediyorsa isim yazar.",
    "Unlike cleanup, this one is worth some thinking: it has to hold a whole "
    "meeting in its head and work out what was actually decided.":
        "Temizlemenin aksine burada düşünmenin karşılığı var: model bütün "
        "toplantıyı aklında tutup neyin gerçekten karara bağlandığını çıkarmak "
        "zorunda.",
    "Clean the transcript up first": "Önce transkripti temizle",
    "Runs the cleanup model over the transcript before the minutes are written, "
    "keeping the timestamps and the speaker labels.":
        "Tutanak yazılmadan önce transkripti temizleme modelinden geçirir; zaman "
        "damgaları ve konuşmacı etiketleri korunur.",
    "Recording": "Kayıt",
    " min": " dk",
    "Longest meeting": "En uzun toplantı",
    "Keep the recording after the minutes are written":
        "Tutanak çıktıktan sonra ses kaydını sakla",
    "A run that fails keeps its recording either way, so it can be tried again "
    "from the Minutes tab. This is about the ones that worked.":
        "Başarısız olan bir işlemin kaydı zaten saklanır, Tutanaklar sekmesinden "
        "yeniden denenebilsin diye. Buradaki ayar başarıyla bitenler için.",
    "none": "yok",
    "Type a key combination first.": "Önce bir tuş kombinasyonu yaz.",
    "No KDE shortcut installed. The tray menu starts a meeting too.":
        "KDE kısayolu kurulu değil. Toplantıyı tepsi menüsünden de başlatabilirsin.",
    "System instruction given to the minutes model.":
        "Tutanak modeline verilen sistem talimatı.",
    "Pick a meeting to read it.": "Okumak için bir toplantı seç.",
    "Write it up": "Tutanağı çıkar",
    "Open the folder": "Klasörü aç",
    "waiting to be written up": "tutanak bekliyor",
    "transcript ready, minutes missing": "transkript hazır, tutanak eksik",
    "failed": "başarısız",
    "Nothing has been written yet.": "Henüz bir şey yazılmadı.",
    "Done: {title}": "Bitti: {title}",
    "This one is being written up right now.": "Bunun tutanağı şu anda çıkarılıyor.",
    "Delete this meeting, its minutes and its recording?":
        "Bu toplantı, tutanağı ve ses kaydı silinsin mi?",
}
