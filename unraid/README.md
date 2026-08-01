# Unraid template

`mediareducer.xml` is a Docker template for Unraid. It sets the two required
mounts (`/library`, `/config`), the two optional read-only appdata mounts that
only feed Auto Detect, the WebUI port, and PUID/PGID/TZ.

**It needs a published image.** `<Repository>` tracks
`ghcr.io/stencil923/mediareducer:alpha`, which `.github/workflows/publish.yml`
pushes when you tag a release:

    git tag v1.0.0-alpha.1 && git push origin v1.0.0-alpha.1

`:latest` is deliberately reserved for the first non-prerelease, so nobody
installs an alpha by asking for "latest". Point this at `:latest` when you cut
1.0.0. A GHCR package is private on first publish; make it public once under the
repository's Packages settings, or nobody can pull it.

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
