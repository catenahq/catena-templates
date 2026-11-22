# Security policy

Email **security@catena.run** (see the full policy in
[catenahq/catena-ce SECURITY.md](https://github.com/catenahq/catena-ce/blob/HEAD/SECURITY.md)).

Scope for THIS repository: the per-template card files (`blueprints/<id>/app.json`),
the hand-edited stack files (`blueprints/<id>/services.yml`, `x-catena.yml` and
`hooks/`) and the packed `blueprints/<id>/docker-compose.yml` a host deploys, the
render pipeline (`build/render.py`), and the catalog document
`build/catalog.py` prints. A host fetches a blueprint folder at
a tag, and the ops tooling and the bench run `build/catalog.py`. A compose change that
weakens a template's isolation (network exposure, dropped auth labels,
privileged mounts) is squarely in scope.

Every pull request, from a fork too, runs the app checks of the shared PR
workflow (`.github/workflows/ci.yml`, with `apps` in `.github/checks.yml`) before
a person reviews it: the app checker, the swarm loader, scanctl on the
app's images (a fixable high or critical vulnerability
or an image secret that the change adds fails), image provenance (a source repository at least a year old with three
releases, or three tags when it publishes no release, else for a Docker Hub image its Docker Hub repository registered at
least a year ago with three pushed tags; the year of age is asked of a pull request from a fork only; an image the
repository builds itself is listed as its own) and ClamAV over each image's files. The check jobs hold
`contents: read` only and receive no secret. Only a pull request of this
repository is sent to the app tests, and only once its ci run completes; an app is
tagged on the merge only when the app tests passed its change.

A pull request from a fork never reaches a runner that holds a secret. A
maintainer who has read it runs `.github/workflows/adopt-fork.yml` by hand with
its number: it copies the files of exactly one app onto a branch of this
repository through the API, as data, and opens the pull request from that
branch. It checks out and runs nothing of the fork, and refuses a pull request
that is not open or not from a fork, links, submodules, files over 1 MB, paths
outside the app and a change to more than one app. That pull request sends the
app to the app tests, which run its app, and the fork's `test.yml` steps as
root, on a
private runner in a disposable test VM copied from the Catena base VM. That VM holds only
throwaway credentials (its own backup repositories and a sandbox licence, and
Catena's own generated secrets made new; none the app tests', checked by a scan), is refused unless the app checker finds no X7, X8 or X9 in the
compose file, runs only the steps of `test.yml` that touch the app's own
containers and volumes or run in a helper sharing an app container's network,
and has its outbound network dropped except the
registry caches, DNS, time servers and the hosts the app's block declares. The
hosts a pull request adds to the block are listed in the test result for review.

Vulnerabilities in the upstream applications the templates deploy
belong upstream; we track and ship the fixed versions.
