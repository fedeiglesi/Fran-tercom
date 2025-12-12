web: gunicorn fran_v4.api:app --worker-class uvicorn.workers.UvicornWorker --bind=0.0.0.0:$PORT --workers=1 --timeout=300 --graceful-timeout=120 --preload
release: python -m fran_v4.catalog_to_postgres https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/Fran-4.0/catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv --table-name catalogo3 --drop-existing
