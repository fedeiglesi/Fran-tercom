# Análisis del catálogo `catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv`

## Cobertura de datos
- Total de filas: 6.940 productos.
- Precios: 3.178 filas sin valor en dólares y 3.762 sin valor en pesos; ninguna fila carece de ambos precios.
- Familias: 1.040 familias distintas; las más frecuentes son PISTON KIT (426), ARO (356), JUNTA MOTOR (203), JUNTA (130) y PUÑOS (126).
- Normalización: todas las filas incluyen `descripcion_normalizada` y `sinonimos`; hay 2.974 filas sin `cilindrada`.

## Ajustes realizados en el código
- Se aprovecha `descripcion_normalizada` como nombre principal cuando está disponible, guardando el nombre original en `raw_name`.
- Se agrega `cilindrada` a las señales de búsqueda dentro de `search_text`.
- Se siguen usando familias, marca, modelo, categoría, sinónimos y proveedor para enriquecer el texto indexado.

Estos cambios garantizan que los campos ya presentes en el CSV se incorporen al índice FAISS/BM25 utilizado por las búsquedas híbridas de la app.
