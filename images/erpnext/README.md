# images/erpnext

The image the `erpnext` template runs: `ghcr.io/catenahq/erpnext`.

## Why it exists

A Frappe app reaches a Docker deployment only by being built into the image.
`bench get-app` inside a running container installs into that one
container and is gone at its next restart (frappe_docker's
`docs/01-getting-started/02-docker-immutability.md`). No upstream image
carries what the template needs: `frappe/erpnext` has no Helpdesk, and
`ghcr.io/frappe/helpdesk` has no ERPNext.

## What it contains

| App | Tracks | Role |
| --- | --- | --- |
| frappe | the `version-16` branch | the framework every app runs on |
| erpnext | the newest `v16.x.y` release | ERP, including its CRM (leads, prospects, opportunities) |
| telephony | the `develop` branch (no releases) | required by Helpdesk |
| helpdesk | the newest release that supports the Frappe it builds against | tickets, linked both ways to ERPNext customers |

`apps.json` declares this list and the frappe_docker commit the image is built
with. Apps install on a site in that file's order. The ERPNext template decides
which of them a site installs; an app the site does not install costs disk
only.

Each app's build-only `node_modules` are removed in the build layer, which
keeps about 740 MiB out of the image. Frappe's own stay: the websocket service
runs from them.

## When a new image is built

`.github/workflows/erpnext-image.yml` runs `build.py resolve` daily. It reads
the newest version of every input from upstream and compares it with what the
newest published image records in its `io.catena.inputs` label. A new image
is built only when one of them moved: a Frappe, ERPNext or Helpdesk release, a
telephony commit, or a frappe_docker commit pinned here.

Tags are `v<erpnext version>-<n>`: `v16.37.0-1` is the first build of
ERPNext 16.37.0, `v16.37.0-2` the next build of it with another input moved.
The Catena update engine orders them (`build_suffix` in catena-admin
`payload/engines/stackupdate/overrides.go`, locked to the v16 line) and each
host decides when to take one: after its soak, its CVE check, and with a
database snapshot it replays when the migration fails.

## Before an image is pushed

`build.py test` creates a site with every app, runs `bench --site all
migrate` and expects `/api/method/ping` to answer. When a previous image
exists, the site is created on it and then upgraded to the new image, so a
release whose migration fails never reaches a host.

## Run it locally

Docker with buildx and compose, git, python 3.11+:

```sh
python3 build.py resolve        # writes inputs.json; --force to build anyway
python3 build.py build
python3 build.py test           # --fresh skips the upgrade from the previous image
python3 build.py publish        # needs a docker login to ghcr.io
```
