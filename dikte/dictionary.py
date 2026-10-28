"""The personal dictionary: names and terms, one record each.

It used to be one free text box, the `transcribe_prompt` setting, handed as it
was to the transcription model as a hint and to the cleanup model as a
glossary. It is still both of those. What changed is that the text is now
rendered from records, so that one entry can be found, corrected or removed,
and so that an entry Dikte learned on its own can be told apart from one the
user typed.

The records live in dictionary.json next to config.json rather than inside it.
Learning adds to them from the dictation thread, and config.json is written
whole by the settings window and by the command line from another process; a
file of its own, behind a lock of its own, is what keeps a learned entry from
being written over by a save that never knew about it. The lock is a lock file
as well as a thread lock, since `dikte history delete` rewrites the file from
the command line's process while the application may be learning into it.

`transcribe_prompt` is kept, holding the rendered text. An older version, or
`dikte config set transcribe_prompt`, keeps working on it, and the file
remembers which text it last agreed with (`synced`). When the setting no longer
matches, somebody edited it outside the dictionary, and the setting wins: its
pieces become the records, keeping whatever an unchanged piece already knew
about itself.

Until the file exists nothing is rendered at all: the hint and the glossary are
the stored text exactly as before, byte for byte. A user who never opens the
dictionary sees no change. When the list is edited, the migration stays
lossless: every piece is its own record, duplicates included, and each record
remembers how many line breaks stood before it, so the text renders back with
the lines it was typed in.
"""

import contextlib
import copy
import difflib
import json
import os
import pathlib
import re
import tempfile
import threading
import unicodedata

from PyQt6.QtCore import QLockFile

from . import config as cfg

MANUAL, AUTO = "manual", "auto"

# How many separate dictations a term has to survive before it is learned. One
# sighting is a coincidence, and a misheard word repeats less often than a real
# name does.
LEARN_AFTER = 3
# Candidates still being counted. Past this the ones not seen for longest are
# forgotten, whatever their count, so the file cannot grow without bound and a
# word seen for the first time is always counted.
PENDING_LIMIT = 200
# Learned words the user deleted, which are not learned again.
REJECTED_LIMIT = 500
# How much room the learned entries may take in the transcription hint, in
# characters. Whisper reads at most 224 tokens of prompt and keeps the end of
# a longer one; 600 characters stays inside that even for text that tokenises
# badly. Typed entries are never trimmed: they go in whole as they always did,
# so a typed list longer than about 600 characters can still pass the limit,
# and then the provider keeps its end. Learned entries only fill what room is
# left. The cleanup glossary has no such limit and carries everything.
HINT_BUDGET = 600

_LOCK = threading.Lock()
# How long to wait for another process holding the file, in milliseconds. A
# write takes a few; past this the holder is stuck, and the caller is told.
FILE_LOCK_MS = 5000


def path():
    """Read from cfg each time, so a test that moves the config moves this too."""
    return cfg.CONFIG_DIR / "dictionary.json"


@contextlib.contextmanager
def _locked():
    """Held around every read that is followed by a write: this process's
    lock, then the file's, for the other processes that write it."""
    with _LOCK:
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(path().with_suffix(".json.lock")))
        # Stale only when its holder is gone, never by age alone, as with the
        # application's own lock (ipc.instance_lock).
        lock.setStaleLockTime(0)
        if not lock.tryLock(FILE_LOCK_MS):
            raise OSError(f"{path()} is held by another process")
        try:
            yield
        finally:
            lock.unlock()


def pieces(text):
    """The pieces of a free text dictionary, one per line or comma, each with
    the number of line breaks that stood before it."""
    out, breaks = [], 0
    for line in (text or "").splitlines():
        for piece in line.split(","):
            piece = piece.strip()
            if piece:
                out.append((piece, breaks if out else 0))
                breaks = 0
        breaks += 1
    return out


def split(text):
    return [piece for piece, _ in pieces(text)]


def entry(term, spelling="", source=MANUAL, breaks=0):
    """A clean record, or None when there is no term to keep."""
    term = " ".join(str(term or "").replace(",", " ").split())
    spelling = " ".join(str(spelling or "").replace(",", " ").split())
    if not term:
        return None
    if spelling == term:
        spelling = ""
    if isinstance(breaks, bool) or not isinstance(breaks, int) or breaks < 0:
        breaks = 0
    return {"term": term, "spelling": spelling,
            "source": AUTO if source == AUTO else MANUAL, "breaks": min(breaks, 9)}


