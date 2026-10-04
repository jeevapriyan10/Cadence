#!/bin/sh
set -e

# Run database migrations with Alembic
echo "Applying database migrations..."
alembic upgrade head

# Execute the main container command (defaults to uvicorn in Dockerfile CMD)
exec "$@"
