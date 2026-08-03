"""OpenAI, Groq, OpenRouter and this machine, stdlib only.

Transcription runs on any of the four: Groq and OpenRouter both mirror OpenAI's
/audio/transcriptions endpoint field for field, and ggml.py starts whisper.cpp
on that same path, so one multipart request serves all of them and only the key,
the base URL and the model id change. llama.cpp answers /chat/completions the way
OpenRouter does, so cleanup here is the same request too.

What is on this machine has no key, and its base URL is not known until a server
is up, which is the one thing this module has to fill in for it.
"""

import collections
import contextlib
import http.client
import json
import mimetypes
import os
import secrets
import threading
import urllib.error
import urllib.request

import ggml
from i18n import t
from platforms import adapter

runtime = adapter("runtime")

APP_URL = "https://github.com/yusufipk/dikte"
USER_AGENT = f"dikte/1.0 (+{APP_URL})"
OPENAI_URL = "https://api.openai.com/v1"
GROQ_URL = "https://api.groq.com/openai/v1"
OPENROUTER_URL = "https://openrouter.ai/api/v1"

# The floor for a local request. The timeouts elsewhere are sized for a hosted
# API, where a slow answer is a bill running; here the only thing being spent is
# time, and a long recording on a machine without a graphics card takes a good
# deal of it. Cutting that off would throw the work away for nothing.
LOCAL_TIMEOUT = 3600

# Where a transcription request goes; built by config.Config.transcribe_target().
# `service` is the name the user sees in an error, `provider` the one the code
# branches on.
Target = collections.namedtuple("Target", "provider service api_key base_url model")


def timestamp_model(provider, selected=""):
    """Which model answers with segment times.

    OpenAI keeps them to whisper-1 and OpenRouter namespaces that id. Everything
    Groq transcribes with is a whisper, so the model already chosen does it and
    the fallback is only for a provider left on its default. So is everything the
    local server runs, whatever the file is called, and there asking for another
    model would name one it has never heard of.
    """
    if provider in ("groq", "local"):
        return selected or "whisper-large-v3-turbo"
    return "openai/whisper-1" if provider == "openrouter" else "whisper-1"


class ApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Aborted(Exception):
    """A request that was cut off from another thread rather than answered."""


class Aborter:
    """A Stop button that reaches the call a worker thread is blocked inside.

    urlopen() hands nothing back until the server has answered, and a whisper on
    this machine is minutes away from answering, so a flag read between calls is
    a Stop that does nothing until the work it was meant to stop is already
    done. What is registered here is cut off where it stands instead.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cancels = []
        self.aborted = False

    def abort(self):
        with self._lock:
            self.aborted = True
            pending, self._cancels = self._cancels, []
        for cancel in pending:
            cancel()

    def check(self):
        if self.aborted:
            raise Aborted

    @contextlib.contextmanager
    def holding(self, cancel):
        """Run `cancel` if an abort lands while this block is open."""
        with self._lock:
            if self.aborted:
                raise Aborted
            self._cancels.append(cancel)
        try:
            yield
        finally:
            with self._lock:
                with contextlib.suppress(ValueError):
                    self._cancels.remove(cancel)


class _Sockets:
    """The connections one request is using, and whether it may still use any.

    A stop can land at any point of the handful of lines urllib takes to get
    from "make a connection" to "wait for the reply", so this keeps the two
    halves of the answer together: what is already open is cut, and anything
    opened after that is refused rather than quietly left to block.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._conns = []
        self._cut = False

    def add(self, conn):
        with self._lock:
            if self._cut:
                raise Aborted
            self._conns.append(conn)

    def cut(self):
        with self._lock:
            self._cut = True
            conns = list(self._conns)
        for conn in conns:
            _stop_using(conn)