def hint_of(record):
    """What the transcription model is told: the spelling to aim for."""
    return record["spelling"] or record["term"]


def gloss_of(record):
    """What the cleanup model is told. An arrow, the way the cleanup prompt's
    own examples show a word before and after."""
    if record["spelling"]:
        return f"{record['term']} -> {record['spelling']}"
    return record["term"]


def _render(records, show):
    out = []
    for record in records:
        if out:
            out.append("\n" * record.get("breaks", 0) or ", ")
        out.append(show(record))
    return "".join(out)


def render_hint(records):
    """Every record, laid out the way the text was typed: what the setting
    holds."""
    return _render(records, hint_of)


def render_glossary(records):
    return _render(records, gloss_of)


def budget_hint(records, budget=HINT_BUDGET, reserve=0):
    """The transcription hint: every typed record, as typed, then as many of
    the learned ones as fit in what is left of the budget, cut at a record
    boundary. Learning only ever fills spare room; what the user wrote goes
    in whole, as it always did. `reserve` is room kept for what a caller adds
    after it, the names of a meeting's participants."""
    kept = [r for r in records if r["source"] != AUTO]
    for record in (dict(r, breaks=0) for r in records if r["source"] == AUTO):
        if len(_render(kept + [record], hint_of)) + reserve > budget:
            break
        kept.append(record)
    return _render(kept, hint_of)


def fold(text):
    """Without regard to case, and with every Turkish i the same letter: the
    dotted capital İ (which would otherwise fold to an i and a combining dot)
    and the dotless ı both fold to a plain i, as I does. "Işık" meets "IŞIK"
    and "ılık" meets "Ilık", which Turkish speakers typing in a hurry and
    transcribers alike mix up. How the dictionary tells two terms are the
    same one, and what site matching compares."""
    return text.casefold().replace("i\u0307", "i").replace("\u0131", "i")


_key = fold


def merge(edited, extra):
    """`edited` as it is, then each of `extra` whose term it does not have."""
    seen = {_key(r["term"]) for r in edited}
    out = list(edited)
    for record in extra:
        if _key(record["term"]) not in seen:
            seen.add(_key(record["term"]))
            out.append(record)
    return out


def _clean_records(raw):
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict):
            record = entry(item.get("term"), item.get("spelling"),
                           item.get("source"), item.get("breaks", 0))
            if record is not None:
                out.append(record)
    return out


