# audio-tools

Petit service Flask + ffmpeg, déployé par Coolify (branche `main`, Dockerfile). Il partage le volume `/media` avec n8n.

## Usages
- `/split`, `/duration` : transcription audio (workflow n8n « Convertisseur Audio to Text », actif). Ne pas les casser.
- `/vertical` : clip 9:16 simple.

## Fichiers
- `app.py` : routes et sécurité (jeton, chemins limités à `/media`).
- `tests/` : pytest, vidéos de synthèse fabriquées par ffmpeg.
- `.claude/verifier.sh` : batterie (`complet` en fin de lot).

## Mesures (30/10/2026)
6 + 1 tests. Complexité max 15 (ruff C901). Fichiers ≤ 400 lignes.