def _stop_using(conn):
    """Take a connection out of use, connected or not.

    A connection whose socket is not open yet would open one on the next line,
    so the reconnect is turned off first. One that is open is being read from,
    and close() alone leaves that read waiting for bytes which are never coming
    now. What does make it return differs between the two socket libraries, so
    the platform is asked.
    """
    conn.auto_open = 0
    sock = getattr(conn, "sock", None)
    if sock is not None:
        runtime.abort_socket(sock)
    with contextlib.suppress(OSError):
        conn.close()


class _TrackedHTTP(urllib.request.HTTPHandler):
    """urllib's own handler, handing the connection it opens to `sockets`.

    That connection is what a Stop is applied to, and urlopen() makes it out of
    sight, inside the call that is about to block on it.
    """

    def __init__(self, sockets):
        super().__init__()
        self._sockets = sockets

    def http_open(self, req):
        return self.do_open(self._connect, req)

    def _connect(self, host, **kwargs):
        conn = http.client.HTTPConnection(host, **kwargs)
        self._sockets.add(conn)
        return conn


class _TrackedHTTPS(urllib.request.HTTPSHandler):
    def __init__(self, sockets):
        super().__init__()
        self._sockets = sockets

    def https_open(self, req):
        return self.do_open(self._connect, req, context=self._context)

    def _connect(self, host, **kwargs):
        conn = http.client.HTTPSConnection(host, **kwargs)
        self._sockets.add(conn)
        return conn


@contextlib.contextmanager
def _opened(req, timeout, aborter):
    """The response, left where `aborter` can cut it off."""
    if aborter is None:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            yield resp
        return
    sockets = _Sockets()
    opener = urllib.request.build_opener(_TrackedHTTP(sockets), _TrackedHTTPS(sockets))
    with aborter.holding(sockets.cut), opener.open(req, timeout=timeout) as resp:
        yield resp


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


def _request(url, data, headers, timeout=120, aborter=None):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with _opened(req, timeout, aborter) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise ApiError(f"HTTP {exc.code}: {_extract_error(body)}", exc.code) from exc
    except (OSError, http.client.HTTPException) as exc:
        # A socket that went out from under the read is this run being stopped,
        # not the network failing. URLError is an OSError, so both land here.
        if aborter is not None and aborter.aborted:
            raise Aborted from None
        raise ApiError(t("Could not connect: {reason}",
                         reason=getattr(exc, "reason", exc))) from exc
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


# What an upload says it is, decided here rather than asked of the machine.
# `mimetypes` reads the system's own table, and the systems disagree: a .wav is
# audio/x-wav on Linux and audio/wav in the Windows registry, and a user who
# has installed something that claimed .m4a can move it again. A request whose
# shape depends on the desktop it was sent from is one more thing to rule out
# when a provider starts refusing uploads, so the common formats are named.
CONTENT_TYPES = {
    ".wav": "audio/x-wav",
    ".mp3": "audio/mpeg",
    ".mpga": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".webm": "audio/webm",
    ".wma": "audio/x-ms-wma",
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mpeg": "video/mpeg",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
}