def _clean_pending(raw):
    """{term: [count, last seen]}. A bare count is an older file's."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for term, value in raw.items():
        if isinstance(value, int) and not isinstance(value, bool):
            value = [value, 0]
        if (isinstance(term, str) and isinstance(value, list) and len(value) == 2
                and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
                and value[0] > 0):
            # Kept folded, and folded again on every read: a file written
            # before fold() changed still counts the same words as one.
            key = PAIR.join(_key(side) for side in term.split(PAIR))
            if key in out:
                out[key] = [max(out[key][0], value[0]), max(out[key][1], value[1])]
            else:
                out[key] = list(value)
    return out


# The last file read, by its modification time and size: each dictation asks
# for the hint and the glossary, and a learning run reads it again, so this
# saves reading and parsing the same few kilobytes three times over.
_cache = {"entry": (None, None)}


def _read_file():
    """The stored state, or None when there is no file to read.

    A file that cannot be parsed counts as no file: the setting still holds
    the rendered text, which is the dictionary minus what was learned since
    it was last saved. A copy every time, so a caller may change it."""
    try:
        status = os.stat(path())
    except OSError:
        return None
    stamp = (str(path()), status.st_ino, status.st_mtime_ns, status.st_size)
    # The stamp and the data travel as one tuple, swapped in one assignment,
    # so a reader on another thread never pairs a new stamp with old data.
    cached_stamp, cached = _cache["entry"]
    if cached_stamp != stamp:
        try:
            with open(path(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            synced = data.get("synced")
            rejected = data.get("rejected")
            clock = data.get("clock")
            data = {
                "entries": _clean_records(data.get("entries")),
                "pending": _clean_pending(data.get("pending")),
                "synced": synced if isinstance(synced, str) else "",
                "rejected": (list(dict.fromkeys(
                    _key(x) for x in rejected if isinstance(x, str)))[-REJECTED_LIMIT:]
                    if isinstance(rejected, list) else []),
                "clock": clock if isinstance(clock, int) and not isinstance(clock, bool) else 0,
            }
        else:
            data = None
        _cache["entry"] = (stamp, data)
        cached = data
    return copy.deepcopy(cached)


def _reconcile(records, text, synced):
    """The records rebuilt from a setting edited outside the dictionary.

    Each piece takes the first record not yet used whose hint it is, so a
    spelling or an Auto mark survives an edit that did not touch it, and two
    records sharing a spelling stay two. A learned record the setting never
    held (learning does not write config.json) is kept too: an edit to a text
    it was never in cannot have meant to remove it."""
    available = list(records)
    rebuilt = []
    for piece, breaks in pieces(text):
        for i, record in enumerate(available):
            if hint_of(record) == piece:
                rebuilt.append(dict(available.pop(i), breaks=breaks))
                break
        else:
            rebuilt.append(entry(piece, breaks=breaks))
    before = set(split(synced))
    rebuilt += [r for r in available
                if r["source"] == AUTO and hint_of(r) not in before]
    return rebuilt


def _text(conf):
    value = conf["transcribe_prompt"]
    return value if isinstance(value, str) else ""


def _state(conf, stored=None):
    """(records, stored, exists), agreeing with the setting as it is now."""
    stored = _read_file() if stored is None else stored
    text = _text(conf)
    if stored is None:
        return [entry(p, breaks=b) for p, b in pieces(text)], None, False
    if text.strip() != stored["synced"].strip():
        return _reconcile(stored["entries"], text, stored["synced"]), stored, True
    return stored["entries"], stored, True


def records(conf):
    """Every record, in the order they are listed."""
    return list(_state(conf)[0])


def hint(conf, reserve=0):
    """The transcription hint. The stored text itself until there is a file."""
    found, _, exists = _state(conf)
    if not exists:
        return _text(conf)
    return budget_hint(found, reserve=reserve)


def glossary(conf):
    """The cleanup glossary, stripped the way the setting always was."""
    found, _, exists = _state(conf)
    if not exists:
        return _text(conf).strip()
    return render_glossary(found)


def _read_for_write():
    """_read_file() for a caller holding _locked() that is about to write. A
    file that is there but cannot be parsed is set aside first, the way
    Config.load sets aside a config.json it cannot read, so that the write
    does not quietly replace whatever was left in it."""
    stored = _read_file()
    if stored is not None:
        return stored
    try:
        with open(path(), encoding="utf-8") as fh:
            unreadable = not isinstance(json.load(fh), dict)
    except OSError:
        return None  # not there, or not ours to read: nothing to set aside
    except ValueError:
        unreadable = True
    if unreadable:
        broken = path().with_suffix(".json.broken")
        try:
            path().replace(broken)
        except OSError:
            pass
        else:
            print(f"dikte: could not read the dictionary; the unreadable file "
                  f"was kept as {broken}")
    return None


def _write(entries, pending, synced, rejected=(), clock=0):
    # Forgotten first: two writes inside one tick of the file system clock can
    # leave the same time and size behind.
    _cache["entry"] = (None, None)
    cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    target = path()
    # A name of its own, made readable to the user alone from the start rather
    # than chmodded after the words are already in it.
    handle, tmp = tempfile.mkstemp(prefix="dictionary.", suffix=".json.tmp",
                                   dir=cfg.CONFIG_DIR)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "entries": entries, "pending": pending,
                       "synced": synced, "rejected": list(rejected)[-REJECTED_LIMIT:],
                       "clock": clock},
                      fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        cfg._replace_with_retry(pathlib.Path(tmp), target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _empty():
    return {"entries": [], "pending": {}, "synced": "", "rejected": [], "clock": 0}


def _arrived(current, loaded, edited):
    """Records on disk that the window never loaded: learned while it was open.

    Not one whose word or spelling `edited` already has as a word or a
    spelling: a correction learned as "kube cuttle -> kubectl" while the user
    typed "kubectl" in would otherwise put kubectl in the hint twice."""
    seen = {_key(r["term"]) for r in loaded}
    have = {_key(r["term"]) for r in edited} | {_key(hint_of(r)) for r in edited}
    return [r for r in current if _key(r["term"]) not in seen
            and _key(r["term"]) not in have and _key(hint_of(r)) not in have]


def save(conf, edited, loaded):
    """Store what the settings window holds, and the setting with it.

    `loaded` is what the window was given when it read the dictionary. A record
    on disk now that was not there then was learned while the window was open,
    and is kept: the user never saw it, so leaving it out is not a deletion.

    The setting is written into `conf` here and saved by the caller with the
    rest of the window, before this file is: should that save fail, the file
    still agrees with the old setting, and nothing is lost either way. Returns
    what write() needs."""
    with _LOCK:
        current, _, _ = _state(conf)
        merged = merge(list(edited), _arrived(current, loaded, edited))
        text = render_hint(merged)
        conf["transcribe_prompt"] = text
        return merged, list(loaded), text


def write(pending, forget=False):
    """The second half of save(), once config.json is on disk.

    The file is read again under the lock, since learning may have added to it
    while config.json was being written, and what it added is kept the same
    way save() kept what arrived before. `forget` empties the names still
    being counted."""
    merged, loaded, text = pending
    with _locked():
        stored = _read_for_write() or _empty()
        merged = merge(merged, _arrived(stored["entries"], loaded, merged))
        # A learned record the user took out is remembered as refused, so the
        # same word is not learned straight back three dictations later.
        kept = {_key(r["term"]) for r in merged}
        in_use = kept | {_key(hint_of(r)) for r in merged}
        rejected = list(stored["rejected"])
        for record in loaded:
            key = _key(record["term"])
            if record["source"] != AUTO or key in kept:
                continue
            # A learned correction refuses its spelling as well, so the same
            # name heard another wrong way does not bring it straight back;
            # not a spelling an entry still kept (a typed one, say) uses.
            for refused in (key, _key(record["spelling"])):
                if refused and refused not in rejected and refused not in in_use:
                    rejected.append(refused)
        _write(merged, {} if forget else stored["pending"], text, rejected,
               stored["clock"])
        return merged


def store(conf, edited, loaded, forget=False):
    """save() and write() for a caller with nothing else to save in between."""
    pending = save(conf, edited, loaded)
    conf.save()
    return write(pending, forget)


def forget_pending(texts=None):
    """Drop names still being counted. They are words out of dictations, kept
    only for learning, and go when learning or the history does: all of them,
    or with `texts`, the ones that occur in those texts (the rows deleted)."""
    with _locked():
        stored = _read_for_write()
        if stored is None or not stored["pending"]:
            return
        if texts is None:
            pending = {}
        else:
            rows = [_joined(_tokens(t)) for t in texts if isinstance(t, str)]
            pending = {}
            for term, value in stored["pending"].items():
                # A correction waits as "heard -> spelling": it goes when a
                # deleted row held either side of it.
                sides = [_tokens(side) for side in term.split(PAIR)]
                if not any(words and any(_joined(words) in row for row in rows)
                           for words in sides):
                    pending[term] = value
            if len(pending) == len(stored["pending"]):
                return
        _write(stored["entries"], pending, stored["synced"], stored["rejected"],
               stored["clock"])


def clear_auto(entries):
    return [r for r in entries if r["source"] != AUTO]


# --- learning ---------------------------------------------------------------

# Words that are written with a capital in the middle of a sentence without
# being anybody's name: the pronoun, the days, the months, and the languages
# and nationalities, which English capitalises and a Turkish speaker mixing
# languages often does too. Everything else a title-case word mid-sentence
# usually is, in English and in Turkish, is a name.
COMMON = set("""
i i'm i've i'll i'd
monday tuesday wednesday thursday friday saturday sunday
january february march april may june july august september october
november december
pazartesi salı çarşamba perşembe cuma cumartesi pazar
ocak şubat mart nisan mayıs haziran temmuz ağustos eylül ekim kasım aralık
english british american turkish german french spanish italian portuguese
dutch polish russian ukrainian arabic persian kurdish greek chinese japanese
korean hindi european african asian
ingilizce türkçe almanca fransızca ispanyolca italyanca portekizce felemenkçe
lehçe rusça ukraynaca arapça farsça kürtçe yunanca çince japonca korece
ingiliz amerikan amerikalı türk alman fransız ispanyol italyan rus arap
kürt yunan japon koreli çinli avrupalı
""".split())
COMMON = {_key(w) for w in COMMON}

# German writes every noun with a capital, so the signal means nothing there.
_NOUN_CAPITALS = {"de", "lb"}

_WORD = re.compile(r"[^\W\d_][\w]*(?:[-.][\w]+)*", re.UNICODE)
# What can stand before the first word of a sentence, a line or a list item.
_SENTENCE_END = set(".!?:;\n\"“”‘«»(-*•")

# Words German uses in nearly every sentence and English and Turkish do not:
# how a dictation is told to be German when the language was left on auto and
# the transcriber did not say what it heard, which a hosted one never does.
_GERMAN = frozenset("""
und der das ist nicht ich mit auf für wir ein eine einen einem dem den des
sich auch wird werden sind noch nur oder aber wenn dass schon wie bei nach
""".split())


def capitalises_nouns(language, text):
    """Whether the dictation was in a language that capitalises every noun:
    by its code when there is one, and otherwise by its words, German when
    two of them and one word in ten are German's everyday ones."""
    code = (language or "").lower()
    if code not in ("", "auto"):
        return code[:2] in _NOUN_CAPITALS
    words = [w.casefold() for w in _WORD.findall(text)]
    hits = sum(w in _GERMAN or "ß" in w for w in words)
    return hits >= 2 and hits * 10 >= len(words)


