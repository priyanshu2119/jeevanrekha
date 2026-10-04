# syntax=docker/dockerfile:1
# JeevanRekha production image.
# Official FastAPI deployment pattern: build from the plain Python image
# (the old tiangolo/uvicorn-gunicorn-fastapi base is deprecated), exec-form
# CMD so SIGTERM shuts workers down gracefully (lifespan events fire), and
# --proxy-headers because TLS terminates at the reverse proxy in front.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    JR_DATA_DIR=/code/data

WORKDIR /code

# Dependencies first -- this layer caches until requirements.txt changes.
COPY requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

COPY alembic.ini /code/alembic.ini
COPY alembic /code/alembic
COPY app /code/app
COPY scripts /code/scripts
COPY entrypoint.sh /code/entrypoint.sh

RUN useradd --create-home --uid 10001 jr \
    && mkdir -p /code/data \
    && chown -R jr:jr /code \
    && chmod +x /code/entrypoint.sh

USER jr
EXPOSE 8000

# entrypoint applies `alembic upgrade head` unless JR_SKIP_MIGRATIONS=1.
ENTRYPOINT ["/code/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--workers", "2"]
