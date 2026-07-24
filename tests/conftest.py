"""Test-wide setup.

Settings requires Supabase env vars to instantiate. Tests never connect to
Supabase — they only need `get_settings()` to build a valid Settings object
(e.g. to read judge_max_rounds). Set harmless dummy values here, before any
test imports trigger `get_settings()`, and force the model provider to stub.
"""
import os

os.environ.setdefault("MODEL_PROVIDER", "stub")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("SUPABASE_DB_URI", "postgresql://postgres:test@localhost:5432/postgres")
