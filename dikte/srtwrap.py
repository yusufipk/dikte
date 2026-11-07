"""Breaking a long subtitle line into balanced lines.

A cue that runs past about 42 characters (the Netflix and BBC style guides)
is hard to read in one line, so it is split into as few lines as fit, each
about as long as the others. Two-line splits prefer a break after
punctuation. Only spaces are break points, so text written without them
(Chinese, Japanese, Thai) stays one line.
"""

import re
import unicodedata

MAX_LINE_CHARS = 42

# Formatting tags: HTML-style tags that start with a letter (so "a < b" is
# text) and ASS override blocks such as {\an8}.
_TAG = r"</?[A-Za-z][^<>]*>|\{\\[^}]*\}"
_TAGS = re.compile(_TAG)
# Words split on ordinary whitespace only: a no-break space stays inside a
# word, and a tag with spaces inside stays one word.
_WORDS = re.compile(rf"(?:{_TAG}|[^ \t\r\n])+")
_LONE_DASH = re.compile("^[-\u2013\u2014]$")
_BIDI = re.compile("[\u200e\u200f\u202a-\u202e]")
_AFTER_PUNCTUATION = re.compile("[,.;:!?\u060c\u061b\u061f][\"'\u201d\u00bb)]*$")
_ABBREVIATION = re.compile(r"(?:^|\s)(?:Mr|Mrs|Ms|Dr|Prof|St|Jr|Sr|vs|etc|Nr)\.$",
                           re.IGNORECASE)


def visible_length(text):
    """Length as shown on screen: tags, bidi marks and combining marks
    (Arabic vowel signs, Devanagari diacritics) take no space of their own."""
    text = _BIDI.sub("", _TAGS.sub("", text))
    return sum(1 for char in text if not unicodedata.category(char).startswith("M"))


def _number_line(line):
    # A line of only digits reads as a block number when the SRT is loaded again.
    return line.isascii() and line.isdigit()


def split_line(text, max_chars=MAX_LINE_CHARS):
    """One line as a list of lines, each at most max_chars where a word allows.

    A dash standing alone sticks to the word before it, so no line starts with
    one and looks like a new speaker.
    """
    words = []
    for token in _WORDS.findall(text):
        if _LONE_DASH.match(token) and words:
            words[-1] += f" {token}"
        else:
            words.append(token)
    total = visible_length(" ".join(words))
    if total <= max_chars or len(words) < 2:
        return [" ".join(words)]

    def greedy(width):
        lines, current = [], ""
        for word in words:
            candidate = f"{current} {word}" if current else word
            if current and visible_length(candidate) > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
        return lines

    def two_lines():
        best, best_score = None, None
        for i in range(1, len(words)):
            top, bottom = " ".join(words[:i]), " ".join(words[i:])
            top_len, bottom_len = visible_length(top), visible_length(bottom)
            if max(top_len, bottom_len) > max_chars:
                continue
            if _number_line(top) or _number_line(bottom):
                continue
            after_punctuation = (_AFTER_PUNCTUATION.search(top)
                                 and not _ABBREVIATION.search(top))
            score = abs(top_len - bottom_len) - (8 if after_punctuation else 0)
            if best is None or score < best_score:
                best, best_score = [top, bottom], score
        return best

    def balanced(count):
        # The narrowest width at which greedy filling needs only count lines.
        for width in range(-(-total // count), max_chars + 1):
            lines = greedy(width)
            if len(lines) <= count and not any(_number_line(x) for x in lines):
                return lines
        return None

    # Greedy filling at full width needs the fewest lines anything can manage,
    # so trying more than that cannot help. Without the cap, a digit word too
    # long for any line would send the search through every count up to the
    # number of words, which on a long cue takes minutes.
    fallback = greedy(max_chars)
    for count in range(-(-total // max_chars), len(fallback) + 1):
        lines = two_lines() if count == 2 else balanced(count)
        if lines:
            return lines
    return fallback


def wrap(text, max_chars=MAX_LINE_CHARS):
    """A cue's text with its long lines broken; short text comes back as it is.

    Lines that already fit are kept. Otherwise they are joined and split
    again, so a cue never ends up with one long line and one stub.
    """
    lines = text.split("\n")
    if all(visible_length(line) <= max_chars for line in lines):
        return text
    return "\n".join(split_line(" ".join(lines), max_chars))
