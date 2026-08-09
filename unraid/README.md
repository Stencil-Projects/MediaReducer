# Unraid template

`mediareducer.xml` is a Docker template for Unraid. Up front it asks for the
WebUI port and the five mounts: the two required (`/library`, `/config`) and
the three optional read-only appdata ones (Tautulli, Radarr, Sonarr) that only
feed Auto Detect. PUID/PGID and the reverse-proxy host list sit behind **Show
more settings**.

## What the template grants the container

`<ExtraParams>` drops every Linux capability and adds back four: `CHOWN` and
`DAC_OVERRIDE` so `entrypoint.py` can hand `/config` to the PUID/PGID user, and
`SETUID`/`SETGID` so it can become them. Past that point the process holds none
at all. `no-new-privileges` is on, and the container log is capped so a chatty
run cannot fill `docker.img`. `tests/unit/test_container_hardening.py` derives
that list from the calls the entrypoint makes, so adding one that needs a fifth
capability fails there rather than at someone's next container start.

The three appdata mounts are read-only and exist only for Auto Detect, which
reads one key out of each. Remove them once the keys are saved.

## Protecting a folder inside the library

Whatever is mapped to `/library` is what MediaReducer *can* delete; the
monitored paths in the UI are a setting on top of that. Map a folder read-only
at the matching spot underneath to take it off the table entirely:

    /mnt/user/media/Music  ->  /library/Music  (ro)

Docker layers the nested mount over the parent, so it is unwritable inside the
container whatever the UI says. Add it alongside the `/library` mapping rather
than instead of it — free space is measured on `/library` itself, and without
that mapping the reading comes from the container's own layer, not the array.

`<Repository>` tracks `ghcr.io/stencil923/mediareducer:alpha`, which
`.github/workflows/publish.yml` pushes on a version tag:

    git tag vX.Y.Z-alpha.N && git push origin vX.Y.Z-alpha.N   # must match APP_VERSION in app.py

`:alpha` moves with each alpha release; `:latest` is reserved for the first
non-prerelease, so point this at `:latest` at 1.0.0. The image path is lowercase
even though the repository name is not, since registries reject uppercase:

    docker pull ghcr.io/stencil923/mediareducer:alpha

## Installing it by hand

Copy the file onto the Unraid box, into the folder Unraid keeps user templates
in, and it appears under **Docker → Add Container → Template**:

    /boot/config/plugins/dockerMan/templates-user/my-MediaReducer.xml

## Listing it in Community Applications

CA indexes templates from repositories its maintainers have added, so being in
this repo is not enough on its own. It needs a published image, and the template
repository submitted to the CA feed. `<TemplateURL>` and `<Icon>` must both
resolve publicly for the listing to render, so check them after any rename or
branch change.
