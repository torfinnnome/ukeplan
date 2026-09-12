import logging
import os
import pathlib
from typing import List, Optional
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_name: str = "Week Planner"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    secret_token: str = "change-me-secret-token"

    # Database
    database_url: str = "sqlite:///./ukeplan.db"
    # On-disk storage for uploaded documents (archive browsable at /documents)
    upload_dir: str = "data/uploads"

    # Data retention: records older than this many weeks are removed automatically.
    # This covers calendar entries (plan items) whose date is in the past, and
    # uploaded documents (PDFs/photos) whose upload (created_at) is in the past —
    # the document's file on disk is deleted too. Recurring-weekly items are
    # standing entries (they repeat every week) and are never auto-removed.
    # The purge runs in a background task (at startup and then on an interval) —
    # no external cron job needed.
    item_retention_weeks: int = 12
    item_purge_interval_seconds: int = 86400
    item_autopurge: bool = True


    # Default family members / children configuration
    # Format: list of dicts {"name": "Ann", "school_class": "1A"} or strings
    default_members: List[dict] = [
        {"name": "Ann", "school_class": "1A", "display_order": 1},
        {"name": "Berit", "school_class": "6B", "display_order": 2},
        {"name": "Matthew", "school_class": "10C", "display_order": 3},
        {"name": "Mom", "school_class": None, "display_order": 4},
        {"name": "Dad", "school_class": None, "display_order": 5},
    ]

    # AI Backend Engine: "openai_compatible" (works with llama.cpp, vLLM, TGI, LocalAI, or Cloud) or "ollama"
    ai_engine: str = "openai_compatible"
    ai_base_url: str = "http://localhost:8089/v1"
    ai_model: str = "qwen2.5-7b-instruct"
    ai_api_key: Optional[str] = "not-needed"

    # OCR stage: any OpenAI-compatible vision endpoint used to transcribe every
    # PDF page and photo upload to markdown (e.g. llama.cpp serving GLM-OCR).
    # The extraction LLM above then structures the text; it never sees images.
    # If OCR is disabled or the endpoint is unavailable, PDFs fall back to their
    # digital text layer and photos yield no text.
    ocr_enabled: bool = False
    ocr_base_url: str = "http://localhost:8080/v1"
    ocr_model: str = "glm-ocr"
    ocr_api_key: Optional[str] = "not-needed"
    # When the OCR endpoint ingests PDF files natively (z.ai API/SDK), the
    # extractor sends the whole PDF in one call. llama.cpp's multimodal
    # loader only accepts image/audio/video inputs — it rejects
    # data:application/pdf URIs — so set OCR_PDF_NATIVE=false there and
    # the extractor renders pages to PNG (one call per page) instead.
    ocr_pdf_native: bool = True


    # Legacy Ollama fields (optional fallback)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    # Email / IMAP config
    imap_enabled: bool = False
    imap_host: str = "imap.example.com"
    imap_port: int = 993
    imap_user: str = "ukeplan@example.com"
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_poll_interval_seconds: int = 120


# Old env var names that pydantic silently ignores (extra="ignore").
# If any are present in .env, warn at startup so a stale config is visible.
_LEGACY_ENV_NAMES = {
    "GLM_OCR_ENABLED": "OCR_ENABLED",
    "GLM_OCR_BASE_URL": "OCR_BASE_URL",
    "GLM_OCR_MODEL": "OCR_MODEL",
    "GLM_OCR_API_KEY": "OCR_API_KEY",
    "USE_LLM_OCR": "OCR_ENABLED",
}


def _warn_legacy_env_vars():
    """Scan the .env file for legacy key names and log a warning for each."""
    env_file = pathlib.Path(".env")
    if not env_file.exists():
        return
    found = set()
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in _LEGACY_ENV_NAMES:
                found.add(key)
    for old in sorted(found):
        logging.getLogger("ukeplan.config").warning(
            f".env contains unused key '{old}'; rename it to '{_LEGACY_ENV_NAMES[old]}'"
        )


settings = Settings()
_warn_legacy_env_vars()