def _titled(word):
    """Title case or camel case, with at least one lower case letter: a name.
    An acronym in capitals is left alone; those are mostly common words."""
    return (len(word) >= 3 and word[0].isupper()
            and any(c.islower() for c in word[1:]))


def runs(text):
    """Runs of title-case words, each with whether it began a sentence.

    A run that begins a sentence is kept whole rather than losing its first
    word: "Claude Code is great" is the run "Claude Code" at a start, not the
    name "Code"."""
    found, run = [], []
    run_starts = False
    previous_end = 0

    def close():
        if run:
            found.append((" ".join(run), run_starts))
            if run_starts and len(run) > 1:
                # The first word may only be capitalised for starting the
                # sentence ("Bunu Yusuf'un"), so the rest is offered too, as
                # a start as well: it counts only once seen mid-sentence.
                found.append((" ".join(run[1:]), True))
            run.clear()

    for match in _WORD.finditer(text):
        word = match.group(0)
        # Spaces and tabs only: a line break is itself a start. Read from the
        # gap since the last word, so the text is walked once.
        gap = text[previous_end:match.start()]
        trimmed = gap.rstrip(" \t")
        starts = (trimmed[-1] in _SENTENCE_END) if trimmed else previous_end == 0
        previous_end = match.end()
        # A Turkish suffix hangs off a name after an apostrophe (Yusuf'un), and
        # the word pattern stops at the apostrophe, so the name arrives bare and
        # the suffix as a lower case word of its own that ends the run.
        if _titled(word) and _key(word) not in COMMON:
            # Only words side by side on one line, in one sentence, are one
            # name: "Ahmet\nMehmet" is two.
            if run and (gap.strip() or "\n" in gap or starts):
                close()
            if not run:
                run_starts = starts
            run.append(word)
        else:
            close()
    close()
    return found


