# Estructura del catálogo `catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv`

- **Columnas presentes (13):** `codigo`, `descripcion`, `precio_dolares`, `precio_pesos`, `familia`, `familia_nombre`, `proveedor_nombre`, `marca_moto`, `modelo_moto`, `descripcion_normalizada`, `sinonimos`, `cilindrada` y `categoria_final`.
- **Filas totales y unicidad:** 6.940 productos con códigos únicos en todas las filas.
- **Precios:** 3.178 filas carecen de `precio_dolares` y 3.762 de `precio_pesos`, pero ninguna fila tiene ambos precios vacíos.
- **Familias y proveedores:** todas las filas incluyen `familia`, `familia_nombre` y `proveedor_nombre`, útiles para enriquecer las búsquedas.
- **Marca/Modelo de moto:** 435 filas no tienen valores en `marca_moto` ni `modelo_moto`; el resto aporta señales adicionales para filtrar por modelo o marca.
- **Texto de búsqueda:** `descripcion_normalizada` y `sinonimos` están completos en todas las filas; son los campos que `load_catalog_enriched()` usa como base del `search_text` junto con familia, marca, modelo, proveedor y categoría.
- **Cilindrada:** presente en 3.966 filas; 2.974 productos no especifican este dato, por lo que es una señal opcional.
- **Categorías finales:** 10 valores distintos. Predominan `Universal / Varios` (3.924 filas), seguidas de `Motor` (1.170), `Electrica` (561), `Transmision` (532) y `Opticas / Tablero / Iluminacion` (316).
- **Duplicados en texto:** hay 133 descripciones normalizadas repetidas (6.940 filas vs. 6.807 nombres normalizados únicos). Aunque pueden producir empates en las búsquedas, cada fila mantiene su `codigo` único.

En conjunto, el CSV está bien estructurado para los buscadores actuales: ofrece nombres normalizados y sinónimos completos, precios en al menos una moneda y metadatos de familia, proveedor y categoría que `load_catalog_enriched()` incorpora al índice FAISS/BM25. Las principales limitaciones son la falta de cilindrada en ~43% de los productos y la presencia de descripciones normalizadas duplicadas.
