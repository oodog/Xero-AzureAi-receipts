#!/usr/bin/env bash
set -euo pipefail

# Example sanity deploy script placeholder – adjust to your CI/CD or Azure Web App container deploy
# Fail fast if required files don’t exist
for f in requirements.txt Procfile; do
  [[ -f "$f" ]] || { echo "Missing $f"; exit 1; }
done

echo "Build environment looks OK. Push with your preferred method (az webapp up / docker / GH Actions)."
