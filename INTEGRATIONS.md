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
