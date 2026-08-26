# Changelog

## Unreleased

- Unraid installs follow the :latest image tag now. A container installed from an earlier template is still pointing at :alpha, which has stopped moving — change its Repository field to ghcr.io/stencil-projects/mediareducer:latest to keep receiving updates. (15affdc)
- The version number now reads 0.7.0. That is not a downgrade from 1.0.0-alpha.21 — the old counter never tracked how finished the app was, and 0.7.0 says plainly that this is pre-1.0 software still settling. (15affdc)
