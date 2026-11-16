# Répertoire du chœur

Le site où les choristes écoutent leur voix, chant par chant : un lecteur par pupitre (reculer, ralentir,
répéter un passage) et la partition dessous. Il se lit sur téléphone, sans compte ni application.

Les chefs de chœur ne touchent jamais au site. Ils déposent les fichiers dans un dossier Drive, et le site
relit ce dossier toutes les heures. La page « Chef de chœur ? » du site leur explique comment faire.

## Comment ça marche

```
Dossier Drive des chefs ──(toutes les heures, GitHub Actions)──▶ construire.py ──▶ GitHub Pages
  un dossier par chant                                       audios → mp3 au même volume
  partition + un audio par voix                              PDF → une image par page
```

- `construire.py lister` lit le Drive (clé d'API et identifiant du dossier, dans les secrets du dépôt).
- `construire.py construire` convertit les nouveaux fichiers, puis écrit le site dans `_site/`.
  Un fichier déjà converti ne l'est plus : le résultat reste dans le cache de GitHub Actions.
- Le site n'est republié que si quelque chose a changé dans le Drive.
- La voix d'un audio se devine d'après son nom (« Choeur infernal altos.opus » → Altos) : les règles et
  leurs exemples sont dans `tests/test_noms.py`.
- Un fichier illisible n'empêche pas le reste de paraître : il est listé en bas de la page des chefs.

Le site n'est pas référencé par les moteurs de recherche : on y arrive par son adresse.

## Mise en place (une fois)

1. **Dossier Drive** (compte Google personnel) : créer « Répertoire du chœur », le partager
   « Tous les utilisateurs disposant du lien : lecteur », et y ajouter les chefs de chœur comme éditeurs.
   Son identifiant est la fin de son adresse, après `/folders/`.
2. **Clé d'API Google** : sur console.cloud.google.com, créer un projet, activer « Google Drive API »
   (API et services → Bibliothèque), puis créer une clé (API et services → Identifiants → Créer des
   identifiants → Clé API) et la restreindre à Google Drive API.
3. **Secrets du dépôt** (Settings → Secrets and variables → Actions → New repository secret) :
   `DRIVE_API_KEY` (la clé) et `DRIVE_FOLDER_ID` (l'identifiant du dossier).
4. **Pages** (Settings → Pages → Build and deployment → Source) : « GitHub Actions ».

Pour mettre le site à jour sans attendre l'heure : Actions → Site du répertoire → Run workflow.

## Essayer sur son ordinateur

Il faut Python 3, ffmpeg et poppler (`pdftoppm`).

```
python3 construire.py lister --local "/chemin/vers/Répertoire du chœur"
python3 construire.py construire
python3 -m http.server -d _site 8000
```

## Changer le nom du chœur

Dans `site.json` : `nom`, `sous_titre`, et `nouveau_jours` (combien de jours un chant reste marqué « Nouveau »).
