# Cisco Meraki Personal Dashboard

Dashboard personnel pour agréger l'inventaire de plusieurs organisations Cisco Meraki avec cache local SQLite.

## Fonctionnalités v1

- Liste des organisations Meraki visibles par la clé API.
- Nombre d'équipements par organisation.
- Cache local SQLite pour éviter les appels API à chaque chargement.
- Bouton de refresh forcé.
- API backend prête pour ajouter plus de détails ensuite.

## Prérequis

- Docker et Docker Compose.
- Une clé API Meraki Dashboard avec accès en lecture aux organisations.

## Démarrage

```bash
cp .env.example .env
# éditer .env et renseigner MERAKI_API_KEY

docker compose up --build
```

L'application sera disponible sur http://localhost:8000

## Endpoints

- `GET /` : dashboard web.
- `GET /api/organizations` : données locales agrégées.
- `POST /api/refresh` : refresh forcé global.
- `POST /api/refresh/{organization_id}` : refresh forcé d'une seule organisation.

## Stratégie anti-rate-limit

- Les données affichées proviennent de SQLite.
- Aucun appel Meraki n'est fait au simple affichage de la page.
- Le refresh n'est déclenché que manuellement, ou au premier démarrage si la base est vide.

## Structure

- `app/main.py` : backend FastAPI.
- `app/templates/dashboard.html` : interface principale.
- `app/static/styles.css` : styles.
- Base SQLite stockée dans le volume Docker `/data`.
