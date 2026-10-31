# audio-tools

Petit service Flask + ffmpeg, déployé par Coolify (branche `main`, Dockerfile). Il partage le volume `/media` avec n8n.
Un seul processus : le verrou des tâches et leur état en mémoire tiennent dans ce processus (pas de gunicorn à plusieurs workers).

## Usages
- `/split`, `/duration` : transcription audio (workflow n8n « Convertisseur Audio to Text », actif). Ouvertes sans jeton. Ne pas les casser.
- `/shorts/<video_id>/<n>` : téléchargement d'un Short par n8n, qui ne voit pas `/media`.
- `/liens`, `/p/<jeton>` : lien public de 24 h pour Buffer, qui refuse Drive et n'a pas le jeton. n8n envoie le Short récupéré dans Drive ; `/p/<jeton>` est ouverte sans Bearer et rend 404 sans dire pourquoi.
- Chaîne des clips (`docs/contrat-chaine.md`, seule référence) : `/sante`, `/fetch`, `/analyse`, `/contexte`, `/lectures/<video_id>`, `/monter`, `/nettoyer`, `/taches/<id>`.

## Fichiers
- `app.py` : routes et sécurité (jeton, chemins limités à `/media`, `video_id` vérifié).
- `taches.py` : une tâche à la fois, état écrit de façon atomique, filtre des secrets dans les erreurs.
- `fetch.py` : téléchargement par Apify, `.part` renommé une fois vérifié.
- `lecture.py` : la chaîne de lecture, à la suite, une tâche à la fois (mesure, identification, passages, hors combat, synthèse), et la 2e passe. Écrit dans `/media/lectures`, jamais effacé par `/nettoyer`.
- `reprise.py` : ce qui survit à une panne. Le relevé `<video_id>.releve.json` garde le coût de tous les essais, la part déjà annoncée et chaque réponse payée ; il n'est clos qu'une fois tout annoncé ; les notes `<video_id>.note.txt` ne partent qu'une fois jouées et écrites.
- `mesure_ecran.py` : relevés de l'écran sans IA (ffmpeg, numpy, Pillow), image par image. Exige la 1080p. `lecture.py` garde ses mesures dans `lectures/<video_id>.mesures.json` et ne donne à chaque passage que les faits de sa fenêtre. Sa signature `signes/sortir-l-arme.png` vit dans `lecture/`.
- `lecture/` : ce que Gemini reçoit (fiches `connaissance/`, 5 consignes, 2 lectures d'Anderson en exemples). Copiés dans l'image par le Dockerfile, sinon `mesure_ecran` plante. Copie de `docs/recherche/selection-clips/` : à recopier si le réglage change. Pas de `__init__.py` : `import lecture` doit rester `lecture.py`.
- `analyse.py` : appels à Gemini, gardes de coût, relances réseau, fichier distant périmé à 46 h. Porte aussi `_preciser` et `_passe_2`, sans appelant : ils reviennent au lot 2, avec `tri.py`, `consigne.md` et le plan.
- `tarifs.py` : modèles, tarifs, limite de dépense par vidéo.
- `montage.py` : coupe, image carrée sur fond noir, son, incrustation.
- `liens.py` : liens publics dans `/media/liens`. Jeton aléatoire, jamais tiré du `video_id`, écriture atomique, 300 Mo au plus. L'échéance est la date du fichier plus 24 h ; les expirés partent à chaque `POST /liens`, `/nettoyer` n'y touche pas.
- `texte.py` : PNG des textes (Pillow), zone haute de l'écran.
- `polices/` : Inter ExtraBold (licence OFL), figée depuis la police variable.
- `tests/` : pytest, vidéos de synthèse fabriquées par ffmpeg, Apify et Gemini simulés.
- `.claude/verifier.sh` : batterie (`complet` en fin de lot).

## Variables d'environnement
`API_TOKEN` (routes de clips, sans lui tout est refusé), `APIFY_TOKEN`, `GEMINI_API_KEY`, `MEDIA_DIR` (facultative, `/media` par défaut), `URL_PUBLIQUE` (facultative : adresse des liens publics, celle du service par défaut), `PLAFOND_GEMINI_MOIS_USD` (facultative : sans elle, le compteur du mois compte sans bloquer).

## Mesures (31/10/2026)
Complexité max 15 (ruff C901), tenue. Fichiers ≤ 400 lignes ; le plus gros du service, `analyse.py`, en fait 386, et `app.py` 309 depuis le retrait de `/vertical`.
Mesure de l'écran sur 30 min : 116 s sur Mac, environ 220 Mo, à refaire sur le VPS.
Rendu d'un Short de 30 s depuis un AV1 1080p : environ 16 s sur le Mac de développement, à remesurer sur le VPS. Coupe exacte à l'image près, -14,5 LUFS.
AV1 : le ffmpeg de Debian (bookworm et trixie) est construit avec `--enable-libdav1d` (vérifié dans `debian/rules`) ; image Docker non construite, pas de Docker sur le poste.
