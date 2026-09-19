from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str
    action: str = ""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _split_host_port(url: str, default_port: int) -> tuple[str, int]:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    try:
        port = parsed.port or default_port
    except ValueError as exc:
        raise ValueError(f"invalid port in URL: {url!r}") from exc
    return host, port


def _safe_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.hostname:
        return "<invalid-url>"
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    path = parsed.path or ""
    return f"{parsed.scheme}://{host}{path}"


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ollama_tags(base_url: str, timeout: float = 3.0) -> list[str]:
    parsed = urlparse(base_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 11434
    scheme = parsed.scheme or "http"
    url = f"{scheme}://{host}:{port}/api/tags"
    with urlopen(url, timeout=timeout) as response:  # noqa: S310 - local runtime probe
        payload = json.loads(response.read().decode("utf-8"))
    return [
        str(item.get("name") or "")
        for item in payload.get("models", [])
        if item.get("name")
    ]


def _gateway_health(port: int, timeout: float = 1.5) -> dict[str, object] | None:
    try:
        with urlopen(  # noqa: S310 - local runtime probe
            f"http://127.0.0.1:{port}/health",
            timeout=timeout,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    if payload.get("status") == "ok" and payload.get("version"):
        return payload
    return None


def check_python() -> CheckResult:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info >= (3, 12):
        return CheckResult("python", "pass", version)
    return CheckResult(
        "python",
        "fail",
        version,
        "Install Python 3.12 or newer before running the gateway.",
    )


def check_env_file(root: Path = PROJECT_ROOT) -> CheckResult:
    env_path = root / ".env"
    if env_path.is_file():
        return CheckResult("env_file", "pass", str(env_path))
    return CheckResult(
        "env_file",
        "fail",
        "missing .env",
        "Copy .env.template to .env and fill the local values.",
    )


def check_auth(env: Mapping[str, str]) -> CheckResult:
    token = bool((env.get("WEBHOOK_AUTH_TOKEN") or "").strip())
    password = bool((env.get("DASHBOARD_PASSWORD") or "").strip())
    if token and password:
        return CheckResult("auth", "pass", "webhook and dashboard credentials are set")
    if _truthy(env.get("GATEWAY_ALLOW_INSECURE")):
        return CheckResult(
            "auth",
            "warn",
            "insecure local mode is enabled",
            "Use only for local development; configure both secrets before deployment.",
        )
    missing = []
    if not token:
        missing.append("WEBHOOK_AUTH_TOKEN")
    if not password:
        missing.append("DASHBOARD_PASSWORD")
    return CheckResult(
        "auth",
        "fail",
        "missing " + ", ".join(missing),
        "Set both secrets, or set GATEWAY_ALLOW_INSECURE=1 for local-only access.",
    )


def check_port(env: Mapping[str, str]) -> CheckResult:
    try:
        port = _int_env(
            env,
            "GATEWAY_PORT",
            _int_env(env, "APP_PORT", 8081),
        )
    except ValueError as exc:
        return CheckResult("port", "fail", str(exc), "Set GATEWAY_PORT to a valid TCP port.")
    if _tcp_reachable("127.0.0.1", port, timeout=0.5):
        health = _gateway_health(port)
        if health is not None:
            return CheckResult(
                "port",
                "pass",
                f"Agent Gateway is already running on 127.0.0.1:{port}",
            )
        return CheckResult(
            "port",
            "fail",
            f"127.0.0.1:{port} is already in use",
            "Choose another GATEWAY_PORT or stop the process using this port.",
        )
    return CheckResult("port", "pass", f"127.0.0.1:{port} is available")


def check_redis(env: Mapping[str, str]) -> CheckResult:
    url = (env.get("REDIS_URL") or "redis://localhost:6379/0").strip()
    try:
        host, port = _split_host_port(url, 6379)
    except ValueError as exc:
        return CheckResult("redis", "fail", str(exc), "Fix REDIS_URL.")
    if not _tcp_reachable(host, port):
        return CheckResult(
            "redis",
            "fail",
            f"{_safe_url(url)} is unreachable",
            "Start Redis, or use docker compose to launch the bundled redis service.",
        )

    try:
        import redis

        client = redis.from_url(
            url,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
        try:
            client.ping()
        finally:
            client.close()
    except Exception as exc:
        return CheckResult(
            "redis",
            "fail",
            f"{_safe_url(url)}: {type(exc).__name__}: {exc}",
            "Check the Redis password/database in REDIS_URL.",
        )
    return CheckResult("redis", "pass", f"{_safe_url(url)} authenticated and reachable")


def _model_names(items: list[str]) -> set[str]:
    names: set[str] = set()
    for item in items:
        names.add(item)
        if ":" not in item:
            names.add(f"{item}:latest")
    return names


def check_llm(env: Mapping[str, str]) -> CheckResult:
    provider = (env.get("LLM_PROVIDER") or "direct").strip().lower()
    model = (env.get("LLM_MODEL") or "").strip()
    base_url = (env.get("LLM_BASE_URL") or "").strip()
    api_key = (
        (env.get("LLM_API_KEY") or "").strip()
        or (env.get("OPENAI_API_KEY") or "").strip()
        or (env.get("OMNIROUTE_API_KEY") or "").strip()
    )

    if not model:
        return CheckResult("llm", "fail", "LLM_MODEL is empty", "Set LLM_MODEL.")

    if provider in {"local", "ollama"}:
        if not base_url:
            return CheckResult(
                "llm",
                "fail",
                "LLM_BASE_URL is empty",
                "Set LLM_BASE_URL=http://localhost:11434/v1.",
            )
        try:
            names = _model_names(_ollama_tags(base_url))
        except Exception as exc:
            return CheckResult(
                "llm",
                "fail",
                f"{base_url}: {type(exc).__name__}: {exc}",
                "Start Ollama with `ollama serve`, then verify `ollama list`.",
            )
        if model not in names:
            return CheckResult(
                "llm",
                "fail",
                f"model {model!r} is not installed",
                f"Run `ollama pull {model}`.",
            )
        fallback = (env.get("LLM_FALLBACK_MODELS") or "").strip()
        missing = [item.strip() for item in fallback.split(",") if item.strip() and item.strip() not in names]
        if missing:
            return CheckResult(
                "llm",
                "warn",
                f"primary {model} ready; fallback missing: {', '.join(missing)}",
                "Install fallback models or remove them from LLM_FALLBACK_MODELS.",
            )
        return CheckResult("llm", "pass", f"Ollama model {model} is ready")

    if not api_key:
        return CheckResult(
            "llm",
            "fail",
            f"provider {provider!r} has no API key",
            "Set LLM_API_KEY or the provider's API key.",
        )
    if not base_url:
        return CheckResult(
            "llm",
            "warn",
            f"provider {provider!r} uses the provider default endpoint",
            "",
        )
    return CheckResult("llm", "pass", f"provider {provider!r}, model {model}")


def _database_url(env: Mapping[str, str]) -> str:
    return (
        (env.get("VECTOR_DB_DSN") or "").strip()
        or (env.get("CODE_REVIEW_DATABASE_URL") or "").strip()
        or (env.get("DATABASE_URL") or "").strip()
        or (env.get("POSTGRES_URL") or "").strip()
    )


def check_vectordb(env: Mapping[str, str]) -> CheckResult:
    dsn = _database_url(env)
    if not dsn:
        return CheckResult(
            "vectordb",
            "warn",
            "no PostgreSQL DSN; using memory/SQLite fallback",
            "Set VECTOR_DB_DSN for persistent pgvector retrieval.",
        )
    try:
        host, port = _split_host_port(dsn, 5432)
    except ValueError as exc:
        return CheckResult("vectordb", "fail", str(exc), "Fix VECTOR_DB_DSN.")
    if not _tcp_reachable(host, port):
        return CheckResult(
            "vectordb",
            "fail",
            f"{_safe_url(dsn)} is unreachable",
            "Start the pgvector service with docker compose.",
        )

    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                row = cur.fetchone()
    except Exception as exc:
        return CheckResult(
            "vectordb",
            "fail",
            f"{_safe_url(dsn)}: {type(exc).__name__}: {exc}",
            "Verify PostgreSQL credentials and database availability.",
        )
    if not row:
        return CheckResult(
            "vectordb",
            "fail",
            "pgvector extension is not installed",
            "Use the pgvector/pgvector:pg16 image or run CREATE EXTENSION vector.",
        )
    return CheckResult(
        "vectordb",
        "pass",
        f"{_safe_url(dsn)} pgvector={row[0]}",
    )


def check_embeddings(env: Mapping[str, str]) -> CheckResult:
    api_key = (
        (env.get("EMBEDDING_API_KEY") or "").strip()
        or (env.get("CODE_REVIEW_EMBEDDING_API_KEY") or "").strip()
        or (env.get("OPENAI_API_KEY") or "").strip()
    )
    base_url = (
        (env.get("EMBEDDING_BASE_URL") or "").strip()
        or (env.get("CODE_REVIEW_EMBEDDING_BASE_URL") or "").strip()
        or (env.get("OPENAI_BASE_URL") or "").strip()
    )
    model = (
        (env.get("EMBEDDING_MODEL") or "").strip()
        or (env.get("CODE_REVIEW_EMBEDDING_MODEL") or "").strip()
    )
    if not api_key:
        return CheckResult(
            "embeddings",
            "fail",
            "embedding API key is empty",
            "Set EMBEDDING_API_KEY (use any non-empty local value for Ollama).",
        )
    if not model:
        return CheckResult(
            "embeddings",
            "fail",
            "embedding model is empty",
            "Set EMBEDDING_MODEL=nomic-embed-text:latest.",
        )

    provider = (env.get("LLM_PROVIDER") or "").strip().lower()
    if provider in {"local", "ollama"} or "localhost" in base_url or "127.0.0.1" in base_url:
        try:
            names = _model_names(_ollama_tags(base_url or "http://localhost:11434/v1"))
        except Exception as exc:
            return CheckResult(
                "embeddings",
                "fail",
                f"{base_url}: {type(exc).__name__}: {exc}",
                "Start Ollama before enabling semantic retrieval.",
            )
        if model not in names:
            return CheckResult(
                "embeddings",
                "fail",
                f"embedding model {model!r} is not installed",
                f"Run `ollama pull {model}`.",
            )
    return CheckResult("embeddings", "pass", model)


def run_checks(root: Path = PROJECT_ROOT) -> list[CheckResult]:
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env", override=False)
    except Exception:
        pass

    env = os.environ
    return [
        check_python(),
        check_env_file(root),
        check_auth(env),
        check_port(env),
        check_redis(env),
        check_llm(env),
        check_vectordb(env),
        check_embeddings(env),
    ]


def exit_code(checks: list[CheckResult], *, strict: bool = False) -> int:
    if any(check.status == "fail" for check in checks):
        return 1
    if strict and any(check.status == "warn" for check in checks):
        return 1
    return 0


def _print_human(checks: list[CheckResult]) -> None:
    for check in checks:
        marker = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[check.status]
        print(f"[{marker}] {check.name}: {check.detail}")
        if check.action:
            print(f"       -> {check.action}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose the local moa-gateway runtime before startup.",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as failures",
    )
    args = parser.parse_args(argv)

    checks = run_checks()
    if args.json:
        print(json.dumps([asdict(check) for check in checks], ensure_ascii=False, indent=2))
    else:
        _print_human(checks)
    return exit_code(checks, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
