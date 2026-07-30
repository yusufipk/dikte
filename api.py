"""OpenAI and OpenRouter calls, stdlib only.

Transcription runs on either provider: OpenRouter mirrors OpenAI's
/audio/transcriptions endpoint field for field, so one multipart request serves
both and only the key, the base URL and the model id change. Hosted cleanup is
OpenRouter; assistant.py also offers the signed-in Codex CLI.
"""

import collections
import json
import mimetypes
import os
import secrets
import urllib.error
import urllib.request

import local_whisper
from i18n import t

APP_URL = "https://github.com/yusufipk/dikte"
USER_AGENT = f"dikte/1.0 (+{APP_URL})"
OPENAI_URL = "https://api.openai.com/v1"
OPENROUTER_URL = "https://openrouter.ai/api/v1"

# Where a transcription request goes; built by config.Config.transcribe_target().
# `service` is the name the user sees in an error, `provider` the one the code
# branches on.
Target = collections.namedtuple("Target", "provider service api_key base_url model")


def timestamp_model(provider):
    """Only whisper-1 returns segment times, and OpenRouter namespaces the id."""
    return "openai/whisper-1" if provider == "openrouter" else "whisper-1"


class ApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def explain(exc, service):
    """Turn an HTTP status into something the user can act on."""
    if exc.status in (401, 403):
        return ApiError(t("{service} rejected the API key (HTTP {code}). Open "
                          "Settings and check it.", service=service, code=exc.status),
                        exc.status)
    if exc.status == 402:
        return ApiError(t("{service} says the account is out of credit (HTTP 402).",
                          service=service), exc.status)
    if exc.status == 429:
        return ApiError(t("{service} is rate limiting you (HTTP 429). Try again in "
                          "a moment.", service=service), exc.status)
    return ApiError(f"{service}: {exc}", exc.status)


def _request(url, data, headers, timeout=120):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise ApiError(f"HTTP {exc.code}: {_extract_error(body)}", exc.code) from exc
    except urllib.error.URLError as exc:
        raise ApiError(t("Could not connect: {reason}", reason=exc.reason)) from exc
    except json.JSONDecodeError as exc:
        raise ApiError(t("Could not parse the response: {error}", error=exc)) from exc


def _extract_error(body):
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body[:300]
    err = payload.get("error")
    if isinstance(err, dict):
        return err.get("message") or json.dumps(err)[:300]
    if isinstance(err, str):
        return err
    return body[:300]


def _multipart(fields, file_field, file_path):
    """Build a multipart/form-data body; returns (body, content-type)."""
    boundary = "----dikte" + secrets.token_hex(16)
    out = bytearray()
    for name, value in fields:
        if value is None or value == "":
            continue
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += str(value).encode("utf-8") + b"\r\n"

    filename = os.path.basename(file_path)
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(file_path, "rb") as fh:
        payload = fh.read()
    out += f"--{boundary}\r\n".encode()
    out += (
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode()
    out += payload + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _headers(provider, api_key, content_type=None):
    headers = {"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT}
    if content_type:
        headers["Content-Type"] = content_type
    if provider == "openrouter":
        # What OpenRouter attributes the calls to on its app leaderboard.
        headers["HTTP-Referer"] = APP_URL
        headers["X-Title"] = "Dikte"
    return headers


def _transcribe_request(target, wav_path, language, prompt, response_format,
                        granularity=None, timeout=300):
    if not target.api_key:
        raise ApiError(t("{service} API key is empty. Add it in Settings.",
                         service=target.service))
    fields = [("model", target.model), ("response_format", response_format)]
    if language and language != "auto":
        fields.append(("language", language))
    # OpenRouter takes the hint field and throws it away, so spare it the bytes.
    # The same words still reach the cleanup model as a glossary.
    if prompt and target.provider == "openai":
        fields.append(("prompt", prompt))
    if granularity:
        fields.append(("timestamp_granularities[]", granularity))
    body, ctype = _multipart(fields, "file", wav_path)
    try:
        return _request(
            f"{target.base_url.rstrip('/')}/audio/transcriptions", body,
            _headers(target.provider, target.api_key, ctype), timeout=timeout,
        )
    except ApiError as exc:
        raise explain(exc, target.service) from None


def transcribe(target, wav_path, language="", prompt="", timeout=300):
    if target.provider == "local":
        try:
            return local_whisper.transcribe(
                wav_path, model_path=target.model, language=language,
                prompt=prompt, timeout=max(timeout, 600),
            )
        except local_whisper.LocalWhisperError as exc:
            raise ApiError(str(exc)) from exc
    data = _transcribe_request(
        target, wav_path, language, prompt, "json", timeout=timeout
    )
    text = (data.get("text") or "").strip()
    if not text:
        raise ApiError(t("Transcript came back empty."))
    return text


def transcribe_segments(target, wav_path, language="", prompt="", timeout=300):
    """[(start_seconds, end_seconds, text)] using whisper-1's verbose response."""
    if target.provider == "local":
        try:
            return local_whisper.transcribe_segments(
                wav_path, model_path=target.model, language=language,
                prompt=prompt, timeout=max(timeout, 600),
            )
        except local_whisper.LocalWhisperError as exc:
            raise ApiError(str(exc)) from exc
    data = _transcribe_request(
        target._replace(model=timestamp_model(target.provider)),
        wav_path, language, prompt, "verbose_json",
        granularity="segment", timeout=timeout,
    )
    segments = data.get("segments") or []
    out = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if text:
            start = float(seg.get("start") or 0.0)
            end = float(seg.get("end") or 0.0)
            out.append((start, max(end, start), text))
    if not out:
        text = (data.get("text") or "").strip()
        if not text:
            raise ApiError(t("Transcript came back empty."))
        out = [(0.0, 0.0, text)]
    return out


def cleanup(text, api_key, model, system_prompt, reasoning="",
            base_url=OPENROUTER_URL, timeout=180):
    if not api_key:
        raise ApiError(t("{service} API key is empty. Add it in Settings.",
                         service="OpenRouter"))
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"<transcript>\n{text}\n</transcript>"},
        ],
    }
    # An empty level means "whatever the model does on its own"; anything else is
    # one of OpenRouter's efforts. The thinking itself is never shown, so ask for
    # it to be left out of the reply.
    if reasoning:
        payload["reasoning"] = {"effort": reasoning, "exclude": True}
    try:
        data = _request(
            f"{base_url.rstrip('/')}/chat/completions",
            json.dumps(payload).encode("utf-8"),
            _headers("openrouter", api_key, "application/json"),
            timeout=timeout,
        )
    except ApiError as exc:
        raise explain(exc, "OpenRouter") from None
    choices = data.get("choices") or []
    if not choices:
        raise ApiError(_extract_error(json.dumps(data)))
    content = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not content:
        raise ApiError(t("The cleanup model returned an empty reply."))
    return content


