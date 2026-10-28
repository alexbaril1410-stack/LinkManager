# Intégrations en lecture seule

LinkManager interroge qBittorrent, Radarr et Sonarr uniquement pour le diagnostic et l'inventaire. Les identifiants restent dans l'environnement du conteneur : ils ne sont pas transmis au navigateur.

## 1. Configurer les URL et clés

Dans `docker-compose.yml`, remplace les valeurs vides :

- `QBITTORRENT_URL` : URL de la WebUI qBittorrent accessible depuis le conteneur LinkManager.
- `QBITTORRENT_USERNAME` et `QBITTORRENT_PASSWORD` : identifiants WebUI.
- `RADARR_URL` et `RADARR_API_KEY` : URL et clé API Radarr (Settings > General).
- `SONARR_URL` et `SONARR_API_KEY` : URL et clé API Sonarr (Settings > General).

Exemples de ports habituels, à adapter à ton installation : qBittorrent WebUI `http://IP_DU_SERVEUR:8080`, Radarr `http://IP_DU_SERVEUR:7878`, Sonarr `http://IP_DU_SERVEUR:8989`.

Ces URL doivent être joignables **depuis le conteneur LinkManager**. Les noms de conteneurs ne fonctionnent que si les conteneurs partagent un réseau Docker. Ne publie pas ces interfaces ou clés sur Internet. Évite de committer les clés API et mots de passe dans un dépôt public.

## 2. Correspondance des chemins

Les chemins Docker diffèrent entre les applications. Les variables `*_PATH_MAP` fournies dans Compose font correspondre le chemin vu par chaque application au chemin visible par LinkManager sous `/media`. Elles sont préremplies à partir des montages connus pour SSD2TO, HDD1TO, HDD500, RAID480 et SSD 240GB.

Si les montages Docker changent, adapte ces correspondances. Un chemin non associé est signalé comme tel ; LinkManager ne devine pas le chemin hôte. Les chemins associés sont indicatifs et ne remplacent pas une vérification du périphérique/système de fichiers avant la création d'un hardlink.

## 3. Déployer et tester

Après avoir modifié le fichier Compose :

```bash
docker compose pull linkmanager
docker compose up -d linkmanager
docker compose logs -f linkmanager
```

Ouvre LinkManager puis clique sur **Tester les connexions**. Le panneau affiche l'état des connexions, la version, un aperçu limité à 30 torrents et les dossiers racines connus de Radarr/Sonarr. L'inventaire est relu uniquement lors du clic ou du chargement de la page.

## 4. Sécurité et limites

- Les intégrations sont en lecture seule : aucun torrent n'est ajouté, arrêté, déplacé ou supprimé ; aucune bibliothèque Radarr/Sonarr n'est modifiée.
- Les requêtes ont un délai d'attente court et l'aperçu qBittorrent est limité.
- Les identifiants ne sont jamais retournés par l'API LinkManager.
- Si la connexion échoue, vérifie l'URL depuis le conteneur, la clé API, les identifiants et les règles réseau.
- LinkManager n'effectue pas de nettoyage automatique. Les chemins de stockage affichés ne suffisent pas, à eux seuls, à conclure qu'un fichier peut être supprimé.


## 5. Diagnostic croisé et actions manuelles

Le bouton **Analyser les médias** appelle `GET /api/audit`. Il rapproche les fichiers qBittorrent, les fichiers Radarr/Sonarr et les chemins réellement visibles sous `/media`.

- **Hardlink confirmé** : même périphérique et même inode pour les chemins locaux.
- **Contenu identique** : SHA-256 identique ; la création d'un hardlink est envisageable seulement si les fichiers sont sur le même système de fichiers.
- **Candidat non vérifié** : nom et taille identiques, mais comparaison de contenu non effectuée.
- **Sans correspondance** : simple signalement à examiner, pas une preuve d'orphelin.
- Le diagnostic récupère les torrents par pages de 500, jusqu'à 50 000 entrées, puis analyse jusqu'à 50 000 fichiers. La lecture SHA-256 reste limitée à 4 Gio par analyse. Si la limite de torrents est atteinte, l'inventaire est signalé comme tronqué ; les actions destructives refusent une vérification incomplète.

### Actions explicitement déclenchées

Chaque action demande la saisie exacte de `SUPPRIMER`. Les routes disponibles sont :

- `POST /api/actions/remove-torrent` : retire un torrent, avec choix de conserver ou demander à qBittorrent de supprimer ses fichiers. Pour supprimer les fichiers, tous les chemins doivent être associés et vérifiables sous `/media`.
- `POST /api/actions/remove-library-file` : demande à Radarr/Sonarr de retirer un fichier de bibliothèque. Un autre nom de hardlink, s'il existe, reste indépendant.
- `POST /api/actions/unmonitor` : désactive la surveillance d'un film ou d'une série.
- `POST /api/actions/remove-library-item` : retire l'entrée Radarr/Sonarr, arrête sa surveillance et ajoute une exclusion, sans supprimer les fichiers sur disque.
- `POST /api/actions/remove-empty-folder` : retire uniquement un dossier vide ; le système de fichiers refuse l'opération si le dossier contient encore quelque chose.

Les actions sont manuelles et sont consignées dans les journaux. Les secrets d'API restent côté serveur. Vérifie toujours l'élément et les chemins affichés avant de confirmer.


## 6. Cache persistant et redémarrages

Le diagnostic utilise SQLite dans `/app/data/linkmanager.sqlite3`, stockée dans le volume Docker nommé `linkmanager_data`. Après un redémarrage du conteneur, LinkManager peut réutiliser les inventaires encore valides au lieu de relire tous les torrents et leurs fichiers. Les listes générales ont un TTL de 300 secondes par défaut (`INVENTORY_CACHE_TTL_SECONDS`). Les fichiers de torrent sont mis en cache par hash ; les chemins et les métadonnées réelles sous `/media` sont recalculés pendant l'analyse. Le cache ne sert jamais à autoriser une suppression : les vérifications de références effectuées avant une action destructive interrogent les applications en direct. Pour forcer une nouvelle collecte des listes, attendre l'expiration du TTL ou réduire temporairement sa valeur dans `docker-compose.yml`.