def _content_type(filename):
    ending = os.path.splitext(filename)[1].lower()
    return (CONTENT_TYPES.get(ending)
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream")


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
    ctype = _content_type(filename)
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
    headers = {"User-Agent": USER_AGENT}
    # A server on this machine has nothing to authorise, and sending it a
    # bearer token would only be a made-up one.
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if content_type:
        headers["Content-Type"] = content_type
    if provider == "openrouter":
        # What OpenRouter attributes the calls to on its app leaderboard.
        headers["HTTP-Referer"] = APP_URL
        headers["X-Title"] = "Dikte"
    return headers


def serving(server):
    """The base URL of a local server, started if it is not up yet.

    It picks its own port, so this is the first moment its address exists.
    serve() is idempotent: once it is running this costs nothing.
    """
    try:
        return server.serve()
    except ggml.LocalError as exc:
        raise ApiError(str(exc)) from None


def local_failure(service, server, exc):
    """A server that died mid-request, explained by its own output.

    Without this the message is that the connection dropped, when the reason for
    it was printed by the process at the other end.
    """
    detail = server.error()
    return ApiError(f"{service}: {exc}" + (f" ({detail})" if detail else ""),
                    exc.status)


def _transcribe_request(target, audio_path, language, prompt, response_format,
                        granularity=None, timeout=300, aborter=None):
    if target.provider == "local":
        # The timeouts here are sized for a hosted API, where a slow answer is a
        # bill running. Locally the only thing being spent is time.
        target = target._replace(base_url=serving(ggml.whisper))
        timeout = max(timeout, LOCAL_TIMEOUT)
    elif not target.api_key:
        raise ApiError(t("{service} API key is empty. Add it in Settings.",
                         service=target.service))
    fields = [("model", target.model), ("response_format", response_format)]
    if language and language != "auto":
        fields.append(("language", language))
    # OpenRouter takes the hint field and throws it away, so spare it the bytes.
    # The same words still reach the cleanup model as a glossary. whisper.cpp
    # takes it as the initial prompt, the way OpenAI does.
    if prompt and target.provider != "openrouter":
        fields.append(("prompt", prompt))
    if granularity:
        fields.append(("timestamp_granularities[]", granularity))
    body, ctype = _multipart(fields, "file", audio_path)
    try:
        return _request(
            f"{target.base_url.rstrip('/')}/audio/transcriptions", body,
            _headers(target.provider, target.api_key, ctype), timeout=timeout,
            aborter=aborter,
        )
    except ApiError as exc:
        if target.provider == "local":
            raise local_failure(target.service, ggml.whisper, exc) from None
        raise explain(exc, target.service) from None


# Whisper marks the start of a word with a leading space, so a piece of text
# that does not begin with one continues the word before it rather than starting
# a new one. Both helpers below turn on that.
def _continues_a_word(previous, following):
    return bool(previous) and not previous[-1:].isspace() and not following[:1].isspace()


def _local_text(text):
    """whisper.cpp's segments, joined back into the flowing line OpenAI returns.

    Its plain text puts one segment per line, and a segment boundary falls
    wherever the tokens fell, which in Turkish lands inside a word about as
    often as between two. Nothing takes the line break's place: whisper's own
    leading spaces are what separate the words, and a break inside "değ|iller"
    has nothing on either side of it worth keeping.
    """
    return "".join(text.split("\n"))


def _merge_word_splits(segments):
    """Fold a segment that begins mid-word into the one it continues.

    The hosted whisper-1 hands back segments cut on sentences; whisper.cpp cuts
    them on tokens, and a subtitle cue reading "değ" is not a cue. The times are
    joined along with the text, so the merged segment still covers the whole
    word.
    """
    merged = []
    for seg in segments:
        text = seg.get("text") or ""
        if merged and _continues_a_word(merged[-1]["text"], text):
            merged[-1]["text"] += text
            merged[-1]["end"] = seg.get("end") or merged[-1]["end"]
            continue
        merged.append({"text": text, "start": seg.get("start") or 0.0,
                       "end": seg.get("end") or 0.0})
    return merged


def transcribe(target, audio_path, language="", prompt="", timeout=300, aborter=None):
    data = _transcribe_request(
        target, audio_path, language, prompt, "json", timeout=timeout, aborter=aborter
    )
    text = data.get("text") or ""
    if target.provider == "local":
        text = _local_text(text)
    text = text.strip()
    if not text:
        raise ApiError(t("Transcript came back empty."))
    return text


def transcribe_segments(target, audio_path, language="", prompt="", timeout=300,
                        aborter=None):
    """[(start_seconds, end_seconds, text)] using whisper-1's verbose response."""
    data = _transcribe_request(
        target._replace(model=timestamp_model(target.provider, target.model)),
        audio_path, language, prompt, "verbose_json",
        granularity="segment", timeout=timeout, aborter=aborter,
    )
    segments = data.get("segments") or []
    if target.provider == "local":
        segments = _merge_word_splits(segments)
    out = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if text:
            start = float(seg.get("start") or 0.0)
            end = float(seg.get("end") or 0.0)
            out.append((start, max(end, start), text))
    if not out:
        text = data.get("text") or ""
        if target.provider == "local":
            text = _local_text(text)
        text = text.strip()
        if not text:
            raise ApiError(t("Transcript came back empty."))
        out = [(0.0, 0.0, text)]
    return out


def _thinking(payload, provider, reasoning):
    """Ask for as much thinking as this provider understands, or for none.

    An empty level means "whatever the model does on its own", so nothing is
    sent. The two mean opposite things by that, which is why the setting is kept
    per provider: OpenRouter's cleanup models answer straight away, while a local
    model that was trained to think will think, and cleanup is punctuation rather
    than a job worth thinking about.
    """
    if not reasoning:
        return
    if provider == "local-llm":
        # What llama.cpp passes to the chat template. The models that think read
        # it; the ones that do not ignore it.
        payload["chat_template_kwargs"] = {"enable_thinking": reasoning != "none"}
    elif reasoning != "none":
        # The thinking itself is never shown, so ask for it to be left out.
        payload["reasoning"] = {"effort": reasoning, "exclude": True}


def local_ceiling(text):
    """How much of a reply is worth waiting for from a model on this machine.

    Cleanup gives back what it was given, near enough, so a reply several times
    the length of the transcript is a model that has lost the thread rather than
    one doing the job. A small one will happily repeat the transcript until the
    context is full, and every one of those tokens is a second of somebody
    waiting. A hosted model is left alone: there the same runaway is rare, and a
    ceiling would cut the minutes short instead.
    """
    return max(512, len(text))


def cleanup(text, api_key, model, system_prompt, reasoning="",
            base_url=OPENROUTER_URL, timeout=180, provider="openrouter",
            service="OpenRouter", aborter=None):
    if not api_key and provider != "local-llm":
        raise ApiError(t("{service} API key is empty. Add it in Settings.",
                         service=service))
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"<transcript>\n{text}\n</transcript>"},
        ],
    }
    if provider == "local-llm":
        payload["max_tokens"] = local_ceiling(text)
    _thinking(payload, provider, reasoning)
    try:
        data = _request(
            f"{base_url.rstrip('/')}/chat/completions",
            json.dumps(payload).encode("utf-8"),
            _headers(provider, api_key, "application/json"),
            timeout=timeout, aborter=aborter,
        )
    except ApiError as exc:
        raise explain(exc, service) from None
    choices = data.get("choices") or []
    if not choices:
        raise ApiError(_extract_error(json.dumps(data)))
    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    if not content:
        # A thinking model can spend the whole reply on the thinking and leave
        # nothing to paste. Worth naming, because the fix is a setting rather
        # than a retry: cleanup is not a job that wants thinking.
        if message.get("reasoning_content") or message.get("reasoning"):
            raise ApiError(t("The cleanup model spent its whole reply on "
                             "thinking. Set Thinking to \u201cOff\u201d."))
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


def openai_models(api_key, base_url=OPENAI_URL, service="OpenAI"):
    """The audio models of anything that speaks OpenAI's /models, Groq included.

    `service` is only the name an error is written in, so a Groq key that is
    refused says Groq rather than OpenAI.
    """
    if not api_key:
        raise ApiError(t("{service} API key is empty. Add it in Settings.",
                         service=service))
    try:
        data = _get_json(
            f"{base_url.rstrip('/')}/models",
            {"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT},
        )
    except ApiError as exc:
        raise explain(exc, service) from None
    ids = [m["id"] for m in data.get("data", []) if m.get("id")]
    audio = [i for i in ids if "transcribe" in i or "whisper" in i]
    return sorted(audio or ids)
