# Fran 4.0 - Core Module
# Sistema de asistente de ventas con OpenAI Assistants API

from openai import OpenAI
import os
from typing import List, Dict, Optional
import time


class Fran4:
    """Fran 4.0 - Asistente de ventas autónomo
    Usa OpenAI Assistants API con File Search para acceso a documentos
    """

    def __init__(self, api_key: Optional[str] = None):
        """Inicializa Fran con API key de OpenAI

        Args:
            api_key: OpenAI API key (si no se provee, lee de env)
        """
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY no encontrada")

        self.client = OpenAI(api_key=self.api_key)
        self.assistant_id = os.getenv("FRAN_ASSISTANT_ID")
        self.vector_store_id = os.getenv("FRAN_VECTOR_STORE_ID")

    def setup_knowledge_base(self, archivos: List[str]) -> str:
        """Crea Vector Store y sube archivos de conocimiento
        EJECUTAR UNA SOLA VEZ en setup inicial
        """
        print("📚 Creando base de conocimiento de Fran...")
        print(f"   Archivos a procesar: {len(archivos)}")

        for archivo in archivos:
            if not os.path.exists(archivo):
                raise FileNotFoundError(f"Archivo no encontrado: {archivo}")

        vector_store = self.client.beta.vector_stores.create(
            name="Fran 4.0 Knowledge Base",
            expires_after={
                "anchor": "last_active_at",
                "days": 365
            }
        )
        self.vector_store_id = vector_store.id
        print(f"✅ Vector Store creado: {self.vector_store_id}")

        file_streams = []
        try:
            file_streams = [open(path, "rb") for path in archivos]
            print("📤 Subiendo archivos...")

            file_batch = self.client.beta.vector_stores.file_batches.upload_and_poll(
                vector_store_id=self.vector_store_id,
                files=file_streams
            )

            print("✅ Archivos procesados:")
            print(f"   Completados: {file_batch.file_counts.completed}")
            print(f"   Fallidos: {file_batch.file_counts.failed}")
            print(f"   En proceso: {file_batch.file_counts.in_progress}")

            return self.vector_store_id

        finally:
            for stream in file_streams:
                try:
                    stream.close()
                except Exception:
                    pass

    def create_assistant(self, vector_store_id: Optional[str] = None) -> Dict:
        """Crea el asistente Fran con personalidad y acceso a archivos"""
        if vector_store_id:
            self.vector_store_id = vector_store_id
        if not self.vector_store_id:
            raise ValueError("vector_store_id no configurado")

        print("🤖 Creando asistente Fran 4.0...")

        assistant = self.client.beta.assistants.create(
            name="Fran 4.0 - Vendedor Tech",
            instructions=(
                "Sos Fran, vendedor experto en tecnología robótica e inteligencia artificial.\n\n"
                "PERSONALIDAD Y TONO:\n"
                "- Técnico pero accesible: explicás conceptos complejos de forma simple.\n"
                "- Proactivo y consultivo: anticipás necesidades y hacés preguntas inteligentes.\n"
                "- Profesional pero cercano: tono argentino/latino neutral.\n"
                "- Honesto y directo: si no sabés algo, lo decís.\n"
                "- Orientado a soluciones: siempre enfocado en resolver el problema del cliente.\n\n"
                "ARCHIVOS Y CONOCIMIENTO:\n"
                "- Catálogo completo de productos.\n"
                "- Playbook de ventas.\n"
                "- Casos de éxito.\n"
                "- Políticas comerciales.\n\n"
                "REGLAS:\n"
                "❌ No inventes precios ni características.\n"
                "✅ Si no sabés algo, decí 'Dejame verificar eso con el equipo'.\n\n"
                "PROCESO DE VENTA CONSULTIVO:\n"
                "1. Descubrir necesidades.\n"
                "2. Calificar al cliente.\n"
                "3. Educar con comparaciones y pros/contras.\n"
                "4. Recomendar soluciones óptimas.\n"
                "5. Manejar objeciones con empatía.\n"
                "6. Cerrar con un siguiente paso concreto.\n\n"
                "ADAPTACIÓN:\n"
                "- Cliente técnico: specs e integraciones.\n"
                "- Decision maker: ROI y valor de negocio.\n"
                "- Usuario final: facilidad de uso y soporte.\n"
                "- Primer contacto: educación antes de vender.\n"
            ),
            model="gpt-4o",
            tools=[{"type": "file_search"}],
            tool_resources={
                "file_search": {"vector_store_ids": [self.vector_store_id]}
            },
            temperature=0.7,
            top_p=0.95
        )

        self.assistant_id = assistant.id
        print("✅ Asistente creado exitosamente")
        print(f"   Assistant ID: {self.assistant_id}")
        print(f"   Modelo: {assistant.model}")
        print(f"   Vector Store: {self.vector_store_id}")

        return {
            "assistant_id": self.assistant_id,
            "vector_store_id": self.vector_store_id,
            "model": assistant.model,
            "name": assistant.name
        }

    def start_conversation(self) -> str:
        """Inicia una nueva conversación (thread)"""
        if not self.assistant_id:
            raise ValueError("Assistant no configurado. Ejecutá setup primero.")

        thread = self.client.beta.threads.create()
        print(f"💬 Nueva conversación iniciada: {thread.id}")
        return thread.id

    def send_message(self, mensaje: str, thread_id: str) -> str:
        """Envía mensaje a Fran y obtiene respuesta"""
        if not self.assistant_id:
            raise ValueError("Assistant no configurado")

        self.client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=mensaje
        )

        run = self.client.beta.threads.runs.create_and_poll(
            thread_id=thread_id,
            assistant_id=self.assistant_id,
            timeout=60
        )

        if run.status == "completed":
            messages = self.client.beta.threads.messages.list(
                thread_id=thread_id,
                order="desc",
                limit=1
            )
            if messages.data:
                msg = messages.data[0].content[0]
                if hasattr(msg, "text"):
                    return msg.text.value
            return "Error: No se pudo obtener respuesta"

        if run.status == "failed":
            return f"❌ Error en el asistente: {run.last_error}"

        if run.status == "expired":
            return "Error: La solicitud expiró. Intentá de nuevo."

        return f"Error: Estado inesperado ({run.status})"

    def get_conversation_history(self, thread_id: str, limit: int = 50) -> List[Dict]:
        """Obtiene historial de conversación"""
        messages = self.client.beta.threads.messages.list(
            thread_id=thread_id,
            order="asc",
            limit=limit
        )
        historial = []
        for msg in messages.data:
            content = ""
            if msg.content and len(msg.content) > 0:
                if hasattr(msg.content[0], "text"):
                    content = msg.content[0].text.value
            historial.append({
                "role": msg.role,
                "content": content,
                "timestamp": msg.created_at,
                "message_id": msg.id
            })
        return historial

    def update_knowledge_base(self, nuevos_archivos: List[str]) -> Dict:
        """Actualiza archivos en el vector store existente"""
        if not self.vector_store_id:
            raise ValueError("Vector store no configurado")

        print("🔄 Actualizando base de conocimiento...")

        file_streams = []
        try:
            file_streams = [open(path, "rb") for path in nuevos_archivos]

            file_batch = self.client.beta.vector_stores.file_batches.upload_and_poll(
                vector_store_id=self.vector_store_id,
                files=file_streams
            )

            resultado = {
                "completados": file_batch.file_counts.completed,
                "fallidos": file_batch.file_counts.failed,
                "total": len(nuevos_archivos)
            }

            print(f"✅ Actualización completa: {resultado}")
            return resultado

        finally:
            for stream in file_streams:
                try:
                    stream.close()
                except Exception:
                    pass

    def delete_assistant(self):
        """Elimina el asistente y vector store (irreversible)"""
        if self.assistant_id:
            try:
                self.client.beta.assistants.delete(self.assistant_id)
                print(f"🗑️  Asistente eliminado: {self.assistant_id}")
            except Exception as e:
                print(f"❌ Error eliminando asistente: {e}")

        if self.vector_store_id:
            try:
                self.client.beta.vector_stores.delete(self.vector_store_id)
                print(f"🗑️  Vector store eliminado: {self.vector_store_id}")
            except Exception as e:
                print(f"❌ Error eliminando vector store: {e}")

    def get_status(self) -> Dict:
        """Obtiene estado actual de Fran"""
        return {
            "assistant_id": self.assistant_id,
            "vector_store_id": self.vector_store_id,
            "api_key_configured": bool(self.api_key),
            "ready": bool(self.assistant_id and self.vector_store_id)
        }


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    fran = Fran4()
    status = fran.get_status()
    print("\n📊 Estado de Fran 4.0:")
    for key, value in status.items():
        print(f"   {key}: {value}")
