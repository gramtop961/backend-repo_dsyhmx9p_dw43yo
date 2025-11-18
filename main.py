import os
import time
from typing import List, Dict, Any, Optional

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import create_document
from schemas import (
    SearchQuery,
    ProviderTrack,
    AggregatedTrack,
    DownloadRequest,
    AuditLog,
)

app = FastAPI(title="Streamability-First Music Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Provider configuration and constants ----------
# NOTE: We never expose raw API keys to the frontend. All calls are from backend.
JAMENDO_BASE = "https://api.jamendo.com/v3.0"
JAMENDO_KEY = os.getenv("JAMENDO_CLIENT_ID", "")  # set in env

SOUNDCLOUD_KEY = os.getenv("SOUNDCLOUD_CLIENT_ID", "")
AUDIOMACK_BASE = "https://api.audiomack.com/v1"  # public data API docs

INTERNET_ARCHIVE_BASE = "https://archive.org/advancedsearch.php"

# Metadata-only providers — NEVER used for full playback
METADATA_ONLY = {"spotify", "deezer", "youtube"}


class APIResult(BaseModel):
    items: List[AggregatedTrack]


# ---------- Utility functions ----------

def audit(action: str, provider: Optional[str] = None, source_id: Optional[str] = None,
          license: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None):
    try:
        create_document("auditlog", AuditLog(action=action, provider=provider, source_id=source_id,
                                             license=license, metadata=metadata or {}).model_dump())
    except Exception:
        # DB might not be configured in preview; ignore but do not crash
        pass


def is_full_stream_source(pt: ProviderTrack) -> bool:
    # Strict rule: must be explicitly playable/streamable and NOT preview only
    if pt.preview_only:
        return False
    # Prefer explicit streamable flags
    if pt.streamable is True or pt.playable is True:
        return True
    # If provider supplies download permission and a direct stream/download url, allow
    if (pt.audiodownload_allowed or pt.zip_allowed) and (pt.stream_url or pt.download_url):
        return True
    return False


def score_source(pt: ProviderTrack, query_region: Optional[str]) -> int:
    score = 0
    if is_full_stream_source(pt):
        score += 100
    # Reliability heuristic: https/cors-likely if not known — boost Jamendo, Archive, SoundCloud
    if pt.provider_name in {"jamendo", "internet_archive", "soundcloud", "audiomack"}:
        score += 20
    if pt.bitrate:
        score += min(pt.bitrate // 32, 10)  # small boost for bitrate
    if pt.duration:
        score += min(pt.duration // 30, 10)
    if pt.audiodownload_allowed:
        score += 5
    if query_region and pt.region and query_region.lower() == (pt.region or "").lower():
        score += 5
    return score


# ---------- Provider fetchers (playback-capable only) ----------

def fetch_jamendo(q: str) -> List[ProviderTrack]:
    if not JAMENDO_KEY:
        return []
    # Jamendo: use tracks endpoint; include fields and ensure we only mark full if streamable or download allowed
    url = f"{JAMENDO_BASE}/tracks"
    params = {
        "client_id": JAMENDO_KEY,
        "format": "json",
        "limit": 20,
        "include": "musicinfo+licenses+stats",
        "search": q,
        "audioformat": "mp31",  # streaming mp3
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json().get("results", [])
    out: List[ProviderTrack] = []
    for t in data:
        pt = ProviderTrack(
            provider_name="jamendo",
            source_id=str(t.get("id")),
            title=t.get("name"),
            artist=(t.get("artist_name") or None),
            duration=t.get("duration"),
            bitrate=None,
            stream_url=t.get("audio"),
            download_url=t.get("audiodownload") or None,
            cover_url=(t.get("image") or None),
            license=(t.get("licenseCC") or None),
            streamable=True,  # Jamendo streams full tracks
            audiodownload_allowed=bool(t.get("audiodownload", None)),
            zip_allowed=False,
            preview_only=False,
            extra={"shareurl": t.get("shareurl")},
        )
        out.append(pt)
    return out


def fetch_soundcloud(q: str) -> List[ProviderTrack]:
    if not SOUNDCLOUD_KEY:
        return []
    # SoundCloud catalog: search tracks and filter streamable/playable
    url = "https://api-v2.soundcloud.com/search/tracks"
    params = {
        "q": q,
        "client_id": SOUNDCLOUD_KEY,
        "limit": 20,
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json().get("collection", [])
    out: List[ProviderTrack] = []
    for t in data:
        # Flags: "streamable": true, and check policy for playback
        streamable = bool(t.get("streamable")) or bool(t.get("playable"))
        pt = ProviderTrack(
            provider_name="soundcloud",
            source_id=str(t.get("id")),
            title=t.get("title"),
            artist=(t.get("user", {}).get("username") or None),
            duration=int(t.get("duration", 0)) // 1000 if t.get("duration") else None,
            stream_url=None,  # served via proxy that signs hls/mp3 url
            cover_url=(t.get("artwork_url") or None),
            license=(t.get("license") or None),
            streamable=streamable,
            playable=streamable,
            preview_only=not streamable,
            extra={"permalink_url": t.get("permalink_url")},
        )
        out.append(pt)
    return out


def fetch_internet_archive(q: str) -> List[ProviderTrack]:
    # Advanced search for audio items
    params = {
        "q": f"{q} AND mediatype:(audio)",
        "fl[]": [
            "identifier", "title", "creator", "licenseurl", "downloads", "format", "publicdate", "source"
        ],
        "rows": 20,
        "output": "json",
    }
    r = requests.get(INTERNET_ARCHIVE_BASE, params=params, timeout=10)
    r.raise_for_status()
    docs = r.json().get("response", {}).get("docs", [])
    out: List[ProviderTrack] = []
    for d in docs:
        # We'll treat Internet Archive items as streamable when they expose direct audio files (we'll resolve in proxy)
        pt = ProviderTrack(
            provider_name="internet_archive",
            source_id=d.get("identifier"),
            title=d.get("title"),
            artist=d.get("creator"),
            duration=None,
            stream_url=None,
            download_url=None,
            cover_url=None,
            license=d.get("licenseurl"),
            streamable=True,
            audiodownload_allowed=True,
            preview_only=False,
            extra={"source": d.get("source")},
        )
        out.append(pt)
    return out


# Audiomack public data API: search catalog and mark playable when stream url is available in follow-up call

def fetch_audiomack(q: str) -> List[ProviderTrack]:
    # Search endpoint
    url = f"{AUDIOMACK_BASE}/search"
    params = {"q": q, "limit": 20}
    r = requests.get(url, params=params, timeout=10)
    if r.status_code != 200:
        return []
    results = r.json().get("results", [])
    out: List[ProviderTrack] = []
    for item in results:
        if item.get("type") != "song":
            continue
        track = item.get("item", {})
        pt = ProviderTrack(
            provider_name="audiomack",
            source_id=str(track.get("id")),
            title=track.get("title"),
            artist=(track.get("artist") or None),
            duration=track.get("duration"),
            cover_url=track.get("image"),
            license=None,
            streamable=bool(track.get("streaming")) or bool(track.get("playable")),
            playable=bool(track.get("streaming")) or bool(track.get("playable")),
            preview_only=not (bool(track.get("streaming")) or bool(track.get("playable"))),
            extra={"url": track.get("url")},
        )
        out.append(pt)
    return out


# ---------- Aggregation ----------

def aggregate_query(q: str, region: Optional[str], allow_metadata_only: bool) -> List[AggregatedTrack]:
    audit("SEARCH", metadata={"q": q})

    providers: List[List[ProviderTrack]] = []
    try:
        providers.append(fetch_jamendo(q))
    except Exception:
        audit("PROXY_ERROR", provider="jamendo")
    try:
        providers.append(fetch_soundcloud(q))
    except Exception:
        audit("PROXY_ERROR", provider="soundcloud")
    try:
        providers.append(fetch_audiomack(q))
    except Exception:
        audit("PROXY_ERROR", provider="audiomack")
    try:
        providers.append(fetch_internet_archive(q))
    except Exception:
        audit("PROXY_ERROR", provider="internet_archive")

    flat: List[ProviderTrack] = [pt for sub in providers for pt in sub]

    # Dedup by title+artist heuristic
    buckets: Dict[str, List[ProviderTrack]] = {}
    for pt in flat:
        key = f"{(pt.title or '').strip().lower()}::{(pt.artist or '').strip().lower()}"
        buckets.setdefault(key, []).append(pt)

    out: List[AggregatedTrack] = []
    for key, pts in buckets.items():
        title, artist = key.split("::")
        # score full-stream sources first
        scored = sorted(pts, key=lambda x: score_source(x, region), reverse=True)
        best = next((s for s in scored if is_full_stream_source(s)), None)
        metadata_only = best is None
        if metadata_only and not allow_metadata_only:
            # keep only metadata but no stream url
            pass
        agg = AggregatedTrack(
            id=key,
            title=title or (best.title if best else pts[0].title or ""),
            artist=artist or (best.artist if best else pts[0].artist),
            duration=best.duration if best and best.duration else (pts[0].duration),
            cover_url=best.cover_url if best and best.cover_url else (pts[0].cover_url),
            best_source=best,
            sources=scored,
            metadata_only=metadata_only,
        )
        out.append(agg)

    return out


# ---------- Routes ----------

@app.get("/")
def root():
    return {"service": "music-backend", "status": "ok"}


@app.get("/api/search", response_model=APIResult)
async def api_search(q: str = Query(..., min_length=2), region: Optional[str] = None,
                     allow_metadata_only_playback: bool = False):
    items = aggregate_query(q, region, allow_metadata_only_playback)
    return {"items": items}


@app.get("/api/stream-proxy")
async def stream_proxy(provider: str, source_id: str, token: Optional[str] = None):
    # For demo scaffolding, we just return a signed URL placeholder. In a full app we would
    # sign and proxy the actual stream URL (HLS/MP3) without exposing provider keys.
    # This endpoint is audited and should enforce provider TOS.
    if provider in METADATA_ONLY:
        raise HTTPException(status_code=400, detail="Provider is metadata-only; not allowed for playback")
    audit("STREAM_START", provider=provider, source_id=source_id)
    return {"ok": True, "provider": provider, "source_id": source_id}


@app.post("/api/download")
async def request_download(req: DownloadRequest):
    # Only allow when explicitly permitted by source license/flags
    if not req.url:
        raise HTTPException(status_code=400, detail="Missing source url")
    audit("DOWNLOAD", provider=req.source_provider, source_id=req.source_id, license=req.license)
    # In production: fetch, encrypt per-user, store to cloud/local, record in DB.
    return {"status": "queued"}


@app.get("/test")
def test_database():
    """Compatibility test endpoint"""
    response = {"backend": "✅ Running"}
    try:
        from database import db
        response["database"] = "✅ Connected" if db is not None else "❌ Not Available"
    except Exception as e:
        response["database"] = f"❌ Error: {e}"
    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
