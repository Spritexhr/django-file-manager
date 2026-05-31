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

# Build static assets bundle. DEBUG=true avoids needing a real SECRET_KEY at
# build time; the bundle is content-hashed by ManifestStaticFilesStorage and
# served at runtime by whitenoise.
RUN DJANGO_DEBUG=true python manage.py collectstatic --noinput

# Pre-create runtime data directories so they exist even before the first
# volume mount + migrate.
RUN mkdir -p db_data media

EXPOSE 8000

CMD ["gunicorn", "file_manager_project.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
