from __future__ import annotations

from MCUM.core.minimax_credentials import resolve_minimax_credentials


def test_minimax_credentials_from_process_env_are_redacted(monkeypatch) -> None:
    monkeypatch.setenv("MINIMAX_API_KEY", "secret-process-token")
    monkeypatch.setenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
    monkeypatch.setenv("MINIMAX_MODEL", "MiniMax-M3")

    credential = resolve_minimax_credentials({})

    assert credential is not None
    assert credential.api_key == "secret-process-token"
    metadata = credential.redacted_metadata()
    assert metadata["source"] == "process_env"
    assert "api_key" not in metadata
    assert "secret-process-token" not in str(metadata)


def test_minimax_credentials_from_explicit_env_file(monkeypatch, tmp_path) -> None:
    for key in ("MINIMAX_API_KEY", "MINIMAX_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "MINIMAX_API_KEY=secret-file-token",
                "MINIMAX_BASE_URL=https://api.minimax.io/v1",
                "MINIMAX_MODEL=MiniMax-M3",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCUM_MINIMAX_ENV_PATH", str(env_path))

    credential = resolve_minimax_credentials({})

    assert credential is not None
    assert credential.api_key == "secret-file-token"
    assert credential.source.startswith("explicit_env:")
    assert "secret-file-token" not in str(credential.redacted_metadata())
