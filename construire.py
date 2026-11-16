#!/usr/bin/env python3
"""Construit le site du répertoire à partir du dossier Drive des chefs de chœur.

    python3 construire.py lister        lit le Drive et écrit manifest.json
    python3 construire.py manquants     dit combien de fichiers restent à convertir
    python3 construire.py construire    écrit le site dans _site/

Le Drive se lit avec les variables DRIVE_API_KEY et DRIVE_FOLDER_ID.
Pour essayer sans Drive, sur un dossier de l'ordinateur :
    python3 construire.py lister --local CHEMIN_DU_DOSSIER
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RACINE = Path(__file__).resolve().parent
CACHE = RACINE / ".cache"
SORTIE = RACINE / "_site"
MANIFESTE = RACINE / "manifest.json"
WEB = RACINE / "web"
CONFIG = RACINE / "site.json"

# À augmenter pour reconvertir tous les fichiers (si la façon de convertir change).
VERSION_CONVERSION = "1"

# Une vidéo (répétition filmée, fichier .mpeg) donne sa piste son.
EXT_AUDIO = {".mp3", ".mpeg", ".mpga", ".mpg", ".m4a", ".aac", ".opus", ".ogg", ".oga", ".wav", ".flac", ".amr",
             ".wma", ".webm", ".mp4", ".m4v", ".mov", ".3gp", ".aif", ".aiff", ".caf"}
EXT_IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
EXT_PDF = {".pdf"}
IGNORES = {".ds_store", "desktop.ini", "thumbs.db"}

NOMS_VOIX = {"sop": "Sopranos", "alt": "Altos", "ten": "Ténors", "bas": "Basses",
             "tut": "Tutti", "acc": "Accompagnement"}
ORDRE_VOIX = ["sop", "alt", "ten", "bas", "tut", "acc", "autre"]
MOTIFS_VOIX = [
    ("sop", r"\bsop"),                        # sop, soprano(s), soprani, soprane
    ("alt", r"alt[oi]s?\b|\balt\b"),          # alt, alto(s), alti, contralto
    ("ten", r"\btenor"),                      # ténor(s), tenori
    ("bas", r"\bbass[eiao]?s?\b|\bbaryton"),  # bass, basse(s), bassi, baryton
]
LETTRES = {"s": "sop", "a": "alt", "t": "ten", "b": "bas"}

DRIVE_API = "https://www.googleapis.com/drive/v3/files"
DRIVE_DOSSIER = "application/vnd.google-apps.folder"
DRIVE_RACCOURCI = "application/vnd.google-apps.shortcut"
DRIVE_CHAMPS = "id,name,mimeType,modifiedTime,md5Checksum,shortcutDetails"


# ---------------------------------------------------------------- noms

def simplifier(texte: str) -> str:
    """Minuscules, sans accents ni ponctuation : « Ténors_2 » → « tenors 2 »."""
    t = unicodedata.normalize("NFKD", texte)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower().replace("œ", "oe").replace("æ", "ae")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def slug(texte: str) -> str:
    return re.sub(r"\s+", "-", simplifier(texte)) or "x"


def nom_sans_copie(nom: str) -> str:
    """Retire l'extension et le « (1) » qu'ajoutent les navigateurs aux fichiers en double."""
    return re.sub(r"\s*\(\d+\)\s*$", "", Path(nom).stem)


def nom_affiche(nom: str) -> str:
    """Nom d'une piste sans voix reconnue : « AUD-20261115-WA0001 » devient « Audio du 15/11 »."""
    base = nom_sans_copie(nom)
    m = re.fullmatch(r"(?:AUD|PTT)-\d{4}(\d{2})(\d{2})-WA\d+", base)
    if m:
        return f"Audio du {m.group(2)}/{m.group(1)}"
    return base.replace("_", " ").strip()


