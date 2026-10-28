from flask import Flask, request, jsonify
import subprocess
import hmac
import math
import os
import re

app = Flask(__name__)

# On définit le dossier de base où se trouvent nos fichiers
# Ce sera le point de montage de notre volume partagé
BASE_DIR = os.environ.get("MEDIA_DIR", "/media")

# Jeton d'accès facultatif : s'il est défini dans les variables d'environnement
# (Coolify), chaque appel doit envoyer l'en-tête "Authorization: Bearer <jeton>"
API_TOKEN = os.environ.get("API_TOKEN")


@app.before_request
def verifier_jeton():
    recu = request.headers.get("Authorization", "")
    if API_TOKEN and not hmac.compare_digest(recu.encode(), f"Bearer {API_TOKEN}".encode()):
        return jsonify({"error": "Unauthorized"}), 401


def chemin_dans_media(relative_path):
    """Chemin absolu du fichier, ou None s'il sort de /media (ou n'est pas un texte).

    os.path.join accepte "../" et les chemins absolus : sans cette vérification,
    "../etc/passwd" ou "/etc/passwd" donnerait accès à tout le conteneur.
    """
    if not isinstance(relative_path, str):
        return None
    base = os.path.realpath(BASE_DIR)
    chemin = os.path.realpath(os.path.join(base, relative_path))
    if os.path.commonpath([base, chemin]) != base:
        return None
    return chemin


@app.route('/split', methods=['POST'])
def split_audio():
    data = request.get_json()
    if not data or 'file_path' not in data:
        return jsonify({"error": "Missing file_path"}), 400

    # On construit le chemin complet et sécurisé du fichier
    input_file = chemin_dans_media(data['file_path'])
    if input_file is None:
        return jsonify({"error": "file_path must stay inside /media"}), 400

    if not os.path.exists(input_file):
        return jsonify({"error": f"File not found: {input_file}"}), 404

    output_dir = os.path.dirname(input_file)
    output_pattern = os.path.join(output_dir, "chunk_%03d.mp3")

    command = [
        'ffmpeg',
        '-i', input_file,
        '-f', 'segment',
        '-segment_time', '300',
        '-c', 'copy',
        output_pattern
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        return jsonify({"status": "success", "message": f"File split into chunks in {output_dir}"}), 200
    except subprocess.CalledProcessError as e:
        return jsonify({"error": "FFmpeg command failed", "details": e.stderr}), 500

@app.route('/duration', methods=['POST'])
def get_duration():
    data = request.get_json()
    if not data or 'file_path' not in data:
        return jsonify({"error": "Missing file_path"}), 400

    input_file = chemin_dans_media(data['file_path'])
    if input_file is None:
        return jsonify({"error": "file_path must stay inside /media"}), 400

    if not os.path.exists(input_file):
        return jsonify({"error": f"File not found: {input_file}"}), 404

    # La commande ffprobe pour obtenir la durée en secondes
    command = [
        'ffprobe',
        '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        input_file
    ]

    try:
        # On exécute la commande et on récupère la sortie
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        duration = float(result.stdout.strip())
        return jsonify({"duration_seconds": duration}), 200
    except (subprocess.CalledProcessError, ValueError) as e:
        return jsonify({"error": "Failed to get duration", "details": str(e)}), 500


def nombre(valeur, minimum):
    """Nombre fini >= minimum, sinon None (texte, null, NaN, négatif...)."""
    if isinstance(valeur, bool):
        return None
    try:
        x = float(valeur)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x >= minimum else None


def duree_video(chemin):
    sortie = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', chemin],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    return float(sortie.strip())


def a_du_son(chemin):
    sortie = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index',
         '-of', 'csv=p=0', chemin],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    return bool(sortie.strip())


def debut_avant_pic(chemin, longueur, avance=2):
    """Début (s) qui place le moment le plus fort du son à `avance` s du début.

    On mesure le volume de chaque seconde ; le premier pic (à 0,5 dB près) est
    en général l'impact, le rugissement ou le coup de grâce.
    """
    sortie = subprocess.run(
        ['ffmpeg', '-hide_banner', '-nostats', '-i', chemin, '-vn',
         '-af', 'aresample=48000,asetnsamples=n=48000,astats=metadata=1:reset=1,'
                'ametadata=print:key=lavfi.astats.Overall.RMS_level',
         '-f', 'null', '-'],
        capture_output=True, text=True, timeout=300,
    ).stderr
    volumes = [-120.0 if v == '-inf' else float(v)
               for v in re.findall(r'RMS_level=(-?[\d.]+|-inf)', sortie)]
    if len(volumes) <= longueur:
        return 0
    seuil = max(volumes) - 0.5
    pic = next(i for i, v in enumerate(volumes) if v >= seuil)
    return max(0, min(pic - avance, len(volumes) - longueur))


