FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY manage.py ./
COPY file_manager_project/ ./file_manager_project/
COPY core/ ./core/
COPY static/ ./static/

# Build the same manifest-backed static bundle used at runtime. A build-only
# key satisfies production settings without copying the deployment secret into
# the image or build context.
RUN DJANGO_DEBUG=false \
    DJANGO_SECRET_KEY=build-only-static-collection-key-not-for-runtime \
    python manage.py collectstatic --noinput

# Pre-create runtime data directories so they exist even before the first
# volume mount + migrate.
RUN mkdir -p db_data media

EXPOSE 8000

# --timeout 1800: large uploads (multi-GB) can hold a sync worker busy well past
# gunicorn's default 30s; without this the worker is killed mid-transfer.
CMD ["gunicorn", "file_manager_project.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "1800", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
