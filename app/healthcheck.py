"""Deployment healthcheck — run on the TARGET machine before first use.

Verifies, without writing anything, that the three things a local deployment
needs are actually reachable:
  1. Config loads (all required env vars present).
  2. The configured model provider is ready:
       - ollama  -> the Ollama server is up and OLLAMA_MODEL is pulled.
       - bedrock -> AWS region is set (model access is verified separately).
       - stub    -> always ready.
  3. Supabase is reachable (a read-only SELECT on public.projects).

Usage:
    python -m app.healthcheck

Exit code 0 if everything passes, 1 otherwise — so it can gate a deploy script.
"""
from __future__ import annotations

import json
import sys
import urllib.request


def _ok(msg: str) -> None:
    print(f"  [PASS] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def check_config() -> tuple[bool, object]:
    try:
        from app.config import get_settings

        settings = get_settings()
        _ok(f"config loaded (model_provider={settings.model_provider})")
        return True, settings
    except Exception as e:  # noqa: BLE001 - healthcheck reports, doesn't raise
        _fail(f"config failed to load: {e}")
        return False, None


def check_provider(settings) -> bool:
    provider = settings.model_provider
    if provider == "stub":
        _ok("stub provider — always ready")
        return True

    if provider == "ollama":
        try:
            url = f"{settings.ollama_base_url.rstrip('/')}/api/tags"
            with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - local, trusted URL
                tags = json.loads(resp.read())
            names = {m.get("name", "") for m in tags.get("models", [])}
            # Ollama tags are like "qwen3:8b"; accept an exact or name-prefix match.
            wanted = settings.ollama_model
            if wanted in names or any(n.split(":")[0] == wanted.split(":")[0] for n in names):
                _ok(f"Ollama up at {settings.ollama_base_url}; model '{wanted}' available")
                return True
            _fail(f"Ollama up but model '{wanted}' not pulled. Run: ollama pull {wanted}")
            return False
        except Exception as e:  # noqa: BLE001
            _fail(f"Ollama not reachable at {settings.ollama_base_url}: {e}. Is `ollama serve` running?")
            return False

    if provider == "bedrock":
        if settings.aws_region:
            _ok(f"bedrock provider — region={settings.aws_region} (verify model access separately)")
            return True
        _fail("bedrock provider but AWS_REGION is empty")
        return False

    _fail(f"unknown model_provider: {provider}")
    return False


def check_supabase() -> bool:
    try:
        from app.db.client import get_supabase

        # Read-only: never writes. Confirms creds + connectivity to the CRM DB.
        get_supabase().table("projects").select("id").limit(1).execute()
        _ok("Supabase reachable (read on public.projects)")
        return True
    except Exception as e:  # noqa: BLE001
        _fail(f"Supabase not reachable: {e}")
        return False


def main() -> int:
    print("Oblivion agent layer — deployment healthcheck\n")
    ok_config, settings = check_config()
    if not ok_config:
        return 1

    results = [check_provider(settings), check_supabase()]
    print()
    if all(results):
        print("All checks passed — the local deployment is ready.")
        return 0
    print("Some checks failed — see [FAIL] lines above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