def _tokens(text):
    """A text as the words learning reads, folded: what a term is matched on,
    whatever the spacing between its words."""
    return [_key(w) for w in _WORD.findall(text)]


# Never part of a word, so words joined and wrapped with it can be searched
# for a whole run of words with one substring test instead of a Python loop
# per position: deleting a few hundred rows must not freeze the window.
_SEP = "\x00"


def _joined(words):
    return _SEP + _SEP.join(words) + _SEP


def candidates(text):
    """Names and terms in a text: runs of title-case words that do not start a
    sentence. "We moved it to Claude Code yesterday" gives "Claude Code"."""
    return [term for term, starts in runs(text) if not starts]


# --- corrections --------------------------------------------------------------
#
# The second thing learning reads: a misheard word that the cleanup keeps
# putting right. "Klod Kod" in the transcript and "Claude Code" in the text,
# three dictations running, is a spelling worth keeping, with what it was
# heard as: the hint then asks for the spelling and the glossary maps the one
# to the other. Only a swap of a few words for a name or a term counts; the
# grammar, the punctuation and the filler the cleanup also changes do not.

# Everyday words, a few hundred of the most frequent in English and in
# Turkish. A correction whose heard side is nothing but these is the cleanup
# putting a name where an ordinary word was said: "cloud" made "Claude" once
# is right, but learned, every "cloud" would be pushed towards "Claude".
EVERYDAY = frozenset(fold(w) for w in """
a about above after again against all almost alone along already also always
am among an and another any anything are around as ask at away back bad be
because been before being below best better between big black blue body book
both box boy bring but buy by call came can car card care case cat change
child city clean clear close cloud cold come could country course cut dark
day dead dear did different do does dog done door down draw dream drive drop
dry during each early earth easy eat end enough even evening ever every eye
face fact fall family far fast father feel few field fill find fine fire
first fish five floor fly follow food for form found four free friend from
front full game gave get girl give glass go gold gone good got great green
ground group grow had half hand hard has have he head hear heart help her
here high him his hold home hope horse hot hour house how i idea if in into
is it its just keep key kind king know land large last late later learn
leave left less let letter life light like line list little live long look
lot love low made make man many map may me mean men might mind miss money
month more morning most mother move much music must my name near need never
new news next nice night no not note nothing now number of off often old on
once one only open or order other our out over own page paper park part pass
past pay people pick piece place plan play point poor power put question
quick rain read ready real red rest right river road rock room run said same
saw say school sea second see seem send set she ship short should show side
sign simple since sing sit six size sleep slow small snow so some something
song soon sound speak stand star start stay still stop story street strong
such sun sure table take talk tell ten than that the their them then there
these they thing think this those though three through time to today
together told too took top town tree true try turn two under until up us
use very voice wait walk wall want war warm was watch water way we week well
went were what when where which while white who why will wind window with
word work world would write year yes yet you young your

ve ile ama fakat çünkü veya ya da de da ki mi mı mu mü bu şu o bir iki üç
dört beş altı yedi sekiz dokuz on yüz bin ben sen biz siz onlar bana sana ona
bize size benim senin onun bizim sizin var yok evet hayır çok az daha en her
hiç bazı tüm bütün hep şimdi sonra önce bugün yarın dün akşam sabah gece
gün hafta ay yıl saat zaman yer ev iş okul yol su ekmek çay kahve yemek
araba kapı pencere masa sandalye kitap kalem kağıt telefon bilgisayar para
anne baba kardeş çocuk adam kadın arkadaş insan dünya şehir köy ülke deniz
dağ ağaç çiçek hava güneş yağmur kar rüzgar bulut ateş toprak taş büyük küçük
iyi kötü güzel çirkin yeni eski uzun kısa sıcak soğuk hızlı yavaş kolay zor
doğru yanlış açık kapalı tam boş dolu siyah beyaz kırmızı mavi yeşil sarı gel
git gelmek gitmek yap yapmak et etmek ol olmak al almak ver vermek bak bakmak
gör görmek bil bilmek söyle söylemek konuş konuşmak yaz yazmak oku okumak
dinle dinlemek iste istemek sev sevmek bekle beklemek başla başlamak bitir
bitirmek aç kapat çalış çalışmak dur durmak otur oturmak kalk kalkmak koş
yürü düşün düşünmek anla anlamak sor sormak cevap soru sorun şey neden niye
nasıl nerede ne kim hangi kaç kadar gibi için göre kadar sadece yine artık
belki galiba herhalde tabii elbette lütfen teşekkür merhaba selam tamam peki
hani yani işte şöyle böyle öyle burada orada şurada içinde dışında üstünde
altında yanında karşı arka ön sağ sol yukarı aşağı ilk son orta yarım çeyrek
sayfa dosya mesaj haber konu fikir plan proje toplantı rapor liste not
""".split())

