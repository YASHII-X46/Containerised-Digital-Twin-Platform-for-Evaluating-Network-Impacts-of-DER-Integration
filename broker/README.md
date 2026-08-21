# NATS Broker

Configuration for the NATS broker that carries OpenFMB command/event messages between the engines and the UI.

## `nats.conf`

The broker listens on the native NATS port `4222` and exposes monitoring on `8222`. The root `docker-compose.yml` mounts this file read-only into the `nats:2` container at `/etc/nats/nats.conf`.
