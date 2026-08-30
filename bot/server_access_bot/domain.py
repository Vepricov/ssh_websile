from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    telegram_user_id: int
    telegram_username: str | None
    full_name: str
    recommender: str


@dataclass(frozen=True)
class ServerRequest:
    telegram_user_id: int
    telegram_username: str | None
    server_username: str
    access_term: str
    activity: str
    servers: tuple[str, ...]


@dataclass(frozen=True)
class RegistryMatch:
    telegram_username: str
    full_name: str
    server_username: str
    access_term: str
    recommender: str
    activity: str
    servers: tuple[str, ...]
    source_row_count: int
    telegram_user_id: int | None = None


@dataclass(frozen=True)
class Application:
    telegram_user_id: int
    telegram_username: str | None
    full_name: str
    server_username: str
    access_term: str
    recommender: str
    activity: str
    servers: tuple[str, ...]


@dataclass(frozen=True)
class ProvisionedAccess:
    host: str
    username: str
    password: str | None
    connection: str
    group_url: str
    created: bool


@dataclass(frozen=True)
class GpuIdleCandidate:
    host: str
    gpu_uuid: str
    gpu_index: int
    server_username: str
    used_memory_mb: int
    pids: tuple[int, ...]


@dataclass(frozen=True)
class GpuIdleIncident:
    key: str
    host: str
    gpu_index: int
    server_username: str
    used_memory_mb: int
    pids: tuple[int, ...]
    first_seen_at: int
    last_seen_at: int
