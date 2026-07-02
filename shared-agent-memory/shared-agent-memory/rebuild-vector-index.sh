#!/bin/sh
set -eu
cd /opt/shared-agent-memory
exec /opt/shared-agent-memory/.venv/bin/python /opt/shared-agent-memory/memory_vector_index.py build
