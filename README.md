# LinkManager

Interface web pour explorer des fichiers et afficher/créer des hardlinks.

## Image Docker sur GHCR

Le workflow `.github/workflows/docker-publish.yml` publie automatiquement l'image :
`ghcr.io/alexbaril1410-stack/linkmanager:latest`.

## Configuration sans fichier .env

Les paramètres sont définis directement dans `docker-compose.yml`. Modifie :
- `LINKMANAGER_PASSWORD` avec un mot de passe long et unique ;
- `volumes.source` avec le chemin absolu hôte commun qui contient les médias et téléchargements.

## Déploiement

```bash
docker compose pull
docker compose up -d
docker compose logs -f linkmanager
```

Accès : `http://IP-DU-SERVEUR:8088`. Ne pas exposer directement l'application à Internet ; utiliser HTTPS via un reverse proxy pour l'accès distant.

Le conteneur tourne avec UID/GID 10001. Cet utilisateur doit avoir les droits de lecture des sources et d'écriture dans le dossier de destination. Les hardlinks nécessitent le même système de fichiers. La V1 ne supprime ni ne déplace aucun fichier.


## Intégrations qBittorrent, Radarr et Sonarr

LinkManager interroge ces applications pour le diagnostic en lecture seule. Des actions manuelles facultatives sont disponibles derrière une confirmation explicite ; aucune suppression ou modification n’est automatisée.

Consulte [INTEGRATIONS.md](INTEGRATIONS.md) pour la configuration détaillée. Tous les paramètres sont définis directement dans `docker-compose.yml` ; aucun fichier `.env` n'est requis.


## Diagnostic croisé et actions manuelles

Le bouton **Analyser les médias** compare les fichiers torrent aux fichiers Radarr/Sonarr et à leurs chemins locaux, confirme les hardlinks par inode/périphérique et compare par SHA-256 un nombre limité de candidats. Les éléments sans correspondance sont des pistes de vérification, jamais une preuve suffisante pour supprimer.

L'inventaire qBittorrent est paginé par lots de 500 pour prendre en charge les grosses bibliothèques de cross-seed (jusqu'à 50 000 torrents et 50 000 fichiers par analyse, avec indicateur si la limite est atteinte). Les actions de suppression ou de modification sont lancées manuellement et nécessitent de saisir `SUPPRIMER`. Le retrait d'un élément Radarr/Sonarr conserve ses fichiers ; la suppression des fichiers du torrent est une option séparée. Les dossiers ne peuvent être supprimés par l'outil que s'ils sont vides.


## Cache persistant des inventaires

Le diagnostic utilise une base SQLite située dans `/app/data/linkmanager.sqlite3`, conservée dans le volume Docker nommé `linkmanager_data`. Les listes qBittorrent/Radarr/Sonarr sont réutilisées pendant 300 secondes par défaut. La liste des fichiers de chaque torrent est conservée par hash du torrent, car son contenu est immuable pour un hash donné ; seules les métadonnées locales (existence, inode, hardlinks) sont relues pendant l’analyse. Les actions sensibles continuent de vérifier les références en direct et n’utilisent pas ce cache pour autoriser une suppression. Le cache est configurable avec `INVENTORY_CACHE_TTL_SECONDS` et `LINKMANAGER_DB_PATH` dans `docker-compose.yml`.
