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
