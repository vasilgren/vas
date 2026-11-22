FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BOT_RESULTS_DIR=/data BOT_NO_BROWSER=1
COPY bot.py legacy_v1.py v2_engine.py cloud_app.py ./
# Railway volume is mounted at /data at runtime. Python standard library only.
EXPOSE 8080
CMD ["python", "cloud_app.py"]