def classer_audio(nom: str) -> tuple[str, int | None]:
    """Devine la voix d'un audio d'après son nom : (« sop » | « alt » | … | « autre », numéro)."""
    base = simplifier(nom_sans_copie(nom))
    m = re.fullmatch(r"([satb])\s*([1-4])?", base)
    if m:
        return LETTRES[m.group(1)], int(m.group(2)) if m.group(2) else None
    trouvees = [v for v, motif in MOTIFS_VOIX if re.search(motif, base)]
    if len(trouvees) == 1:
        num = re.search(r"(?:^|\s)([1-4])(?:\s|$)", base)
        return trouvees[0], int(num.group(1)) if num else None
    if not trouvees:
        if re.search(r"\btutti\b|\bensemble\b|\btoutes? (les )?voix\b|\btous\b|\bchoeur complet\b|\bsatb\b", base):
            return "tut", None
        if re.search(r"\bpiano\b|\baccomp|\bplayback\b|\binstru", base):
            return "acc", None
    return "autre", None


def lire_nom_dossier(nom: str) -> tuple[int | None, str, str]:
    """« 01 – Gluck – Chœur infernal » → (1, « Gluck », « Chœur infernal »)."""
    ordre = None
    m = re.match(r"\s*(\d{1,3})\s*[.)_\-–—]?\s+(?=\S)", nom)
    if m:
        ordre = int(m.group(1))
        nom = nom[m.end():]
    parties = [p.strip() for p in re.split(r"\s+[-–—]\s+|\s*[–—]\s*", nom) if p.strip()]
    if not parties:
        return ordre, "", nom.strip()
    return ordre, " · ".join(parties[:-1]), parties[-1]


def type_fichier(f: dict) -> str | None:
    ext = Path(f["nom"]).suffix.lower()
    mime = f.get("mime") or ""
    if ext in EXT_PDF or mime == "application/pdf":
        return "pdf"
    if ext in EXT_AUDIO or mime.startswith(("audio/", "video/")):
        return "audio"
    if ext in EXT_IMAGE or mime.startswith("image/"):
        return "image"
    if mime.startswith("application/vnd.google-apps"):
        return "google"
    return None


# ---------------------------------------------------------------- lecture du Drive

def drive_lire(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        if e.code in (403, 404):
            sys.exit(
                f"Le Drive refuse l'accès ({e.code}). Vérifier que le dossier est partagé "
                f"« Tous les utilisateurs disposant du lien » et que la clé DRIVE_API_KEY autorise "
                f"Google Drive API.\n{detail}"
            )
        sys.exit(f"Le Drive a répondu {e.code}.\n{detail}")


def drive_enfants(dossier: str, cle: str):
    jeton = None
    while True:
        params = {
            "q": f"'{dossier}' in parents and trashed = false",
            "fields": f"nextPageToken,files({DRIVE_CHAMPS})",
            "pageSize": "1000",
            "orderBy": "name",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "key": cle,
        }
        if jeton:
            params["pageToken"] = jeton
        data = json.loads(drive_lire(DRIVE_API + "?" + urllib.parse.urlencode(params)))
        for f in data.get("files", []):
            yield drive_resoudre(f, cle)
        jeton = data.get("nextPageToken")
        if not jeton:
            return


def drive_resoudre(f: dict, cle: str) -> dict:
    """Un raccourci Drive devient le fichier qu'il désigne, sous le nom du raccourci."""
    if f.get("mimeType") != DRIVE_RACCOURCI:
        return f
    cible = (f.get("shortcutDetails") or {}).get("targetId")
    if not cible:
        return f
    params = urllib.parse.urlencode({"fields": DRIVE_CHAMPS, "supportsAllDrives": "true", "key": cle})
    vrai = json.loads(drive_lire(f"{DRIVE_API}/{cible}?{params}"))
    vrai["name"] = f["name"]
    return vrai


def drive_fichiers(dossier: str, cle: str, prefixe: str = "", profondeur: int = 0):
    for f in drive_enfants(dossier, cle):
        if f["mimeType"] == DRIVE_DOSSIER:
            if profondeur < 3:
                yield from drive_fichiers(f["id"], cle, prefixe + f["name"] + "/", profondeur + 1)
            continue
        yield {
            "id": f["id"],
            "nom": f["name"],
            "chemin": prefixe + f["name"],
            "mime": f.get("mimeType", ""),
            "modifie": f.get("modifiedTime", ""),
            "empreinte": f.get("md5Checksum") or f.get("modifiedTime", ""),
        }


def lister_drive(racine: str, cle: str) -> dict:
    chants, alertes = [], []
    for f in drive_enfants(racine, cle):
        if f["mimeType"] == DRIVE_DOSSIER:
            chants.append({"id": f["id"], "nom": f["name"], "fichiers": list(drive_fichiers(f["id"], cle))})
        else:
            alertes.append({"chant": "", "fichier": f["name"],
                            "raison": "Posé hors du dossier d'un chant : rangez-le dans le dossier de son chant."})
    return {"chants": chants, "alertes": alertes}


def lister_local(racine: Path) -> dict:
    chants, alertes = [], []
    for d in sorted(racine.iterdir()):
        if d.name.startswith(".") or d.name.lower() in IGNORES:
            continue
        if not d.is_dir():
            alertes.append({"chant": "", "fichier": d.name,
                            "raison": "Posé hors du dossier d'un chant : rangez-le dans le dossier de son chant."})
            continue
        fichiers = []
        for p in sorted(d.rglob("*")):
            if not p.is_file() or any(x.startswith(".") for x in p.relative_to(d).parts):
                continue
            rel = p.relative_to(racine).as_posix()
            fichiers.append({
                "id": hashlib.sha1(rel.encode()).hexdigest()[:16],
                "nom": p.name,
                "chemin": p.relative_to(d).as_posix(),
                "mime": "",
                "modifie": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "empreinte": hashlib.sha1(p.read_bytes()).hexdigest(),
                "local": str(p),
            })
        chants.append({"id": hashlib.sha1(d.name.encode()).hexdigest()[:16], "nom": d.name, "fichiers": fichiers})
    return {"chants": chants, "alertes": alertes}


# ---------------------------------------------------------------- conversion

def cle_cache(f: dict) -> str:
    return hashlib.sha1(f"{VERSION_CONVERSION}:{f['id']}:{f['empreinte']}".encode()).hexdigest()[:16]


def obtenir(f: dict, dest: Path, cle_api: str | None) -> None:
    if f.get("local"):
        shutil.copyfile(f["local"], dest)
        return
    params = urllib.parse.urlencode({"alt": "media", "supportsAllDrives": "true", "key": cle_api or ""})
    try:
        with urllib.request.urlopen(f"{DRIVE_API}/{f['id']}?{params}", timeout=300) as r, open(dest, "wb") as out:
            shutil.copyfileobj(r, out)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"téléchargement refusé par le Drive ({e.code})") from e


