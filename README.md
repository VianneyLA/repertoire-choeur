# Répertoire du chœur

Le site où les choristes travaillent leur voix, chant par chant, sur téléphone, sans compte ni application.
La page d'un chant montre la partition en plein écran ; tout ce qui sert à répéter tient dans la barre du bas :
la piste avec son volume (on y voit les respirations et les passages forts ; on la touche pour se placer, on
glisse dessus pour choisir un passage), la façon d'écouter (seule, en avant, tutti, sans moi), la vitesse et la
boucle, dont on marque le début puis la fin pendant l'écoute.

Quand un chant a plusieurs voix, on peut aussi les écouter ensemble : sa voix en avant, tout le chœur, ou
toutes les voix sauf la sienne, avec un volume par voix.

Les chefs de chœur ne touchent jamais au site. Ils déposent les fichiers dans un dossier Drive, et le site
relit ce dossier tous les quarts d'heure. La page « Chef de chœur ? » du site leur explique comment faire.

## Comment ça marche

```
Dossier Drive des chefs ──(toutes les heures, GitHub Actions)──▶ construire.py ──▶ GitHub Pages
  un dossier par chant                                       audios → mp3 au même volume
  partition + un audio par voix                              PDF → une image par page
```

- `construire.py lister` lit le Drive (clé d'API et identifiant du dossier, dans les secrets du dépôt).
- `construire.py construire` convertit les nouveaux fichiers, puis écrit le site dans `_site/`, avec pour chaque
  chant un fichier `volumes-….json` : le volume de chaque piste toutes les 100 ms, pour dessiner la piste.
  Un fichier déjà converti ne l'est plus : le résultat reste dans le cache de GitHub Actions.
- Le site n'est republié que si quelque chose a changé dans le Drive.
- La voix d'un audio se devine d'après son nom (« Choeur infernal altos.opus » → Altos) : les règles et
  leurs exemples sont dans `tests/test_noms.py`.
- Un fichier illisible n'empêche pas le reste de paraître : il est listé en bas de la page des chefs.

## Écoute à plusieurs voix

Les voix d'un chant, enregistrées séparément, ne partent pas au même moment (une annonce parlée avant la
première note) ni au même tempo. Pour les écouter ensemble, la construction les recale :

1. Elle repère dans chaque piste le début et la fin du chant : la première et la dernière note tenue
   (même hauteur au moins 0,3 s). Une annonce parlée ne tient pas sa hauteur, mais une note de départ
   chantée pendant l'annonce passe pour le début : un chef corrige alors le début à l'oreille. Ce qui précède
   le début est sauté à l'écoute, voix seule comme ensemble. (Deux autres critères, la continuité et la force
   du chant, ont été essayés sur de vraies pistes : l'annonce y passait pour du chant.)
2. Si le dossier du chant contient un document Google « Calage », ses repères remplacent ceux trouvés
   automatiquement. Il se remplit depuis la page « Caler les voix » du site (lien en bas de chaque chant).
   Des repères au milieu (une attaque commune) affinent le calage.
3. Entre deux repères, chaque piste est accélérée ou ralentie sans changer la hauteur (rubberband), pour que
   les repères de toutes les voix tombent au même moment. Les positions communes sont la médiane des voix.
4. Chaque piste recalée existe en trois vitesses (1, 0,75 et 0,5) ; le navigateur les décode et les lance
   au même instant (Web Audio), ce qui les garde ensemble. Changer de voix garde la position musicale.

Un morceau étiré de plus de 25 % est signalé sur la page des chefs : un repère est sans doute mal placé.
Des voix chantées sur le même métronome n'ont besoin d'aucun calage.

`outils/analyser_voix.py` (lancé à la main par Actions → « Analyser le calage des voix ») mesure le décalage
restant entre les pistes, déposées ou recalées (`--calees`).

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

Pour mettre le site à jour sans attendre : Actions → Site du répertoire → Run workflow.

## Essayer sur son ordinateur

Il faut Python 3 avec numpy, ffmpeg (avec rubberband, sinon atempo prend le relais) et poppler (`pdftoppm`).

```
python3 construire.py lister --local "/chemin/vers/Répertoire du chœur"
python3 construire.py construire
python3 -m http.server -d _site 8000
```

## Changer le nom du chœur

Dans `site.json` : `nom`, `sous_titre`, et `nouveau_jours` (combien de jours un chant reste marqué « Nouveau »).

## Place prise

GitHub Pages accepte 1 Go. Un chant à quatre voix de 2 min 30 prend environ 20 Mo (pistes, pistes recalées en
trois vitesses, partition) : une cinquantaine de chants tiennent.
