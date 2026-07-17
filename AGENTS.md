# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Commands

Local dev runs inside a conda env named `file_manager_env` (Django, Pillow,
gunicorn, whitenoise are installed there — the conda `base` env has no Django).
Activate it before any `manage.py`/python command:

```bash
conda activate file_manager_env
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

# Optional Samba sidecar (shares the same media volume)
docker compose --profile smb up --build -d
docker compose --profile smb exec samba testparm -s
docker compose --profile smb exec samba smbcontrol smbd ping

# Materialize legacy flat paths (preview first, then apply)
docker compose exec web python manage.py materialize_storage
docker compose exec web python manage.py materialize_storage --apply

# Reconcile filesystem changes made over Samba; --prune is destructive
docker compose exec web python manage.py sync_samba
docker compose exec web python manage.py sync_samba --prune
```

## Environment

Copy `.env.example` to `.env`. For local dev, the minimum required:

```
DJANGO_DEBUG=true
# SECRET_KEY is auto-generated in DEBUG mode, so optional locally
```

`DJANGO_SECRET_KEY` is **required** when `DEBUG=False`.

`DJANGO_MEDIA_ROOT` optionally overrides the local media directory.
`DJANGO_FILE_DOWNLOAD_CHUNK_KB` controls authenticated download streaming and
defaults to 1024 KiB. `DJANGO_FILESYSTEM_SYNC_ON_BROWSE=false` is the safe
default; enable it only when files written directly through the optional Samba
sidecar should be reconciled while a user browses their directory.

The Samba profile requires a separate `SAMBA_USER` and `SAMBA_PASSWORD` (at
least 12 characters). These are not Django credentials. See `docs/SMB.md` before
changing the loopback-only bind address.

## Architecture

Single Django app (`core`) with no external database dependency — SQLite only, stored at `db.sqlite3` locally or at `DJANGO_DB_PATH` in Docker.

**Storage and SMB**: Django always uses local `FileSystemStorage`, rooted at
`DJANGO_MEDIA_ROOT` (default `media/`). New uploads materialize the full Folder
ancestor tree under `user_<id>/`. Optional SMB access is provided by the `samba`
Compose profile, not by a Python SMB backend; both containers mount the same
`media` volume. One independent Samba account can see the entire physical
volume. Do not reuse Django passwords for Samba. Run `materialize_storage`
without flags before applying legacy path migration with `--apply`; reconcile
external changes with `sync_samba` and use `--prune` only intentionally. Full
setup and security constraints are documented in `docs/SMB.md`.

**Models** (`core/models.py`):
- `Folder` — self-referential FK on `parent` for arbitrary nesting; scoped per `created_by` user.
- `File` — FK to `Folder` (nullable = root level); new files materialize at
  `media/user_<id>/<Folder ancestor path>/<filename>`.

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

**Deployment**: Docker Compose uses two named volumes (`db_data`, `media`) for
persistence. Gunicorn runs with 3 workers. The optional `samba` service is gated
by the `smb` profile, shares `media`, and binds to `127.0.0.1:445` by default;
never expose TCP 445 publicly. Migrations run automatically in the current web
container command. Back up the database and media volumes together.
