#!/bin/bash
set -euo pipefail

# Variables
PG_CONTAINER_NAME=pg-installer-db
PG_USER=admin
PG_PASSWORD=adminpw
PG_DB=installer
SQL_FILE=/root/ai-db/installer-db-backup.sql
PG_IMAGE=registry.redhat.io/rhel9/postgresql-15

# 1. Remove old container if it exists
if podman ps -a --format '{{.Names}}' | grep -q "^${PG_CONTAINER_NAME}$"; then
  echo "Removing existing container ${PG_CONTAINER_NAME}..."
  podman rm -f $PG_CONTAINER_NAME
fi

# 2. Run a new Postgres container
podman run -d \
  --name $PG_CONTAINER_NAME \
  -e POSTGRESQL_USER=$PG_USER \
  -e POSTGRESQL_PASSWORD=$PG_PASSWORD \
  -e POSTGRESQL_DATABASE=$PG_DB \
  -p 5432:5432 \
  $PG_IMAGE

# 3. Wait until the DB is accepting connections
echo "Waiting for Postgres to be ready..."
until podman exec $PG_CONTAINER_NAME /usr/bin/pg_isready -U $PG_USER -d $PG_DB >/dev/null 2>&1; do
  sleep 2
done

# 4. Copy SQL backup file
podman cp $SQL_FILE $PG_CONTAINER_NAME:/tmp/backup.sql

# 5. Restore the database
podman exec -i $PG_CONTAINER_NAME \
  /usr/bin/psql -U $PG_USER -d $PG_DB -f /tmp/backup.sql

echo "✅ Database restored successfully."

# 6. Example query
podman exec -it $PG_CONTAINER_NAME \
  /usr/bin/psql -U $PG_USER -d $PG_DB -c "\dt"

