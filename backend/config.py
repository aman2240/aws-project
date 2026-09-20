"""
Central app configuration. Loads and validates every credential/config
value later phases need from the environment (and .env in dev). Import
`settings` anywhere a value is needed — importing this module is what
makes a missing required credential fail loudly at startup instead of
surfacing as a confusing error deep in some phase-4 code path.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # AWS — required. Transcribe Streaming is the sole ASR path, no
    # fallback provider.
    aws_region: str
    aws_access_key_id: str
    aws_secret_access_key: str

    # Slack — required. DMs are the only notification channel.
    slack_bot_token: str
    slack_signing_secret: str

    # LLM — required. Powers decision detection and drafted answers via
    # Groq (groq_llm.py) — a Groq API key (console.groq.com), not AWS
    # Bedrock (tried and blocked at the AWS account level — see
    # groq_llm.py's docstring) or a direct Anthropic key.
    llm_api_key: str

    # Postgres — required, no default. A missing/unset value must fail
    # loudly at startup rather than silently pointing at some other
    # database. Local dev: postgresql+asyncpg://ghost:ghost@localhost:5432/ghost
    # (matches docker-compose.yml's postgres service).
    database_url: str

    # OpenSearch — optional. Not needed until phase 5 (P1); Postgres
    # covers decision history before that.
    opensearch_host: str | None = None
    opensearch_user: str | None = None
    opensearch_password: str | None = None

    # Detection tuning.
    confidence_threshold: float = Field(default=0.7)
    debounce_window_seconds: int = Field(default=12)

    # Audio format constants — reference these everywhere audio format
    # is specified (provider start calls, PCM decoding, etc.); never
    # repeat the literal values inline. Fixed by the extension's
    # capture pipeline (Phase 1), not meant to vary per deployment.
    SAMPLE_RATE_HZ: int = 16000
    AUDIO_ENCODING: str = "pcm"


settings = Settings()
