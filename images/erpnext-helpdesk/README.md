# images/erpnext-helpdesk

The image the `erpnext-helpdesk` template runs: `ghcr.io/catenahq/erpnext-helpdesk`.

## Why it exists

A Frappe app reaches a Docker deployment only by being built into the image.
`bench get-app` inside a running container installs into that one
container and is gone at its next restart (frappe_docker's
`docs/01-getting-started/02-docker-immutability.md`). No upstream image
carries what the template needs: `frappe/erpnext` (the image of the `erpnext`
template) has no Helpdesk, and
`ghcr.io/frappe/helpdesk` has no ERPNext.

## What it contains

| App | Tracks | Role |
| --- | --- | --- |
| frappe | the newest release tag matching its pattern | the framework every app runs on |
| erpnext | the newest release tag matching its pattern | ERP, including its CRM (leads, prospects, opportunities) |
| telephony | the `develop` branch (no releases) | required by Helpdesk |
| helpdesk | the newest release tag whose `frappe-dependencies` admit the Frappe pinned | tickets, linked both ways to ERPNext customers |

`apps.json` declares this list, each input's tracking rule (tag pattern or
branch) and the value it is pinned to now (`version` and the commit `sha`
for a tag, `sha` for a branch), and the frappe_docker commit the image is built
with. Every run of `build.py resolve` sets each `sha` from upstream: the commit
a pinned tag names, the current head of a branch-tracked input. The build
fails when a branch moves while the image builds. An image is built from
exactly those values: Frappe is cloned at its pinned tag, each app at its
pinned tag or branch. Apps install on a site in
that file's order. The template decides which of them a site installs; an app
the site does not install costs disk only.

Each app's build-only `node_modules` are removed in the build layer, which
keeps about 740 MiB out of the image. Frappe's own stay: the websocket service
runs from them.

The image is repacked at the end of the build. The built image's tree is copied
into a scratch stage, one `COPY` per group of `LAYERS` in `build.py` (the bench
venv, each of Frappe and ERPNext, the rest of the bench, `.nvm`, the rest of
the frappe home, the system libraries, `/usr/share`, `/usr/local`, the rest of
`/`), so the image holds about ten layers and a host pulls them in parallel. The scratch stage restates the built image's
environment, user, working directory, entrypoint, command, volumes, ports and
labels. The build then fails unless a walk inside each image finds the same
paths with the same type, owner, mode, size, link target, extended attributes,
content hash and hard-link groups, and the same configuration (every field of
the image's config except its parent image id and hostname), and unless
every layer is at most `LAYER_BOUND` (400 MB). A hard-link group split across
two groups shows up as a difference.

The NLTK corpora an app lists under `nltk_data` in `apps.json` are part of the
image. Helpdesk loads them for its search and downloads any it cannot find
into the container, after every migration and from its scheduler, so a host
without them fetches them again after each update. The smoke test fails when
a migration downloads one.

## When a new image is built

`.github/workflows/erpnext-helpdesk-image.yml` runs daily and on a manual
dispatch: `build.py resolve --newest` moves `apps.json` to the newest version
of every input (a Frappe tag only once Frappe has published its `frappe/build`
and `frappe/base` images on Docker Hub, which the build is based on), then
`build.py plan` compares `apps.json`'s inputs with what each published image of
the ERPNext release records in its `io.catena.inputs` label. A new image is
built only when none equals the inputs: a Frappe, ERPNext or Helpdesk release,
a telephony commit, a frappe_docker commit pinned here, or the build-time steps
`build.py` adds to frappe_docker's Containerfile (the `recipe` input) differ
from every published build. When one equals them, that tag is the plan's tag. A
push of `images/erpnext-helpdesk/**` to `main` runs `build.py resolve` instead,
which only refreshes the shas, and plans from what `apps.json` names.

A manual run of the workflow with `force` builds and tests without a difference,
and with `publish` off it stops before the image is pushed and pins nothing.
A tag the registry already holds is never pushed again.

## The pin

The template's seven Frappe services pin the image's tag in
`blueprints/erpnext-helpdesk/docker-compose.yml`. After the image is published,
or when the template pins another tag than the plan's, `build.py pin`
sets the seven pins to the plan's tag. When that, or the new `apps.json`,
differs from `main`, the workflow commits both files to the branch
`image-pin/erpnext-helpdesk`, pushes it only when its content differs from
the remote branch, opens a pull request and turns auto-merge on; the
repository's required checks decide the merge. The GitHub App token that
does this is minted in the last steps, after any build was tested and
published.

Tags are `v<erpnext version>-<n>`: `v1.2.3-1` is the first build of
ERPNext 1.2.3, `v1.2.3-2` the next build of it with another input moved.
The Catena update engine orders them (the `build_suffix` rule in the `images`
entry of the template's `x-catena` block, whose tag pattern holds them to one
major line) and each host decides when to take one: after its soak, its CVE check, and with a
database snapshot it replays when the migration fails.

## Before an image is pushed

`build.py test` runs the `erpnext-helpdesk` template's own backend
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
python3 build.py resolve        # refreshes every sha in apps.json; --newest moves every input
python3 build.py plan           # writes inputs.json; --force to build anyway
python3 build.py build
python3 build.py test           # --fresh skips the upgrade from the previous image
python3 build.py publish        # needs a docker login to ghcr.io and GHCR_TOKEN
python3 build.py pin            # sets the blueprint's pins to inputs.json's tag
```

`GHCR_USER` and `GHCR_TOKEN` in the environment authenticate the registry
reads of `plan` and `publish`; a package that is not public lists no
tags without them.
