"""Explicit, bounded style preferences; examples never leave this module."""
import re

DEFAULTS = {
    "style_enabled": False,
    "style_preferences": {},
    "style_learned": {},
}
OPTIONS = {
    "formality": {"formal": "Use a formal tone.", "conversational": "Use a conversational tone."},
    "sentences": {"short": "Prefer short sentences.", "flowing": "Prefer flowing sentences."},
    "punctuation": {"minimal": "Use minimal punctuation where meaning remains clear.",
                    "standard": "Use standard punctuation."},
    "brevity": {"concise": "Remove redundant wording without dropping information.",
                "full": "Keep the speaker's level of detail."},
}


def normalize(value):
    if not isinstance(value, dict):
        return {}
    return {key: choice for key, choice in value.items()
            if key in OPTIONS and isinstance(choice, str) and choice in OPTIONS[key]}


def apply(prompt, conf):
    """Custom cleanup replaces personalization altogether, including conflicts."""
    if conf["style_enabled"] is not True or conf["cleanup_prompt"].strip():
        return prompt
    prefs = normalize(conf["style_learned"])
    prefs.update(normalize(conf["style_preferences"]))
    if not prefs:
        return prompt
    rules = "\n".join(OPTIONS[key][prefs[key]] for key in OPTIONS if key in prefs)
    return prompt + "\n\nWriting style preferences:\n" + rules + (
        "\nThese preferences override default stylistic choices only. Preserve all facts, "
        "names, numbers, meaning, intent and the spoken language, including mixed languages. "
        "Never translate, invent details or summarize away information. "
        "When a preference would change meaning, preserve the original wording."
    )


def suggest(example, before=""):
    """Local surface observations, not a model of the user's voice or accuracy.

    Return only finite choices and explanations. No source text is retained.
    Formality is intentionally left to the user: punctuation cannot establish it.
    """
    example, before = example[:10000].strip(), before[:10000].strip()
    words = re.findall(r"\w+", example, re.UNICODE)
    if len(words) < 8:
        return {}, ["Use at least eight words to preview suggestions."]
    prefs, reasons = {}, []
    endings = re.findall(r"[.!?。！？]+(?:\s|$)", example)
    if len(endings) >= 2 and len(words) / len(endings) <= 12:
        prefs["sentences"] = "short"
        reasons.append("Sentence length: at least two endings and at most 12 words per ending.")
    if endings:
        prefs["punctuation"] = "standard"
        reasons.append("Punctuation: sentence-ending punctuation is present.")
    elif not re.search(r"[,;:，；：]", example):
        prefs["punctuation"] = "minimal"
        reasons.append("Punctuation: no sentence endings, commas, colons or semicolons found.")
    prior = re.findall(r"\w+", before, re.UNICODE)
    if len(prior) >= 8 and len(words) <= len(prior) * .8:
        prefs["brevity"] = "concise"
        reasons.append("Correction: at least 20% fewer words; confirm that concision was intentional.")
    return prefs, reasons or ["No supported surface preferences found. Set preferences manually."]