# Les deux mises en page verticales 1080x1920
FILTRES_VERTICAUX = {
    # Image entière au centre, sur un fond fait de la même vidéo agrandie, floutée et assombrie
    "blur": (
        "[0:v]split=2[f][g];"
        "[f]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "boxblur=25:2,eq=brightness=-0.3[fond];"
        "[g]scale=1080:-2[jeu];"
        "[fond][jeu]overlay=0:(H-h)/2,setsar=1,format=yuv420p[v]"
    ),
    # Découpe d'une bande verticale au centre de l'image, agrandie en plein écran
    "crop": "[0:v]crop=ih*9/16:ih,scale=1080:1920,setsar=1,format=yuv420p[v]",
}


@app.route('/vertical', methods=['POST'])
def vertical():
    """Clip 16:9 -> vidéo verticale 1080x1920 prête pour Shorts, Reels et TikTok.

    JSON attendu :
      file_path    (obligatoire) chemin dans /media
      mode         "blur" (défaut) ou "crop"
      duration     durée de l'extrait en secondes (défaut 15)
      start        début imposé en secondes ; sinon calé 2 s avant le pic sonore
      output_path  chemin de sortie dans /media (défaut : <nom>_vertical_<mode>.mp4)
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or 'file_path' not in data:
        return jsonify({"error": "Missing file_path"}), 400

    input_file = chemin_dans_media(data['file_path'])
    if input_file is None:
        return jsonify({"error": "file_path must be a path inside /media"}), 400
    if not os.path.isfile(input_file):
        return jsonify({"error": f"File not found: {data['file_path']}"}), 404

    mode = data.get('mode', 'blur')
    if mode not in FILTRES_VERTICAUX:
        return jsonify({"error": "mode must be 'blur' or 'crop'"}), 400

    duree_voulue = nombre(data.get('duration', 15), 1)
    if duree_voulue is None:
        return jsonify({"error": "duration must be a number >= 1"}), 400
    debut_impose = None
    if data.get('start') is not None:
        debut_impose = nombre(data['start'], 0)
        if debut_impose is None:
            return jsonify({"error": "start must be a number >= 0"}), 400

    nom = os.path.splitext(data['file_path'])[0]
    output_file = chemin_dans_media(data.get('output_path') or f"{nom}_vertical_{mode}.mp4")
    if output_file is None:
        return jsonify({"error": "output_path must stay inside /media"}), 400

    try:
        total = duree_video(input_file)
        son = a_du_son(input_file)
        if debut_impose is not None:
            debut = debut_impose
            if debut >= total - 1:
                return jsonify({"error": f"start outside the clip ({total:.0f} s)"}), 400
        else:
            debut = debut_avant_pic(input_file, int(min(duree_voulue, total))) if son else 0
        longueur = min(duree_voulue, total - debut)

        command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                   '-ss', str(debut), '-t', str(longueur), '-i', input_file]
        if son:
            # Son du jeu gardé, volume harmonisé à -14 LUFS (niveau des plateformes)
            # (loudnorm travaille en 192 kHz : on revient à 48 kHz, la norme vidéo)
            audio = ";[0:a]loudnorm=I=-14:TP=-1.5,aresample=48000[a]"
        else:
            # Clip muet : piste silencieuse, certaines plateformes refusent une vidéo sans son
            command += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
            audio = ";[1:a]anull[a]"
        command += [
            '-filter_complex', FILTRES_VERTICAUX[mode] + audio,
            '-map', '[v]', '-map', '[a]', '-t', str(longueur),
            '-r', '30', '-c:v', 'libx264', '-preset', 'medium', '-crf', '21',
            '-c:a', 'aac', '-b:a', '160k', '-movflags', '+faststart', output_file,
        ]
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=600)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as e:
        # Pas de fichier à moitié écrit qui traînerait dans /media
        if os.path.exists(output_file):
            os.remove(output_file)
        details = getattr(e, 'stderr', None) or str(e)
        return jsonify({"error": "Vertical conversion failed",
                        "details": details.replace(os.path.realpath(BASE_DIR), "/media")}), 500

    return jsonify({
        "status": "success",
        "output_path": os.path.relpath(output_file, os.path.realpath(BASE_DIR)),
        "start": debut,
        "duration": longueur,
        "mode": mode,
    }), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
