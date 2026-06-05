# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Local dev runs inside a conda env named `file_manager_project_env` (Django, Pillow,
gunicorn, whitenoise are installed there — the conda `base` env has no Django).
Activate it before any `manage.py`/python command:

```bash
conda activate file_manager_project_env
```

```bash
# Local development (requires .env with DJANGO_DEBUG=true)
python manage.py runserver

# Apply migrations
python manage.py migrate

# Create superuser
python manage.py createsuperuser

# Collect static files (needed before production deploy)
python manage.py collectstatic --noinput

# Reset all uploaded files and DB records (dev utility)
python clear_data.py

# Docker (production-like)
docker compose up --build -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

## Environment

Copy `.env.example` to `.env`. For local dev, the minimum required:

```
DJANGO_DEBUG=true
# SECRET_KEY is auto-generated in DEBUG mode, so optional locally
```

`DJANGO_SECRET_KEY` is **required** when `DEBUG=False`.

## Architecture

Single Django app (`core`) with no external database dependency — SQLite only, stored at `db.sqlite3` locally or at `DJANGO_DB_PATH` in Docker.

**Models** (`core/models.py`):
- `Folder` — self-referential FK on `parent` for arbitrary nesting; scoped per `created_by` user.
- `File` — FK to `Folder` (nullable = root level); files stored at `media/user_<id>/<filename>`.

**Views** (`core/views.py`): All views are function-based and `@login_required`. The `file_manager` view handles both GET (render) and POST (upload / create-folder) in one function, dispatching on `request.POST` keys. Delete views (`delete_file`, `delete_folder`, `bulk_delete`) are `@require_POST` and return `JsonResponse` when called with `X-Requested-With: XMLHttpRequest`, otherwise redirect.

**URL structure** (`core/urls.py`):
- `/` → root file manager
- `/folder/<id>/` → folder view (same view, different `folder_id`)
- `/delete/file/<id>/`, `/delete/folder/<id>/`, `/delete/bulk/` → POST-only

Auth (`/login/`, `/logout/`) is handled by Django's built-in auth views, not in `core`.

**File validation** (`core/forms.py`): Extension whitelist in `ALLOWED_EXTENSIONS`. SVG and HTML are intentionally excluded to prevent script injection via `MEDIA_URL`. Upload limits are env-configurable in `settings.py` (`MAX_UPLOAD_FILE_SIZE`, `MAX_UPLOAD_BATCH_SIZE`, `MAX_UPLOAD_FILES`) and re-read by the form for friendly errors. Defaults: 5GB per file, 20GB per batch, 20 files per request. Tune via `DJANGO_MAX_UPLOAD_FILE_MB` / `DJANGO_MAX_UPLOAD_BATCH_MB` / `DJANGO_MAX_UPLOAD_FILES`. **The reverse proxy (nginx) sets the real ceiling** — large uploads also need `client_max_body_size` raised there (see `.env.example`), and gunicorn runs with `--timeout 1800` so multi-GB transfers aren't killed.

**User management** (`core/views.py`): Staff-only (`@staff_required`, gated on `user.is_staff`). The `user_management` view renders `core/user_management.html` (a Vue app); `user_create`, `user_set_password`, `user_toggle_active`, `user_delete` are `@require_POST` and redirect back with messages. Guards: nobody can delete/disable themselves, only a superuser can act on a superuser or grant the staff flag, and superuser accounts can't be deleted from the UI (use Django admin). The entry point is a "用户管理" link in the header user menu, shown only to staff.

**Static files**: WhiteNoise serves compressed/hashed static assets. `STATICFILES_DIRS` points to `static/`; `STATIC_ROOT` is `staticfiles/` (built at image build time). Do not edit files in `staticfiles/` — run `collectstatic` instead.

**Security hardening**: Production mode (`DEBUG=False`) enables HSTS, secure cookies, SSL redirect, and proxy header trust. These are all toggled in `settings.py` based on env vars — do not hardcode them.

**Deployment**: Docker Compose with two named volumes (`db_data`, `media`) for persistence. Gunicorn with 3 workers. Migrations must be run manually after first `docker compose up`.
