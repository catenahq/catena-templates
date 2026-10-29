# Security policy

Email **security@catena.run** (see the full policy in
[catenahq/catena-ce SECURITY.md](https://github.com/catenahq/catena-ce/blob/HEAD/SECURITY.md)).

Scope for THIS repository: the per-template metadata (`sources/<id>.json`),
the hand-edited stack files (`blueprints/<id>/docker-compose.yml`), the
render pipeline (`build/render.py`), and the generated `templates.json`,
`catalog.json` and blueprint READMEs that client Portainer instances fetch
from raw.githubusercontent.com. A compose change that
weakens a template's isolation (network exposure, dropped auth labels,
privileged mounts) is squarely in scope.

Every pull request, from a fork too, and every commit pushed to `main` runs
`.github/workflows/app-ci.yml`'s checks (a pull request before a person reviews
it, a commit of `main` through `.github/workflows/app-main.yml`): the app checker, the swarm loader, scanctl on the
app's images (a fixable high or critical vulnerability
fails), image provenance (a source repository at least a year old with three
releases, else for a Docker Hub image its Docker Hub repository registered at
least a year ago with three pushed tags; an image the repository builds itself
is listed as its own) and ClamAV over each image's files. The workflow holds
`contents: read` only and passes pull-request text to scripts through
environment variables. A push to `main` sends only its commits that passed
these checks to the bench (the nightly run takes the head of `main`), and an
app is tagged only when they passed at the commit that last changed it.

A pull request from a fork never reaches a runner that holds a secret. After
`app-ci` passes it, `.github/workflows/adopt-fork.yml` copies the files of
exactly one app onto a branch of this repository through the API, as data: it
checks out and runs nothing of the fork, and refuses links, submodules, files
over 1 MB, paths outside the app and a change to more than one app. The bench
then runs that branch's app, and the fork's `test.yml` steps as root, on a
private runner in a disposable test VM copied from the Catena base VM. That VM holds only
throwaway credentials (its own backup repositories and a sandbox licence, and
Catena's own generated secrets made new; none the bench's, checked by a scan), is refused unless the app checker finds no X7, X8 or X9 in the
compose file, runs only the steps of `test.yml` that touch the app's own
containers and volumes, and has its outbound network dropped except the
registry caches, DNS, time servers and the hosts the app's block declares. The
hosts a pull request adds to the block are listed in the test result for review.

Vulnerabilities in the upstream applications the templates deploy
belong upstream; we track and ship the fixed versions.
