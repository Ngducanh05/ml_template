FROM python:3.12.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements-runtime.txt ./
RUN python -m pip install --no-cache-dir --requirement requirements-runtime.txt \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /nonexistent app

COPY --chown=app:app src/ ./src/
COPY --chown=app:app artifacts/model.joblib ./artifacts/model.joblib

USER app

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "model_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
