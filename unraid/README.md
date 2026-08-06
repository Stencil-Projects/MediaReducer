# Unraid template

`mediareducer.xml` is a Docker template for Unraid. Up front it asks for the
WebUI port and the five mounts: the two required (`/library`, `/config`) and
the three optional read-only appdata ones (Tautulli, Radarr, Sonarr) that only
feed Auto Detect. PUID/PGID and the reverse-proxy host list sit behind **Show
more settings**.

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
