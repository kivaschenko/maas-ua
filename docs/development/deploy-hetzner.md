# Hetzner deployment

The GitHub Actions workflow runs tests for pull requests targeting `main`. A
successful push to `main` (normally the merge commit from a PR) also deploys to
Hetzner over SSH. It updates `/opt/maas-ua` with a fast-forward-only pull and
builds/starts the production `app` service and its PostgreSQL dependency. It
does not start local-development services, reset local changes, or run database
migrations.

## Server setup

1. Install Docker Engine with the Compose plugin and Git on the server. Create
   `/opt` so the deployment account can create `/opt/maas-ua` there and run
   Docker.
2. Create `/opt/maas-ua/.env.production` before the first deployment:

   ```dotenv
   PROD_DB_PASSWORD=<64-character-hex-secret>
   APP_SECRET_KEY=<64-character-hex-secret>
   ```

   Generate independent values with `openssl rand -hex 32`. Keep this file
   readable only by the deployment account; it is ignored by Git.
3. Configure these GitHub Actions repository secrets:
   - `HETZNER_HOST`: server hostname or IP.
   - `HETZNER_USER`: deployment account.
   - `HETZNER_SSH_KEY`: private SSH key authorized for that account.
   - `HETZNER_KNOWN_HOSTS`: verified `known_hosts` entry for the server. Do not
     populate this from an unverified `ssh-keyscan` during a deployment.

The deployment account must be able to clone the public repository, run Docker
commands, and write `/opt/maas-ua`. Existing server-side changes in the checkout
cause the fast-forward-only update to fail rather than being overwritten.

## Access and database

The app and its PostgreSQL container use the host network, but both bind only to
loopback: the app listens on `127.0.0.1:8000`, and PostgreSQL on
`127.0.0.1:5433`. No public app or database port is opened. Connect through an
SSH tunnel, for example:

```bash
ssh -L 8000:127.0.0.1:8000 <deployment-user>@<server>
```

Then open `http://localhost:8000`. The existing application is still a
development-only Flask interface with no production authentication or
authorization; loopback binding and SSH access do not add application-level
access control.

The production PostgreSQL data is kept in the separate
`postgres_production_data` volume. The workflow never upgrades the schema.
Before using the database, review the target and backup, then run the
repository's approved Alembic upgrade procedure manually. Do not use this
deployment workflow for real inventory data until production access controls,
data handling, and an approved backup/migration procedure are in place.
