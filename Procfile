web: gunicorn main:app –bind 0.0.0.0:$PORT –workers 1 –threads 4 –timeout 600 –graceful-timeout 60 –keep-alive 5 –log-level info –preload
