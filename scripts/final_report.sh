#!/bin/bash
set -e
cd ~/projects/rpgllm
echo "===== getent hosts mraz.local ====="
getent hosts mraz.local
echo
echo "===== docker compose ps ====="
docker compose ps
echo
echo "===== curl -I http://mraz.local ====="
curl -sI http://mraz.local --max-time 10
echo
echo "===== curl -I http://mraz.local/admin/ ====="
curl -s -o /dev/null -w "admin HTTP %{http_code}\n" http://mraz.local/admin/ --max-time 10
echo
echo "===== seed_demo (idempotent) ====="
docker compose exec -T web python manage.py seed_demo 2>&1 | tail -6