# How a correction waits among the pending names: "heard -> spelling".
PAIR = " -> "
# Words on either side of a correction, at most.
CORRECTION_WORDS = 4
# How alike the two sides must sound, from 0 to 1. A misheard name sounds like
# itself; a rewrite of the sentence does not.
CORRECTION_LIKENESS = 0.6
# How alike a short word and the longer one it starts must be to still count.
CORRECTION_SAME = 0.9

_SOUNDS = (("ph", "f"), ("ck", "k"), ("c", "k"), ("q", "k"), ("x", "ks"),
           ("w", "v"), ("y", "i"), ("au", "o"), ("ou", "u"), ("ee", "i"),
           ("oo", "u"))


def _plain(text):
    """Folded, without accents or anything but letters and digits: two
    spellings that differ only in case, punctuation, diacritics or the space
    between the words come out the same."""
    text = unicodedata.normalize("NFKD", fold(text))
    return "".join(c for c in text if c.isalnum())


def _sound(words):
    """A rough sound of some words, so that Klod Kod and Claude Code meet:
    the spellings English and Turkish write the same sound with, doubled
    letters single, and a silent final e gone."""
    out = []
    for word in words:
        word = _plain(word)
        for written, said in _SOUNDS:
            word = word.replace(written, said)
        word = re.sub(r"(.)\1+", r"\1", word)
        if len(word) > 3 and word.endswith("e"):
            word = word[:-1]
        out.append(word)
    return "".join(out)


