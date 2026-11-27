"""Runtime settings. The API key is read from the environment and never logged."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# First match wins. BB_ANONYM_TOKEN is the name used by the bb-mask documentation.
TOKEN_ENV_VARS = ("BB_ANONYM_TOKEN", "BBMASK_TOKEN", "TOKEN")
DEFAULT_BASE_URL = "https://app.bbmask.co"
DEFAULT_TIMEOUT = 150.0
DEFAULT_CORPUS_DIR = "olusturma"
DEFAULT_RUNS_DIR = "runs"


class ConfigError(RuntimeError):
    """Raised when the environment is not usable (missing key, bad value)."""


@dataclass(frozen=True)
class Settings:
    token: str
    token_source: str
    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT

    def __repr__(self) -> str:  # never leak the key through logs or tracebacks
        return (
            f"Settings(token=<{self.token_source}>, base_url={self.base_url!r}, "
            f"timeout={self.timeout})"
        )


def load_env(env_file: str | Path | None = None) -> None:
    """Load `.env` (from the given path or the working directory) without overriding the shell."""
    load_dotenv(dotenv_path=env_file or Path.cwd() / ".env", override=False)


def load_settings(env_file: str | Path | None = None) -> Settings:
    load_env(env_file)
    for name in TOKEN_ENV_VARS:
        token = (os.environ.get(name) or "").strip()
        if token:
            break
    else:
        raise ConfigError(
            "No API key found. Set BB_ANONYM_TOKEN in the environment or in .env "
            "(see .env.example). Keys are issued at https://app.bbmask.co"
        )
    try:
        timeout = float(os.environ.get("BBMASK_TIMEOUT") or DEFAULT_TIMEOUT)
    except ValueError as exc:
        raise ConfigError("BBMASK_TIMEOUT must be a number of seconds") from exc
    return Settings(
        token=token,
        token_source=name,
        base_url=(os.environ.get("BBMASK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        timeout=timeout,
    )


def default_corpus_dir() -> Path:
    return Path(os.environ.get("BBMASK_BENCH_CORPUS") or DEFAULT_CORPUS_DIR)
