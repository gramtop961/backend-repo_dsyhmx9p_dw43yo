"""
Database Schemas

Define your MongoDB collection schemas here using Pydantic models.
Each Pydantic model represents a collection in your database.
The collection name is the lowercase of the class name.
"""
from __future__ import annotations
from typing import Optional, List, Literal, Dict, Any
from pydantic import BaseModel, Field


class UserKey(BaseModel):
    user_id: str = Field(..., description="Unique user identifier")
    enc_key: str = Field(..., description="Base64 encoded per-user encryption key (Fernet)")


class AuditLog(BaseModel):
    user_id: Optional[str] = Field(None)
    action: Literal["SEARCH", "STREAM_START", "STREAM_END", "DOWNLOAD", "PROXY_ERROR"]
    provider: Optional[str] = None
    source_id: Optional[str] = None
    license: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ProviderTrack(BaseModel):
    provider_name: str
    source_id: str
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    duration: Optional[int] = None
    bitrate: Optional[int] = None
    stream_url: Optional[str] = None
    download_url: Optional[str] = None
    cover_url: Optional[str] = None
    license: Optional[str] = None
    streamable: Optional[bool] = None
    playable: Optional[bool] = None
    audiodownload_allowed: Optional[bool] = None
    zip_allowed: Optional[bool] = None
    preview_only: Optional[bool] = None
    region: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)


class AggregatedTrack(BaseModel):
    id: str
    title: str
    artist: Optional[str] = None
    duration: Optional[int] = None
    cover_url: Optional[str] = None
    best_source: Optional[ProviderTrack] = None
    sources: List[ProviderTrack] = Field(default_factory=list)
    metadata_only: bool = False


class SearchQuery(BaseModel):
    q: str
    region: Optional[str] = None
    allow_metadata_only_playback: bool = False


class DownloadRequest(BaseModel):
    user_id: str
    track_id: str
    source_provider: str
    source_id: str
    license: Optional[str] = None
    url: str

