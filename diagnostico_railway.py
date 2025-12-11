#!/usr/bin/env python3
"""Script de diagnóstico para problemas de Railway + PostgreSQL + pgvector.

Ejecuta este script para identificar automáticamente problemas de configuración:

    python diagnostico_railway.py

Si estás en Railway:
    railway run python diagnostico_railway.py
"""
import asyncio
import os
import sys
from typing import List, Tuple


class Diagnostico:
    def __init__(self):
        self.errores: List[str] = []
        self.advertencias: List[str] = []
        self.exitos: List[str] = []

    def print_header(self, text: str):
        print(f"\n{'=' * 70}")
        print(f"  {text}")
        print(f"{'=' * 70}\n")

    def check_env_var(self, name: str, required: bool = True) -> str | None:
        """Verifica que una variable de entorno exista."""
        value = os.getenv(name)
        if not value:
            if required:
                self.errores.append(f"❌ Variable {name} no está configurada")
            else:
                self.advertencias.append(f"⚠️  Variable {name} no está configurada (opcional)")
            return None
        else:
            # Ocultar valores sensibles
            if "KEY" in name or "PASSWORD" in name:
                display_value = value[:8] + "..." if len(value) > 8 else "***"
            else:
                display_value = value[:50] + "..." if len(value) > 50 else value
            self.exitos.append(f"✅ {name}={display_value}")
            return value

    async def test_database_connection(self, database_url: str) -> bool:
        """Intenta conectar a PostgreSQL."""
        try:
            import asyncpg

            # Normalizar URL
            url = database_url.replace("postgresql+asyncpg://", "postgresql://")
            url = url.replace("postgres://", "postgresql://")

            conn = await asyncpg.connect(url, timeout=10)
            version = await conn.fetchval("SELECT version()")
            await conn.close()

            self.exitos.append(f"✅ Conexión exitosa a PostgreSQL")
            self.exitos.append(f"   Versión: {version[:50]}...")
            return True

        except ImportError:
            self.errores.append("❌ Librería 'asyncpg' no instalada. Instala con: pip install asyncpg")
            return False
        except Exception as e:
            self.errores.append(f"❌ Error al conectar a PostgreSQL: {e}")
            return False

    async def test_pgvector_extension(self, database_url: str) -> bool:
        """Verifica si pgvector está instalado y activo."""
        try:
            import asyncpg

            url = database_url.replace("postgresql+asyncpg://", "postgresql://")
            url = url.replace("postgres://", "postgresql://")

            conn = await asyncpg.connect(url, timeout=10)

            # Verificar si la extensión está disponible
            result = await conn.fetch(
                "SELECT * FROM pg_available_extensions WHERE name = 'vector'"
            )

            if not result:
                self.errores.append("❌ Extensión 'vector' NO está disponible en esta BD")
                self.errores.append("   💡 Solución: Usa Supabase o el template de Railway con pgvector")
                await conn.close()
                return False

            # Verificar si está activa
            result = await conn.fetch("SELECT * FROM pg_extension WHERE extname = 'vector'")

            if not result:
                self.advertencias.append("⚠️  Extensión 'vector' disponible pero NO activada")
                self.advertencias.append("   💡 Actívala con: CREATE EXTENSION IF NOT EXISTS vector;")
                await conn.close()
                return False

            version = result[0]["extversion"]
            self.exitos.append(f"✅ pgvector instalado y activo (versión {version})")
            await conn.close()
            return True

        except Exception as e:
            self.errores.append(f"❌ Error al verificar pgvector: {e}")
            return False

    async def test_table_exists(self, database_url: str, table_name: str) -> bool:
        """Verifica si una tabla existe."""
        try:
            import asyncpg

            url = database_url.replace("postgresql+asyncpg://", "postgresql://")
            url = url.replace("postgres://", "postgresql://")

            conn = await asyncpg.connect(url, timeout=10)
            result = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = $1
                )
                """,
                table_name,
            )
            await conn.close()

            if result:
                self.exitos.append(f"✅ Tabla '{table_name}' existe")
                return True
            else:
                self.advertencias.append(f"⚠️  Tabla '{table_name}' NO existe")
                return False

        except Exception as e:
            self.errores.append(f"❌ Error al verificar tabla '{table_name}': {e}")
            return False

    async def count_products(self, database_url: str) -> int:
        """Cuenta productos en la tabla."""
        try:
            import asyncpg

            url = database_url.replace("postgresql+asyncpg://", "postgresql://")
            url = url.replace("postgres://", "postgresql://")

            conn = await asyncpg.connect(url, timeout=10)
            count = await conn.fetchval("SELECT COUNT(*) FROM products")
            await conn.close()

            if count > 0:
                self.exitos.append(f"✅ Tabla 'products' tiene {count} productos")
            else:
                self.advertencias.append("⚠️  Tabla 'products' está vacía (0 productos)")
                self.advertencias.append("   💡 Ejecuta: railway run python -m fran_v4.catalog_to_postgres ...")

            return count

        except Exception as e:
            self.errores.append(f"❌ Error al contar productos: {e}")
            return 0

    async def test_openai_api(self, api_key: str) -> bool:
        """Verifica que la API key de OpenAI funcione."""
        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key, timeout=10.0)
            response = await client.embeddings.create(
                model="text-embedding-3-small", input="test"
            )

            if response.data and len(response.data) > 0:
                self.exitos.append("✅ OpenAI API key válida y funcionando")
                return True
            else:
                self.errores.append("❌ OpenAI API key no retorna embeddings")
                return False

        except ImportError:
            self.errores.append("❌ Librería 'openai' no instalada. Instala con: pip install openai")
            return False
        except Exception as e:
            self.errores.append(f"❌ Error con OpenAI API: {e}")
            return False

    def print_summary(self):
        """Imprime resumen de diagnóstico."""
        print("\n" + "=" * 70)
        print("  RESUMEN DE DIAGNÓSTICO")
        print("=" * 70 + "\n")

        if self.exitos:
            print("✅ ÉXITOS:")
            for exito in self.exitos:
                print(f"   {exito}")
            print()

        if self.advertencias:
            print("⚠️  ADVERTENCIAS:")
            for advertencia in self.advertencias:
                print(f"   {advertencia}")
            print()

        if self.errores:
            print("❌ ERRORES CRÍTICOS:")
            for error in self.errores:
                print(f"   {error}")
            print()

        print("-" * 70)

        if not self.errores:
            print("\n🎉 ¡TODO ESTÁ CONFIGURADO CORRECTAMENTE!\n")
            return 0
        else:
            print(f"\n⚠️  Se encontraron {len(self.errores)} errores críticos.\n")
            print("📖 Consulta la guía completa en: RAILWAY_SETUP_GUIA_COMPLETA.md\n")
            return 1


async def main():
    diag = Diagnostico()

    diag.print_header("DIAGNÓSTICO DE CONFIGURACIÓN - Fran 4.0")

    # Paso 1: Variables de entorno
    diag.print_header("1. Verificando Variables de Entorno")

    database_url = diag.check_env_var("DATABASE_URL", required=True)
    openai_key = diag.check_env_var("OPENAI_API_KEY", required=True)
    diag.check_env_var("MODEL_NAME", required=False)
    diag.check_env_var("OPENAI_EMBEDDING_MODEL", required=False)
    catalogo_url = diag.check_env_var("CATALOGO_CSV_URL", required=False)

    if not database_url:
        diag.errores.append("   💡 Configura DATABASE_URL en Railway variables")
        diag.print_summary()
        return 1

    # Paso 2: Conexión a PostgreSQL
    diag.print_header("2. Probando Conexión a PostgreSQL")
    db_ok = await diag.test_database_connection(database_url)

    if not db_ok:
        diag.errores.append("   💡 Verifica que DATABASE_URL sea correcta")
        diag.errores.append("   💡 Verifica que PostgreSQL esté corriendo en Railway")
        diag.print_summary()
        return 1

    # Paso 3: pgvector
    diag.print_header("3. Verificando Extensión pgvector")
    await diag.test_pgvector_extension(database_url)

    # Paso 4: Tablas
    diag.print_header("4. Verificando Tablas")
    products_exists = await diag.test_table_exists(database_url, "products")

    if products_exists:
        await diag.count_products(database_url)

    await diag.test_table_exists(database_url, "conversation_events")
    await diag.test_table_exists(database_url, "carts")
    await diag.test_table_exists(database_url, "session_messages")

    # Paso 5: OpenAI API
    if openai_key:
        diag.print_header("5. Verificando OpenAI API")
        await diag.test_openai_api(openai_key)

    # Paso 6: Catálogo CSV
    diag.print_header("6. Verificando Catálogo CSV")
    if catalogo_url:
        if catalogo_url.startswith("http"):
            try:
                from urllib.request import urlopen

                response = urlopen(catalogo_url, timeout=10)
                if response.status == 200:
                    diag.exitos.append(f"✅ CATALOGO_CSV_URL es accesible")
                else:
                    diag.errores.append(f"❌ CATALOGO_CSV_URL retorna status {response.status}")
            except Exception as e:
                diag.errores.append(f"❌ Error al acceder a CATALOGO_CSV_URL: {e}")
        else:
            from pathlib import Path

            path = Path(catalogo_url)
            if path.exists():
                diag.exitos.append(f"✅ Catálogo local existe: {catalogo_url}")
            else:
                diag.errores.append(f"❌ Catálogo local no encontrado: {catalogo_url}")

    # Resumen final
    return diag.print_summary()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
