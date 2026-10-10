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
| frappe | the release branch `apps.json` names | the framework every app runs on |
| erpnext | the newest release matching the tag pattern in `apps.json` | ERP, including its CRM (leads, prospects, opportunities) |
| telephony | the `develop` branch (no releases) | required by Helpdesk |
| helpdesk | the newest release that supports the Frappe it builds against | tickets, linked both ways to ERPNext customers |

`apps.json` declares this list and the frappe_docker commit the image is built
with. Apps install on a site in that file's order. The ERPNext template decides
which of them a site installs; an app the site does not install costs disk
only.

Each app's build-only `node_modules` are removed in the build layer, which
keeps about 740 MiB out of the image. Frappe's own stay: the websocket service
runs from them.

The NLTK corpora an app lists under `nltk_data` in `apps.json` are part of the
image. Helpdesk loads them for its search and downloads any it cannot find
into the container, after every migration and from its scheduler, so a host
without them fetches them again after each update. The smoke test fails when
a migration downloads one.

## When a new image is built

`.github/workflows/erpnext-image.yml` runs `build.py resolve` daily. It reads
the newest version of every input from upstream and compares it with what the
newest published image records in its `io.catena.inputs` label. A new image
is built only when one of them moved: a Frappe, ERPNext or Helpdesk release, a
telephony commit, a frappe_docker commit pinned here, or the build-time steps
`build.py` adds to frappe_docker's Containerfile (the `recipe` input).

Tags are `v<erpnext version>-<n>`: `v1.2.3-1` is the first build of
ERPNext 1.2.3, `v1.2.3-2` the next build of it with another input moved.
The Catena update engine orders them (`build_suffix` in catena-admin
`payload/engines/stackupdate/overrides.go`, whose tag pattern holds them to
one major line) and each
host decides when to take one: after its soak, its CVE check, and with a
database snapshot it replays when the migration fails.

## Before an image is pushed

`build.py test` runs the catalog template's own backend
(`smoke-compose.yml` extends it), whose script writes the bench config,
creates a site with every app and starts the app server. It then runs
`bench --site all migrate` and expects `/api/method/ping` to answer. When a
previous image exists, the site is created on it, and the new image's backend
starts on that site before the migration, as on a host, so a release whose
backend cannot start on its predecessor's site, or whose migration fails,
never reaches a host.

## Run it locally

Docker with buildx and compose, git, and a python3 whose standard library
has `tomllib`:

```sh
python3 build.py resolve        # writes inputs.json; --force to build anyway
python3 build.py build
python3 build.py test           # --fresh skips the upgrade from the previous image
python3 build.py publish        # needs a docker login to ghcr.io
```
