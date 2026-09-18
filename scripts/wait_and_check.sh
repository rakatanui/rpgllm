#!/bin/bash
set -e
cd ~/projects/rpgllm
for i in $(seq 1 10); do
  sleep 3
  st=$(docker compose ps litellm --format '{{.Status}}')
  echo "litellm: $st"
  if echo "$st" | grep -q healthy; then
    break
  fi
done
docker compose cp scripts/check_litellm_ollama.py litellm:/tmp/cl.py
docker compose exec -T litellm python /tmp/cl.py