def chat(messages, api_key, model, system_prompt, reasoning="",
         base_url=OPENROUTER_URL, timeout=180):
    """A conversation, rather than one transcript rewritten.

    The messages are the whole history and come back unchanged; the caller keeps
    them, because there is no session on OpenRouter's side to resume.
    """
    if not api_key:
        raise ApiError(t("{service} API key is empty. Add it in Settings.",
                         service="OpenRouter"))
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}] + list(messages),
    }
    if reasoning:
        payload["reasoning"] = {"effort": reasoning, "exclude": True}
    try:
        data = _request(
            f"{base_url.rstrip('/')}/chat/completions",
            json.dumps(payload).encode("utf-8"),
            _headers("openrouter", api_key, "application/json"),
            timeout=timeout,
        )
    except ApiError as exc:
        raise explain(exc, "OpenRouter") from None
    choices = data.get("choices") or []
    if not choices:
        raise ApiError(_extract_error(json.dumps(data)))
    content = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not content:
        raise ApiError(t("The model returned an empty reply."))
    return content


def _get_json(url, headers, timeout=20):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise ApiError(f"HTTP {exc.code}: {_extract_error(body)}", exc.code) from exc
    except urllib.error.URLError as exc:
        raise ApiError(t("Could not connect: {reason}", reason=exc.reason)) from exc
    except json.JSONDecodeError as exc:
        raise ApiError(t("Could not parse the response: {error}", error=exc)) from exc


def openrouter_key_status(api_key):
    """Check the key against OpenRouter's own /key endpoint."""
    if not api_key:
        raise ApiError(t("{service} API key is empty. Add it in Settings.",
                         service="OpenRouter"))
    try:
        data = _get_json(f"{OPENROUTER_URL}/key",
                         {"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT})
    except ApiError as exc:
        raise explain(exc, "OpenRouter") from None
    info = data.get("data") or {}
    limit, usage = info.get("limit"), info.get("usage")
    if limit is None:
        return t("Key works, no spending limit set.")
    return t("Key works. Used {usage} of {limit}.",
             usage=round(float(usage or 0), 3), limit=round(float(limit), 3))


def openrouter_models(api_key="", transcription=False):
    """Model ids available on OpenRouter (no key required).

    `transcription` narrows the list to the speech-to-text models, the only ones
    /audio/transcriptions accepts. The filter is applied again on the result,
    because a query parameter the API stops honouring would otherwise quietly
    hand back all several hundred models.
    """
    url = f"{OPENROUTER_URL}/models"
    if transcription:
        url += "?output_modalities=transcription"
    headers = {"User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    models = _get_json(url, headers).get("data", [])
    if transcription:
        models = [m for m in models
                  if "transcription" in (m.get("architecture") or {}).get(
                      "output_modalities", [])]
    return sorted(m["id"] for m in models if m.get("id"))


def openai_models(api_key, base_url=OPENAI_URL):
    if not api_key:
        raise ApiError(t("{service} API key is empty. Add it in Settings.",
                         service="OpenAI"))
    try:
        data = _get_json(
            f"{base_url.rstrip('/')}/models",
            {"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT},
        )
    except ApiError as exc:
        raise explain(exc, "OpenAI") from None
    ids = [m["id"] for m in data.get("data", []) if m.get("id")]
    audio = [i for i in ids if "transcribe" in i or "whisper" in i]
    return sorted(audio or ids)