def lancer(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} : {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def dimensions(p: Path) -> tuple[int, int]:
    out = lancer(["ffprobe", "-v", "error", "-select_streams", "v:0",
                  "-show_entries", "stream=width,height", "-of", "csv=p=0", str(p)])
    w, h = out.strip().split(",")[:2]
    return int(w), int(h)


def convertir(f: dict, cle_api: str | None) -> dict:
    """Convertit un fichier une seule fois : le résultat reste dans .cache/ d'une construction à l'autre."""
    dossier = CACHE / cle_cache(f)
    meta_path = dossier / "meta.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    tmp = dossier.with_name(dossier.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    ext = Path(f["nom"]).suffix.lower()
    source = tmp / ("source" + ext)
    obtenir(f, source, cle_api)
    genre = type_fichier(f)
    if genre == "audio":
        lancer(["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-vn",
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100",
                "-codec:a", "libmp3lame", "-b:a", "96k", str(tmp / "piste.mp3")])
        duree = float(lancer(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(tmp / "piste.mp3")]).strip() or 0)
        meta = {"type": "audio", "fichier": "piste.mp3", "duree": round(duree, 1)}
    elif genre == "pdf":
        shutil.copyfile(source, tmp / "partition.pdf")
        lancer(["pdftoppm", "-png", "-scale-to-x", "1300", "-scale-to-y", "-1", str(source), str(tmp / "page")])
        pages = sorted(tmp.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1]))
        if not pages:
            raise RuntimeError("PDF sans page lisible")
        meta = {"type": "pdf", "pdf": "partition.pdf",
                "pages": [{"fichier": p.name, "l": dimensions(p)[0], "h": dimensions(p)[1]} for p in pages]}
    elif genre == "image":
        sortie = tmp / ("image.png" if ext == ".png" else "image.jpg")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-frames:v", "1",
               "-vf", "scale='min(1400,iw)':-2"]
        if sortie.suffix == ".jpg":
            cmd += ["-q:v", "3"]
        lancer(cmd + [str(sortie)])
        l, h = dimensions(sortie)
        meta = {"type": "image", "fichier": sortie.name, "l": l, "h": h}
    else:
        raise RuntimeError("type de fichier non pris en charge")
    source.unlink()
    (tmp / "meta.json").write_text(json.dumps(meta))
    shutil.rmtree(dossier, ignore_errors=True)
    tmp.rename(dossier)
    return meta


