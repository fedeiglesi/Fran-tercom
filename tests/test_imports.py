"""Test básico de imports para verificar que los módulos se cargan correctamente."""
import pytest


def test_config_imports():
    """Verifica que el módulo de configuración se puede importar."""
    from fran_v4 import config

    assert config.MODEL_NAME is not None
    assert config.DATABASE_URL is not None


def test_database_module_imports():
    """Verifica que el módulo de base de datos se puede importar."""
    from fran_v4 import database

    assert database.Database is not None
    assert database.metadata is not None


def test_api_module_imports():
    """Verifica que el módulo de API se puede importar."""
    from fran_v4 import api

    assert api is not None


def test_fix_database_url():
    """Verifica que la función _fix_database_url maneja correctamente sslmode."""
    from fran_v4.config import _fix_database_url

    # Test 1: Convierte postgresql:// a postgresql+asyncpg://
    url = "postgresql://user:pass@host:5432/db"
    fixed = _fix_database_url(url)
    assert fixed == "postgresql+asyncpg://user:pass@host:5432/db"

    # Test 2: Elimina sslmode de la URL
    url = "postgresql://user:pass@host:5432/db?sslmode=require"
    fixed = _fix_database_url(url)
    assert fixed == "postgresql+asyncpg://user:pass@host:5432/db"
    assert "sslmode" not in fixed

    # Test 3: No modifica URLs que ya tienen asyncpg
    url = "postgresql+asyncpg://user:pass@host:5432/db"
    fixed = _fix_database_url(url)
    assert fixed == "postgresql+asyncpg://user:pass@host:5432/db"

    # Test 4: Preserva otros parámetros mientras elimina sslmode
    url = "postgresql://user:pass@host:5432/db?sslmode=require&timeout=10"
    fixed = _fix_database_url(url)
    assert "sslmode" not in fixed
    assert "timeout=10" in fixed
