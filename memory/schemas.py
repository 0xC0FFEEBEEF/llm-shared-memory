"""Request/response schemas (Pydantic v2)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# --- Projects ---
class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = None


class ProjectOut(BaseModel):
    id: str
    name: str
    description: Optional[str]
    created_at: datetime


# --- Sessions ---
class SessionCreate(BaseModel):
    title: Optional[str] = None
    project_id: Optional[str] = None


class SessionOut(BaseModel):
    id: str
    provider: str
    source_session_id: str
    project_id: Optional[str]
    title: Optional[str]
    created_at: datetime
    updated_at: datetime


# --- Messages ---
class MessageCreate(BaseModel):
    source_message_id: str = Field(..., alias="source_message_id")
    content: str
    role: str = "assistant"
    order: int = 0
    original_timestamp: datetime
    payload: Optional[dict[str, Any]] = None
    provider: Optional[str] = None
    source_client: Optional[str] = None

    model_config = {"populate_by_name": True}


class MessageOut(BaseModel):
    id: str
    session_id: str
    project_id: Optional[str]
    provider: str
    role: str
    order: int
    original_timestamp: datetime
    ingestion_timestamp: datetime
    source_id: str
    content: str
    payload: Optional[dict[str, Any]] = None
    source_client: Optional[str] = None


# --- Memory streams (facts / decisions / preferences) ---
class MemoryCreate(BaseModel):
    key: str = Field(..., min_length=1, max_length=255)
    value: str
    source_client: Optional[str] = None
    source_id: Optional[str] = None
    session_id: Optional[str] = None


class MemoryOut(BaseModel):
    id: str
    key: str
    value: str
    source_client: Optional[str]
    source_id: Optional[str]
    session_id: Optional[str]
    project_id: Optional[str]
    created_at: datetime


# --- Events ---
class EventCreate(BaseModel):
    type: str = Field(..., min_length=1, max_length=64)
    payload: Optional[dict[str, Any]] = None
    source_client: Optional[str] = None
    session_id: Optional[str] = None


class EventOut(BaseModel):
    id: str
    type: str
    payload: Optional[dict[str, Any]]
    source_client: Optional[str]
    session_id: Optional[str]
    project_id: Optional[str]
    created_at: datetime


# --- Project states ---
class ProjectStateCreate(BaseModel):
    state_json: dict[str, Any]
    source_client: Optional[str] = None
    session_id: Optional[str] = None


class ProjectStateOut(BaseModel):
    id: str
    project_id: str
    session_id: Optional[str]
    state_json: dict[str, Any]
    source_client: Optional[str]
    created_at: datetime
