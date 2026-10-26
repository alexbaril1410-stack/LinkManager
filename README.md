# LinkManager

Interface web pour parcourir un espace de fichiers et inspecter/créer des hardlinks.

## Fonctionnalités de cette base V1

- Explorateur de fichiers et recherche limitée.
- Affiche `nlink`, inode, périphérique, type et taille.
- Création manuelle de hardlinks avec confirmation explicite.
- Refuse les destinations existantes et les sources symboliques.
- Vérifie que source et destination sont sur le même système de fichiers.
- Authentification HTTP Basic, journalisation et chemins confinés à `/media`.
- Conteneur non-root, système de fichiers racine en lecture seule, sans capabilities Linux.

## Pré-requis

- Docker Engine et Docker Compose.
- Un chemin hôte commun qui expose les dossiers souhaités sous `/media`.
- Les permissions du processus conteneur doivent permettre la lecture et la création d'entrées dans les dossiers de destination.

## Démarrage

```bash
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f linkmanager
```

Ouvrir `http://IP_DU_SERVEUR:8088` puis saisir l'utilisateur et le mot de passe définis dans `.env`.

**Ne pas exposer directement ce port sur Internet.** Pour un accès distant, utiliser un reverse proxy HTTPS avec une protection adaptée.

## Configuration du montage /media

`MEDIA_HOST_PATH` doit être le chemin hôte commun contenant les dossiers entre lesquels les hardlinks doivent être créés. Par exemple, si l'hôte possède `/srv/media/torrents` et `/srv/media/library`, configurez :

```dotenv
MEDIA_HOST_PATH=/srv/media
```

Ils apparaîtront alors dans le conteneur comme `/media/torrents` et `/media/library`.

Attention : monter un répertoire commun ne garantit pas à lui seul que les dossiers sont sur le même système de fichiers. Si des sous-dossiers sont des montages distincts, le système les détectera généralement via le périphérique (`st_dev`) et refusera de créer un hardlink entre eux. Vérifier également que le chemin hôte est bien le même stockage physique que celui utilisé par les conteneurs de téléchargement et de médiathèque.

## UID/GID et permissions

L'application tourne par défaut avec UID/GID `10001:10001`, sans privilèges. Si les fichiers appartiennent à un autre UID/GID, adaptez le `USER` du Dockerfile ou utilisez une stratégie de groupe adaptée à votre hôte. Ne lancez pas le conteneur en root juste pour contourner des permissions.

La création d'un hardlink nécessite :
- le droit de lecture sur le fichier source et de traversée sur ses dossiers parents ;
- le droit d'écriture et de traversée dans le dossier de destination ;
- le même système de fichiers ;
- une destination qui n'existe pas déjà.

## Limites et précautions

- `nlink` est le nombre total de liens physiques connus du système de fichiers, pas le nombre de chemins affichés par l'application.
- La V1 ne supprime aucun fichier et n'effectue aucun déplacement.
- La recherche est limitée à 20 000 entrées analysées par requête et à 500 résultats.
- Cette V1 ne possède pas encore de mécanisme CSRF dédié ni de gestion multi-utilisateur. Garder l'application sur un réseau de confiance, ne pas l'exposer publiquement et placer un reverse proxy correctement configuré devant elle si nécessaire.
- L'authentification HTTP Basic doit être utilisée uniquement derrière HTTPS lorsqu'un accès traverse un réseau non fiable.

## Développement local

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
LINKMANAGER_USER=admin LINKMANAGER_PASSWORD='mot-de-passe-local' MEDIA_ROOT=/media uvicorn app.main:app --reload
```

## Dépôt GitHub

1. Créer un dépôt GitHub vide nommé `linkmanager`.
2. Décompresser le projet, puis depuis son dossier :

```bash
git init
git add .
git commit -m "Initial LinkManager V1"
git branch -M main
git remote add origin https://github.com/VOTRE-COMPTE/linkmanager.git
git push -u origin main
```

Ne jamais committer le fichier `.env`.
