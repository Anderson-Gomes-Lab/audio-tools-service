# audio-tools

Petit service Flask + ffmpeg, déployé par Coolify (branche `main`, Dockerfile). Il partage le volume `/media` avec n8n.
Un seul processus : le verrou des tâches et leur état en mémoire tiennent dans ce processus (pas de gunicorn à plusieurs workers).

## Usages
- `/split`, `/duration` : transcription audio (workflow n8n « Convertisseur Audio to Text », actif). Ouvertes sans jeton. Ne pas les casser.
- `/vertical` : clip 9:16 simple.
- `/shorts/<video_id>/<n>` : téléchargement d'un Short par n8n, qui ne voit pas `/media`.
- Chaîne des clips (`docs/contrat-chaine.md`, seule référence) : `/sante`, `/fetch`, `/analyse`, `/monter`, `/nettoyer`, `/taches/<id>`.

## Fichiers
- `app.py` : routes et sécurité (jeton, chemins limités à `/media`, `video_id` vérifié).
- `taches.py` : une tâche à la fois, état écrit de façon atomique, filtre des secrets dans les erreurs.
- `fetch.py` : téléchargement par Apify, `.part` renommé une fois vérifié.
- `analyse.py`, `tri.py`, `consigne.md` : choix des moments par Gemini.
- `montage.py` : coupe, image carrée sur fond noir, son, incrustation ; porte les réglages communs avec `/vertical`.
- `texte.py` : PNG des textes (Pillow), zone haute de l'écran.
- `polices/` : Inter ExtraBold (licence OFL), figée depuis la police variable.
- `tests/` : pytest, vidéos de synthèse fabriquées par ffmpeg, Apify et Gemini simulés.
- `.claude/verifier.sh` : batterie (`complet` en fin de lot).

## Variables d'environnement
`API_TOKEN` (routes de clips, sans lui tout est refusé), `APIFY_TOKEN`, `GEMINI_API_KEY`, `MEDIA_DIR` (facultative, `/media` par défaut).

## Mesures (31/10/2026)
Complexité max 15 (ruff C901), tenue. Fichiers ≤ 400 lignes ; le plus gros, `app.py`, en fait 391 : la route `/vertical`, que la chaîne n'appelle pas, est la première à retirer. 253 tests.
Rendu d'un Short de 30 s depuis un AV1 1080p : environ 16 s sur le Mac de développement, à remesurer sur le VPS. Coupe exacte à l'image près, -14,5 LUFS.
AV1 : le ffmpeg de Debian (bookworm et trixie) est construit avec `--enable-libdav1d` (vérifié dans `debian/rules`) ; image Docker non construite, pas de Docker sur le poste.
