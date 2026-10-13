from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator


class Credentials(BaseModel):
    address: str = Field(min_length=1, max_length=512)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)
    ssh_port: int = Field(default=22, ge=1, le=65535)

    @field_validator("address", "username")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value


class VolumeInfo(BaseModel):
    path: str
    filesystem: str
    total_bytes: int
    used_bytes: int
    available_bytes: int
    usage_percent: int


class DiscoveryResponse(BaseModel):
    host: str
    server_fingerprint: str
    volumes: list[VolumeInfo]
    identity: dict | None = None


VolumePath = Annotated[str, Field(pattern=r"^/volume[0-9]+$")]


class AnalysisRequest(Credentials):
    volumes: list[VolumePath] = Field(min_length=1, max_length=32)
    large_file_mb: int = Field(default=1024, ge=0, le=10_000_000)
    # Legacy clients may still send these; results are no longer truncated.
    max_large_files: int = Field(default=0, ge=0, le=10_000_000)
    max_hardlink_groups: int = Field(default=0, ge=0, le=10_000_000)

    @field_validator("volumes")
    @classmethod
    def unique_volumes(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))


class JobCreated(BaseModel):
    job_id: str


class JobResponse(BaseModel):
    id: str
    status: Literal["queued", "running", "cancelling", "cancelled", "completed", "failed"]
    progress: int
    stage: str
    created_at: str
    updated_at: str
    error: str | None = None
    result: dict | None = None