# ---------------------------------------------------------------- construction

def a_convertir(manifeste: dict) -> list[dict]:
    return [f for c in manifeste["chants"] for f in c["fichiers"]
            if type_fichier(f) in ("audio", "pdf", "image") and not (CACHE / cle_cache(f) / "meta.json").exists()]


def garder_le_plus_recent(fichiers: list[dict]) -> list[dict]:
    """Deux fichiers du même nom (un audio redéposé) : seul le plus récent compte."""
    retenus: dict[tuple, dict] = {}
    for f in fichiers:
        cle = (type_fichier(f), simplifier(nom_sans_copie(f["nom"])))
        if cle not in retenus or f["modifie"] > retenus[cle]["modifie"]:
            retenus[cle] = f
    return sorted(retenus.values(), key=lambda f: simplifier(f["chemin"]))


def construire(manifeste: dict, cle_api: str | None) -> dict:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    alertes = list(manifeste.get("alertes", []))
    utilises: set[str] = set()
    chants, slugs = [], set()
    shutil.rmtree(SORTIE, ignore_errors=True)
    (SORTIE / "m").mkdir(parents=True)

    for c in manifeste["chants"]:
        ordre, groupe, titre = lire_nom_dossier(c["nom"])
        s = slug(f"{groupe} {titre}")
        if s in slugs:
            s = f"{s}-{c['id'][:6].lower()}"
        slugs.add(s)
        dest = SORTIE / "m" / s
        pistes, pdfs, images, dates = [], [], [], []

        for f in c["fichiers"]:
            if f["nom"].lower() in IGNORES or f["nom"].startswith("."):
                continue
            genre = type_fichier(f)
            if genre == "google":
                alertes.append({"chant": c["nom"], "fichier": f["chemin"],
                                "raison": "Document Google : déposez plutôt un PDF."})
            elif genre is None:
                alertes.append({"chant": c["nom"], "fichier": f["chemin"],
                                "raison": "Type de fichier non lu : déposez un PDF, une photo ou un audio."})

        for f in garder_le_plus_recent([f for f in c["fichiers"] if type_fichier(f) in ("audio", "pdf", "image")]):
            try:
                meta = convertir(f, cle_api)
            except Exception as e:  # un fichier illisible ne bloque pas le reste du site
                alertes.append({"chant": c["nom"], "fichier": f["chemin"], "raison": f"Illisible ({e})."})
                continue
            cle = cle_cache(f)
            utilises.add(cle)
            dates.append(f["modifie"])
            dest.mkdir(parents=True, exist_ok=True)
            source = CACHE / cle
            if meta["type"] == "audio":
                voix, num = classer_audio(f["nom"])
                nom = NOMS_VOIX[voix] + (f" {num}" if num else "") if voix != "autre" else nom_affiche(f["nom"])
                fichier = f"{slug(nom)}-{cle[:8]}.mp3"
                shutil.copyfile(source / meta["fichier"], dest / fichier)
                pistes.append({"voix": voix, "num": num or 0, "nom": nom, "src": f"m/{s}/{fichier}", "duree": meta["duree"]})
            elif meta["type"] == "pdf":
                base = f"{slug(nom_sans_copie(f['nom']))}-{cle[:8]}"
                shutil.copyfile(source / meta["pdf"], dest / f"{base}.pdf")
                pages = []
                for i, p in enumerate(meta["pages"], 1):
                    shutil.copyfile(source / p["fichier"], dest / f"{base}-{i}.png")
                    pages.append({"src": f"m/{s}/{base}-{i}.png", "l": p["l"], "h": p["h"]})
                pdfs.append({"nom": nom_sans_copie(f["nom"]), "pdf": f"m/{s}/{base}.pdf", "pages": pages})
            else:
                fichier = f"{slug(nom_sans_copie(f['nom']))}-{cle[:8]}{Path(meta['fichier']).suffix}"
                shutil.copyfile(source / meta["fichier"], dest / fichier)
                images.append({"src": f"m/{s}/{fichier}", "l": meta["l"], "h": meta["h"]})

        if not pistes and not pdfs and not images:
            continue
        pistes.sort(key=lambda p: (ORDRE_VOIX.index(p["voix"]), p["num"], simplifier(p["nom"])))
        vus: dict[str, int] = {}
        for p in pistes:  # deux pistes au même nom : « Basses », « Basses (2) »
            vus[p["nom"]] = vus.get(p["nom"], 0) + 1
            if vus[p["nom"]] > 1:
                p["nom"] = f"{p['nom']} ({vus[p['nom']]})"
            del p["num"]
        partitions = pdfs + ([{"nom": "Partition", "pdf": "", "pages": images}] if images else [])
        chants.append({"slug": s, "ordre": ordre, "groupe": groupe, "titre": titre,
                       "maj": max(dates), "pistes": pistes, "partitions": partitions})

    chants.sort(key=lambda c: (c["ordre"] is None, c["ordre"] or 0, not c["groupe"], simplifier(c["groupe"]), simplifier(c["titre"])))
    donnees = {
        "nom": config["nom"],
        "sous_titre": config.get("sous_titre", ""),
        "nouveau_jours": config.get("nouveau_jours", 10),
        "maj": max((c["maj"] for c in chants), default=""),
        "chants": chants,
        "alertes": alertes,
    }
    json_txt = json.dumps(donnees, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    page = (WEB / "index.html").read_text(encoding="utf-8")
    page = page.replace("__TITRE__", html.escape(config["nom"])).replace("__DONNEES__", json_txt.replace("</", "<\\/"))
    (SORTIE / "index.html").write_text(page, encoding="utf-8")
    shutil.copytree(WEB / "polices", SORTIE / "polices")
    (SORTIE / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    (SORTIE / "version.txt").write_text(hashlib.sha256(page.encode()).hexdigest() + "\n")

    if CACHE.exists():  # le cache ne garde que ce que le site utilise
        for d in CACHE.iterdir():
            if d.name not in utilises:
                shutil.rmtree(d, ignore_errors=True)
    return donnees


def resumer(donnees: dict) -> str:
    lignes = [f"{len(donnees['chants'])} chant(s) en ligne."]
    for c in donnees["chants"]:
        voix = ", ".join(p["nom"] for p in c["pistes"]) or "aucune piste"
        nb_pages = sum(len(p["pages"]) for p in c["partitions"])
        lignes.append(f"- {(c['groupe'] + ' – ') if c['groupe'] else ''}{c['titre']} : {voix} ; partition : {nb_pages} page(s)")
    if donnees["alertes"]:
        lignes.append(f"\n{len(donnees['alertes'])} fichier(s) non lu(s) :")
        lignes += [f"- {a['chant'] + ' / ' if a['chant'] else ''}{a['fichier']} : {a['raison']}" for a in donnees["alertes"]]
    return "\n".join(lignes)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("etape", choices=["lister", "manquants", "construire"])
    ap.add_argument("--local", type=Path, help="lire un dossier de l'ordinateur au lieu du Drive")
    args = ap.parse_args()
    cle = os.environ.get("DRIVE_API_KEY")

    if args.etape == "lister":
        if args.local:
            manifeste = lister_local(args.local)
        else:
            dossier = os.environ.get("DRIVE_FOLDER_ID", "").strip()
            if not cle or not dossier:
                sys.exit("Il manque DRIVE_API_KEY ou DRIVE_FOLDER_ID (secrets du dépôt GitHub).")
            manifeste = lister_drive(dossier, cle)
        MANIFESTE.write_text(json.dumps(manifeste, ensure_ascii=False, indent=1, sort_keys=True))
        n = sum(len(c["fichiers"]) for c in manifeste["chants"])
        print(f"{len(manifeste['chants'])} dossier(s) de chant, {n} fichier(s).")
        return

    manifeste = json.loads(MANIFESTE.read_text())
    if args.etape == "manquants":
        n = len(a_convertir(manifeste))
        print(f"{n} fichier(s) à convertir.")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as out:
                out.write(f"nombre={n}\n")
        return

    donnees = construire(manifeste, cle)
    texte = resumer(donnees)
    print(texte)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as out:
            out.write(texte + "\n")


if __name__ == "__main__":
    main()
