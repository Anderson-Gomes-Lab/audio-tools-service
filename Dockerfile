# On part d'une image Python légère et officielle
FROM python:3.10-slim

# On installe FFmpeg, l'outil ultime pour l'audio/vidéo
# (celui de Debian lit l'AV1 des chasses : il est construit avec --enable-libdav1d)
RUN apt-get update && apt-get install -y ffmpeg

# On définit le dossier de travail à l'intérieur du conteneur
WORKDIR /app

# On installe les dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# On copie le code de notre application, sa consigne et sa police
COPY *.py consigne.md ./
COPY polices/ polices/

# On expose le port sur lequel notre service va écouter
EXPOSE 5000

# La commande pour démarrer notre service
CMD ["python", "app.py"]
