"""OpenAI-compatible endpoints and the few protocol differences they keep."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    service: str
    base_url: str
    key: str
    transcription: bool = False
    chat: bool = True
    timing: str = "selected"
    reasoning: str = "custom"
    catalog: str = "openai"
    attribution: bool = False
    audio_chat: bool = False


PRESETS = {
    "groq": Preset("Groq", "https://api.groq.com/openai/v1", "groq_api_key",
                   transcription=True, timing="whisper", catalog="audio"),
    "requesty": Preset("Requesty", "https://router.requesty.ai/v1", "requesty_api_key",
                      transcription=True, timing="file", reasoning="requesty",
                      catalog="managed", attribution=True),
    "gemini": Preset("Google AI Studio", "https://generativelanguage.googleapis.com/v1beta/openai",
                     "gemini_api_key", transcription=True, timing="none",
                     reasoning="gemini", catalog="gemini", audio_chat=True),
    "deepseek": Preset("DeepSeek", "https://api.deepseek.com", "deepseek_api_key",
                       reasoning="deepseek"),
    "opencode": Preset("OpenCode Go", "https://opencode.ai/zen/go/v1", "opencode_api_key",
                       reasoning="router"),
    "other": Preset("Other", "", "custom_api_key", transcription=True,
                    reasoning="custom", catalog="custom"),
}
ALIASES = tuple(name for name in PRESETS if name != "other")


def capabilities(provider, preset=""):
    # Old API callers can still name a gateway directly.
    return PRESETS.get(preset or provider)


def model_setting(role, preset):
    # Reuse stored model names so switching presets retains each one's choice.
    name = "custom" if preset == "other" else preset
    return f"{name}_transcribe_model" if role == "transcribe" else f"{role}_{name}_model"
