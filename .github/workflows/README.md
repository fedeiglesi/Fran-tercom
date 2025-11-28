# GitHub Actions - Workflows

## 🧪 Tests Workflow

Este workflow ejecuta automáticamente los tests de pytest cuando:
- Se hace push a las ramas: `main`, `Fran-3.15`, o cualquier rama que empiece con `claude/`
- Se crea un Pull Request hacia `main`
- Se ejecuta manualmente desde la pestaña "Actions" en GitHub

### Configuración Requerida

Para que los tests funcionen correctamente, necesitas configurar el siguiente secreto en tu repositorio:

1. Ve a **Settings** → **Secrets and variables** → **Actions**
2. Crea un nuevo secreto llamado `OPENAI_API_KEY`
3. Pega tu API key de OpenAI

### Ejecución Manual

Para ejecutar los tests manualmente:
1. Ve a la pestaña **Actions** en GitHub
2. Selecciona el workflow **"Tests"**
3. Haz clic en **"Run workflow"**
4. Selecciona la rama y confirma

### Estado de los Tests

Puedes agregar este badge a tu README.md para mostrar el estado de los tests:

```markdown
![Tests](https://github.com/fedeiglesi/Fran-tercom/workflows/Tests/badge.svg)
```

### Archivos de Prueba

Los tests se ejecutan desde el directorio `tests/`:
- `test_multi_intent.py` - Pruebas de detección de múltiples intenciones
- `test_conversation_flow.py` - Pruebas del flujo de conversación
- `test_observability.py` - Pruebas de observabilidad y circuit breaker
