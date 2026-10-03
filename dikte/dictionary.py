"""Personal dictionary records and deliberately narrow, entirely local learning."""

import collections
import re
import unicodedata
import uuid


def key(text):
    # Turkish dotted/dotless I need mapping before Unicode case folding.
    return " ".join(unicodedata.normalize("NFC", text).translate(
        str.maketrans({"I": "ı", "İ": "i"})).casefold().split())


def entry(text, source="manual"):
    return {"id": uuid.uuid4().hex, "text": text, "source": source}


def render(entries):
    return "\n".join(row["text"] for row in entries)


def reconcile(value, legacy):
    """Keep old clients' edits authoritative; never guess freeform delimiters."""
    valid = (isinstance(value, list) and all(
        isinstance(r, dict) and isinstance(r.get("id"), str)
        and bool(r.get("id")) and isinstance(r.get("text"), str)
        and r.get("source") in ("manual", "auto") for r in value))
    if valid and len({r["id"] for r in value}) != len(value):
        valid = False
    if valid and render(value) == legacy:
        return [dict(r) for r in value]
    return [entry(legacy)] if legacy else []


def add(entries, text, source="manual", replace_id=None):
    text = unicodedata.normalize("NFC", text.strip())
    if not text:
        raise ValueError("Enter a word or expression.")
    if any(r["id"] != replace_id and key(r["text"]) == key(text)
           for r in entries):
        raise ValueError("This dictionary entry already exists.")
    row = entry(text, source)
    if replace_id is not None:
        row["id"] = replace_id
        return [row if r["id"] == replace_id else dict(r) for r in entries]
    return [*entries, row]


def candidates(raw, final):
    """Repeated mid-sentence title-case names surviving cleanup unchanged.

    No clipboard, application text, history scan, network, or model request.
    This cannot establish truth; the user can review every learned entry.
    """
    counts = collections.Counter()
    # Limit work on untrusted provider output. Exclude sentence-initial words,
    # URLs, email, digits, acronyms, repeated letters and one-letter tokens.
    for sentence in re.split(r"[.!?\n]", raw[:50000]):
        words = sentence.split()
        for word in words[1:]:
            word = word.strip(",;:()\"“”")
            if (3 <= len(word) <= 40 and word.isalpha()
                    and word[0].isupper() and word[1:].islower()
                    and not re.search(r"(.)\1\1", key(word))):
                counts[word] += 1
    final_words = set(re.findall(r"[^\W\d_]+", final[:50000]))
    return [word for word, count in counts.items()
            if count >= 2 and word in final_words][:20]