def _technical(word):
    """A digit or a dot in it (GPT-4o, Node.js): a term whatever its case."""
    return any(c.isdigit() for c in word) or "." in word


def _namey(word):
    """Title or camel case, or technical: how a name or a term looks, rather
    than prose."""
    return _titled(word) or _technical(word)


# Sounds and words the cleanup drops as filler, which the alignment would
# otherwise take into the heard side of a correction ("uh klod kod").
FILLERS = frozenset(fold(w) for w in """
uh uhh um umm uhm er erm ah eh hm hmm mm mmm ehm
ee eee eeee ıı ııı ıııı öö ööö şey yani hani işte
""".split())


def corrections(raw, text):
    """(heard, spelling) pairs: runs of up to four words the cleanup swapped
    for a name or a term that sounds like them.

    A swap is widened to the whole name it is part of, so "cloud code" made
    "Claude Code" is that pair and not "cloud" for "Claude". Filler at either
    end of the heard side is left out. Not a correction: words only added or
    only dropped, a change of case, punctuation, accents or a Turkish suffix
    (Ahmet'e for Ahmet), a word that was only moved (it is in the transcript
    elsewhere), a heard side made only of everyday words, and a heard side
    that is the start of a longer word (mark for Marketing)."""
    heard = [m.group(0) for m in _WORD.finditer(raw)]
    matches = list(_WORD.finditer(text))
    written = [m.group(0) for m in matches]
    heard_keys = [_key(w) for w in heard]
    written_keys = [_key(w) for w in written]
    heard_joined = _joined(heard_keys)

    def together(left, right):
        """Two cleaned words side by side on one line, in one sentence."""
        gap = text[matches[left].end():matches[right].start()]
        return not gap.strip() and "\n" not in gap

    def begins(index):
        """A word whose capital may only be the sentence's: not widened onto."""
        gap = text[:matches[index].start()].rstrip(" \t")
        return not gap or gap[-1] in _SENTENCE_END

    found, seen = [], set()
    matcher = difflib.SequenceMatcher(None, heard_keys, written_keys, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op != "replace":
            continue
        # The rest of the name, where the transcriber got it right.
        while (i1 > 0 and j1 > 0 and _namey(written[j1 - 1])
               and not begins(j1 - 1)
               and heard_keys[i1 - 1] == written_keys[j1 - 1] and together(j1 - 1, j1)):
            i1, j1 = i1 - 1, j1 - 1
        while (i2 < len(heard) and j2 < len(written) and _namey(written[j2])
               and heard_keys[i2] == written_keys[j2] and together(j2 - 1, j2)):
            i2, j2 = i2 + 1, j2 + 1
        before, after = heard[i1:i2], written[j1:j2]
        while before and _key(before[0]) in FILLERS:
            before = before[1:]
        while before and _key(before[-1]) in FILLERS:
            before = before[:-1]
        if not (1 <= len(before) <= CORRECTION_WORDS
                and 1 <= len(after) <= CORRECTION_WORDS):
            continue
        # The widening stopped at a capitalised word that starts the
        # sentence: the name may run on into it ("Claude kod is great"), so
        # what is left is part of a name, and a part is not learned.
        # An everyday word that only has its capital for starting the
        # sentence ("Try cloud code") is not part of the name, though.
        if (j1 > 0 and _namey(written[j1 - 1]) and together(j1 - 1, j1)
                and written_keys[j1 - 1] not in EVERYDAY):
            continue
        # A capital that only starts the sentence makes nothing a name.
        if begins(j1) and not any(_titled(w) for w in after[1:]) \
                and not _technical(after[0]):
            continue
        if not any(_namey(w) for w in after) or any(_key(w) in COMMON for w in after):
            continue
        if all(_key(w) in EVERYDAY for w in before):
            continue
        # The name was heard right, and only the words around it were
        # rewritten ("on github" made "We use GitHub"): nothing to correct.
        if {_key(w) for w in after if _namey(w)} <= {_key(w) for w in before}:
            continue
        # Said, just somewhere else: the cleanup moved it, it did not hear it.
        if _joined([_key(w) for w in after]) in heard_joined:
            continue
        plain_before, plain_after = _plain(" ".join(before)), _plain(" ".join(after))
        if plain_before == plain_after:
            continue
        likeness = max(
            difflib.SequenceMatcher(None, plain_before, plain_after).ratio(),
            difflib.SequenceMatcher(None, _sound(before), _sound(after)).ratio())
        if likeness < CORRECTION_LIKENESS:
            continue
        shorter, longer = sorted((plain_before, plain_after), key=len)
        if longer.startswith(shorter):
            # A suffix the cleanup took off or put on (Ahmeti and Ahmet), or
            # a short word grown into a longer one (mark and Marketing),
            # unless the two are as good as the same.
            if len(longer) - len(shorter) <= 4 or likeness < CORRECTION_SAME:
                continue
        pair = (" ".join(before), " ".join(after))
        if (_key(pair[0]), _key(pair[1])) not in seen:
            seen.add((_key(pair[0]), _key(pair[1])))
            found.append(pair)
    return found


def learn(conf, raw, text, speech_language=""):
    """Count the names in one finished dictation, and learn the ones that have
    now been seen often enough. Returns the records added.

    Only a term that both the transcriber heard and the cleanup kept counts:
    the transcript and the cleaned text agree on it, which is what a real name
    looks like and a misheard word mostly does not. Each term counts once per
    dictation. A run at the start of a sentence counts only once the same run
    has been seen in the middle of one, here or in an earlier dictation. The
    caller decides which dictations are eligible at all."""
    if capitalises_nouns(speech_language, raw):
        return []
    heard = {_key(w) for w in _WORD.findall(raw)}
    found = [(term, starts) for term, starts in runs(text)
             if all(_key(w) in heard for w in term.split())]
    # Before the lock: it reads only the two texts, and it is the slow part.
    swaps = corrections(raw, text)
    with _locked():
        current, stored, _ = _state(conf, _read_for_write())
        stored = stored or _empty()
        pending, rejected = stored["pending"], stored["rejected"]
        clock = stored["clock"] + 1
        middle = {term for term, starts in found if not starts}
        terms = []
        for term, starts in found:
            if starts and term not in middle and _key(term) not in pending:
                continue
            # Once a dictation, however it was capitalised (GitHub, Github).
            if _key(term) not in {_key(t) for t in terms}:
                terms.append(term)
        known = set(rejected)
        for record in current:
            known.add(_key(record["term"]))
            known.add(_key(hint_of(record)))
        # A spelling that is only part of a longer listed name ("Code" of
        # "Claude Code") is that name heard in part, not a term of its own.
        longer = [_joined(_tokens(hint_of(r))) for r in current
                  if len(_tokens(hint_of(r))) > 1]
        fixed = [(before, after) for before, after in swaps
                 if _key(before) not in known and _key(after) not in known
                 and not any(_joined(_tokens(after)) in name for name in longer)]
        terms = [term for term in terms if _key(term) not in known]
        # Every term and correction left changes a count, so with none left
        # the file is not rewritten for nothing.
        if not terms and not fixed:
            return []
        added = []
        for before, after in fixed:
            # Folded, so the same correction counts as one however the
            # transcriber happened to capitalise it.
            key = _key(before) + PAIR + _key(after)
            count = pending.get(key, [0, 0])[0] + 1
            if count >= LEARN_AFTER:
                pending.pop(key, None)
                record = entry(before, after, AUTO)
                current.append(record)
                known.update((_key(before), _key(after)))
                added.append(record)
            else:
                pending[key] = [count, clock]
        for term in terms:
            if _key(term) in known:
                continue
            count = pending.get(_key(term), [0, 0])[0] + 1
            if count >= LEARN_AFTER:
                pending.pop(_key(term), None)
                record = entry(term, source=AUTO)
                current.append(record)
                known.add(_key(term))
                added.append(record)
            else:
                pending[_key(term)] = [count, clock]
        if len(pending) > PENDING_LIMIT:
            # Least recently seen out first, whatever the count.
            recent = sorted(pending.items(), key=lambda item: -item[1][1])
            pending = dict(recent[:PENDING_LIMIT])
        # The setting is left as it is: this thread does not write config.json.
        # The file says which text it agrees with, so the two stay in step.
        _write(current, pending, _text(conf), rejected, clock)
        return added
