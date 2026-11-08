#!/usr/bin/env bash
# Runs on Render every time the service is built
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate --no-input
python manage.py seed_specs   # the instrument table (safe to run again: it only updates)

# First deploy only: make an admin from environment variables, then delete those variables in the dashboard
if [[ -n "${DJANGO_SUPERUSER_USERNAME:-}" && -n "${DJANGO_SUPERUSER_EMAIL:-}" && -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]]; then
  python manage.py createsuperuser --noinput || echo "That admin already exists, skipping."
fi