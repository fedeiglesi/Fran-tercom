web: gunicorn fran_v4.api:app --worker-class uvicorn.workers.UvicornWorker --bind=0.0.0.0:$PORT --workers=1 --timeout=300 --graceful-timeout=120 --preload
