# Trading Journal API

Django 6.1 + DRF + PostgreSQL backend for a forex-first trading journal.

## Local setup (Windows / PowerShell)

```powershell
cd C:\Users\HP\Documents\Trading_journal
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env     # then edit values
python manage.py migrate
python manage.py seed_specs
python manage.py createsuperuser
python manage.py runserver
```

Run tests: `python manage.py test`

## Environment variables

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Django secret key |
| `DEBUG` | `True` locally, `False` in production |
| `ALLOWED_HOSTS` | Comma-separated hosts |
| `DATABASE_URL` | Postgres connection URL |
| `REDIS_URL` | Shared cache (optional locally) |
| `FRONTEND_URL` | Used in email links |
| `CORS_ALLOWED_ORIGINS` | Frontend origins |
| `RESEND_API_KEY`, `MAIL_*` | Email via Resend |
| `GOOGLE_CLIENT_IDS` | Google sign-in audiences |
| `STORAGE_BACKEND`, `S3_*` | Screenshot storage (Supabase S3) |
| `TWO_FACTOR_KEY` | Fernet key encrypting 2FA secrets |
| `ADMIN_URL` | Obscured admin path |

## API

All routes are under `/api/`. List them with
`python manage.py export_endpoints`. Auth is JWT (`Authorization: Bearer <access>`).
Errors are JSON with a machine-readable `code`. Money is always a 2-decimal string.

endpoint.json ists every method, path, auth level and summary, and it’s generated from the code, so it can’t drift. It doesn’t have request bodies or filter names, if needed that is where api-endpoints.json in the docs folder comes in but since it’s hand-assembled, treat the real code as the authority if the two ever disagree.