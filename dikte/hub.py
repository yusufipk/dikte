"""Where the programs and the models come from: GitHub releases and Hugging Face.

Both answer plain JSON over HTTPS without a key, and both publish a sha256 for
every file they hand out: GitHub as the asset digest, Hugging Face as the LFS
object id. Nothing that lands on disk is trusted for having arrived, which
matters more here than it usually would, because half of what is fetched is a
program Dikte then runs.

The lists are read rather than kept. A model catalogue written into the source
means a release of Dikte for every new model, and a pinned whisper.cpp version
means one for every whisper.cpp release; both of those are somebody else's news,
not Dikte's. Answers are cached for a few hours, and a cache that has gone stale
is still a better answer than none when the network is down.

Nothing here imports the rest of Dikte apart from two leaves, the string table
and the path map: this module knows two websites and nothing about dictation.
"""

import collections
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import paths
from .i18n import t

GITHUB_API = "https://api.github.com"
HF_API = "https://huggingface.co/api"
HF_FILES = "https://huggingface.co"
USER_AGENT = "dikte/1.0 (+https://github.com/yusufipk/dikte)"

CACHE_DIR = paths.cache_dir()
# Long enough that opening the settings window twice in an evening asks nobody
# anything, short enough that a model published this morning is offered today.
CACHE_TTL = 6 * 3600

# `sha256` is empty for the few files neither side stores in LFS; those are the
# small ones, and a checksum is only worth having where there is something to
# check.
Item = collections.namedtuple("Item", "name url size sha256")
Repo = collections.namedtuple("Repo", "id downloads updated")


class HubError(Exception):
    pass


def _get(url, timeout=20):
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        exc.close()   # it holds the response body open until it is collected
        raise HubError(t("{url} answered HTTP {code}.",
                         url=urllib.parse.urlsplit(url).netloc, code=exc.code)) from exc
    except urllib.error.URLError as exc:
        raise HubError(t("Could not reach {url}: {error}",
                         url=urllib.parse.urlsplit(url).netloc,
                         error=exc.reason)) from exc
    except (ValueError, OSError) as exc:
        raise HubError(t("Could not read the answer from {url}: {error}",
                         url=urllib.parse.urlsplit(url).netloc, error=exc)) from exc


def _cache_file(key):
    safe = "".join(c if c.isalnum() or c in "-._" else "-" for c in key)
    return CACHE_DIR / f"{safe}.json"


def _read_cache(key, ttl):
    """What was stored under this key, or None. `ttl` of 0 ignores the age."""
    path = _cache_file(key)
    try:
        age = time.time() - path.stat().st_mtime
        if ttl and age > ttl:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_cache(key, payload):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_file(key).write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass          # a cache that cannot be written is not a failed lookup


def _fetch(key, url, ttl=CACHE_TTL, refresh=False):
    """The JSON at `url`, from the cache when it is fresh enough.

    A lookup that fails falls back to the cache however old it is: an offline
    settings window that shows yesterday's list is worth a great deal more than
    one that shows an error.
    """
    if not refresh:
        cached = _read_cache(key, ttl)
        if cached is not None:
            return cached
    try:
        payload = _get(url)
    except HubError:
        stale = _read_cache(key, 0)
        if stale is not None:
            return stale
        raise
    _write_cache(key, payload)
    return payload


def _digest(value):
    """GitHub writes its digests as "sha256:…"; Hugging Face writes the hash."""
    value = (value or "").strip()
    return value.split(":", 1)[1] if value.startswith("sha256:") else value


def release(repo, tag="latest", refresh=False):
    """(tag, [Item]) for one GitHub release, newest when no tag is given."""
    where = "latest" if tag in ("", "latest") else f"tags/{tag}"
    data = _fetch(f"gh-{repo}-{tag or 'latest'}",
                  f"{GITHUB_API}/repos/{repo}/releases/{where}", refresh=refresh)
    if not isinstance(data, dict) or not data.get("assets"):
        raise HubError(t("{repo} has no downloadable release.", repo=repo))
    assets = [Item(a.get("name") or "", a.get("browser_download_url") or "",
                   int(a.get("size") or 0), _digest(a.get("digest")))
              for a in data["assets"] if a.get("browser_download_url")]
    return data.get("tag_name") or tag, assets


def newest_release(repo, refresh=False):
    """(tag, page, published) for the newest release of a repository.

    release() above is for taking a file out of one and insists on there being
    files to take; this is for the number, which a release with nothing
    attached answers just as well. GitHub keeps prereleases out of "latest" on
    its own, which is what leaves the nightly build off this answer.
    """
    data = _fetch(f"gh-newest-{repo}",
                  f"{GITHUB_API}/repos/{repo}/releases/latest", refresh=refresh)
    if not isinstance(data, dict) or not data.get("tag_name"):
        raise HubError(t("{repo} has published no release.", repo=repo))
    return (data["tag_name"], data.get("html_url") or "",
            data.get("published_at") or "")


def files(repo, revision="main", refresh=False):
    """[Item] for every file in a Hugging Face repository.

    The size is there whether or not the file is in LFS; the hash is only there
    when it is, which for anything worth downloading it always is.
    """
    data = _fetch(f"hf-tree-{repo}-{revision}",
                  f"{HF_API}/models/{repo}/tree/{revision}?recursive=true",
                  refresh=refresh)
    if not isinstance(data, list):
        raise HubError(t("{repo} did not return a file list.", repo=repo))
    out = []
    for entry in data:
        if entry.get("type") != "file":
            continue
        path = entry.get("path") or ""
        lfs = entry.get("lfs") or {}
        out.append(Item(
            path,
            f"{HF_FILES}/{repo}/resolve/{revision}/{urllib.parse.quote(path)}",
            int(lfs.get("size") or entry.get("size") or 0),
            _digest(lfs.get("oid") or lfs.get("sha256")),
        ))
    return out


def repos(author="", search="", limit=40, refresh=False):
    """[Repo] of GGUF repositories, newest first.

    Filtered by author on purpose. Hugging Face's own trending list is open to
    everyone and reads like it: asking it for the popular GGUF today answers
    with a wall of roleplay merges, which is not what a dictation transcript
    wants cleaning up. An author is a small enough thing to trust and a large
    enough one to keep the list current without Dikte being updated.
    """
    query = {"filter": "gguf", "sort": "lastModified", "direction": "-1",
             "limit": str(limit)}
    if author:
        query["author"] = author
    if search:
        query["search"] = search
    url = f"{HF_API}/models?{urllib.parse.urlencode(query)}"
    data = _fetch(f"hf-models-{author}-{search}-{limit}", url, refresh=refresh)
    if not isinstance(data, list):
        raise HubError(t("Hugging Face did not return a model list."))
    return [Repo(m.get("id") or "", int(m.get("downloads") or 0),
                 m.get("lastModified") or "")
            for m in data if m.get("id")]
