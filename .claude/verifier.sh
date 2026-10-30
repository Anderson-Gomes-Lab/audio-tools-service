#!/bin/bash
# Batterie du dépôt. Sans argument : tests et lint. "complet" : ajoute la taille des fichiers.
set -e
cd "$(dirname "$0")/.."
[ -d .venv ] || { uv venv -q --python 3.12 .venv; uv pip install -q --python .venv/bin/python -r requirements-dev.txt; }
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
if [ "$1" = complet ]; then
  trop=$(find . -name '*.py' -not -path './.venv/*' -exec wc -l {} + | awk '$2!="total" && $1>400')
  [ -z "$trop" ] || { echo "Fichier de plus de 400 lignes : $trop"; exit 1; }
fi
