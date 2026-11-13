# Plan: ops issue 473, an application with no web address reads as broken

## Summary

The call relay (and the virus scanner) declares no web address, as the catalog contract asks
(catena-templates AGENTS.md:57-58). The panel renders that as a fault: a "not reachable" badge and the line
"Not reachable: no service of this app declares its public address (vps.route.host)", and a grey dot, because
a tile's health comes only from Gatus by host and the hourly verify answer is kept nowhere the panel reads. Nextcloud
wires Talk to the relay but its verify hook never checks that wiring, so nothing says whether Talk uses the relay.

Three changes, in push order:

1. catena-admin: a tile of an application with no web address says in plain words that it runs in the
   background, with no not-routed badge and no "Not reachable" line.
2. catena-admin: `catena-apps verify` keeps each application's last verdict with its time; the view file carries
   it; the tile of an application with no web address takes its dot from it.
3. catena-templates: the relay's setup steps say where Nextcloud Talk lists it.

## What depends on what today

- dashboard-sync writes `not_routed = "no_route_host"` for an app whose services declare no `vps.route.host`
  (catena-admin payload/lib/app_intent.py:316-324). Its other readers stay as they are: gate_routes
  (payload/lib/gate_routes.py:65), oidc_clients (payload/lib/oidc_clients.py:467), gatus sync and version check
  (payload/lib/tests/test_gatus_sync_health_labels.py:32, test_version_check.py:220), the restore scope
  (shell/web/restorescope_test.go:20), the app checker (shell/appcheck/appcheck_test.go:32). The code stays; only the
  panel's reading of it changes.
- The tile: shell/apps/apps.go:432-434 turns any app-wide or main-address reason into an admin-only tile with
  `NotRoutedBadge` and `NotRouted`; :443-445 is the only health source (Gatus by host). The template renders the dot
  at shell/web/templates/pages/apps.tmpl:32-35 and the reason at :64-66; the badge text is
  shell/web/translations/en.yml:546, the reason en.yml:550 and fr.yml:550.
- The verify: payload/engines/apps/hooks.go:215-252 (`Verify`) returns ok, fail, stopped, none or starting and tells
  the maintenance feed of a failure only (hooks.go:311-323). `runVerify` (hooks_cli.go:18-81) prints the results as
  JSON; for one stack the release of its lock rewrites the view (hooks_cli.go:65, applock.go:113-152); for `--all` the
  view is rewritten at hooks_cli.go:71-75. The hourly timer is payload/lanes/systemd/catena-app-verify.timer:9; its
  unit waits up to an hour for the host lock and has `TimeoutStartSec=3h` (catena-app-verify.service:10-17).
- The update lane calls `apps.Verify` too, before its commit (payload/engines/stackupdate/folders.go:199-217), and
  runs no verify after a rollback.
- The per-stack state `<StateRoot>/<stack>.json` is rewritten whole by `Stop` (deploy.go:205) and
  `recordFingerprints` (deploy.go:539), each keeping only `Failure`, and created fresh by add (add.go:101): a field
  added to `State` would be dropped by them. The start mark `<StateRoot>/<stack>.starting` (starting.go:16-69) is the precedent of a small per-stack file
  beside the state; `StateRoot` is `/var/lib/catena/apps` (folder.go:34).
- The view: payload/engines/apps/view.go:52-97 (`ViewApp`), read by shell/appview/appview.go:63-105 (`App`), version 1
  on both sides (view.go:28, appview.go:30). The Apps page builds tiles in shell/web/server.go:1047-1067;
  `MarkStarting` (apps.go:249-258) runs there for every viewer.
- Nextcloud: hooks/configure.sh:81-102 writes Talk's relay entries once per relay (`turn_record`, recorded in
  `catena turn_wired`), :274-293 calls it while Talk is enabled; hooks/verify.sh:1-24 checks occ and status.php
  only; tests/test_nextcloud.py drives both hooks over a stand-in occ (FAKE, :38-221) and pins the create-once wiring
  (`test_a_second_configure_adds_nothing_twice`, :377-383).
- Talk 25 (spreed `stable35`, the release for the pinned `nextcloud:35.0.2-apache`, services.yml:3; its
  appinfo/info.xml says version 25.0.8, nextcloud min and max 35):
  - `talk:turn:list` (lib/Command/Turn/ListCommand.php) calls `writeMixedInOutputFormat` on the stored array, which
    with `--output=json` prints `json_encode` of it on one line (server core/Command/Base.php `stable35`,
    `writeArrayInOutputFormat`). Each entry is `{"schemes","server","secret","protocols"}` as
    lib/Command/Turn/Add.php writes it.
  - `talk:stun:list --output=json` prints the list of `host:port` strings.
  - occ loads the commands of enabled apps only (server lib/private/Console/Application.php `stable35`,
    `loadCommands`), so `talk:turn:list` fails while Talk is disabled.

## Design

### Panel wording (step 1)

- a. Panel only (recommended): `appTiles` tells `no_route_host` apart from the other reasons. Such a tile stays
  admin-only with no link, carries `NoAddress` instead of `NotRouted`, gets no badge, and the template shows one line
  `apps.no_address`. The key `apps.not_routed.no_route_host` goes (nothing renders it). The app-intent code and its
  other readers are untouched.
- b. Rename or split the reason in dashboard-sync. Rejected: six readers key on the code, and the panel is the one
  place that misreads it.

A hand-written app that forgot its label reads the same line ("no web address"), which is still true and says what
to look at; the compose checker is the place that tells an author about labels.

### Where the verdict lives (step 2)

- a. A small per-stack file `<StateRoot>/<stack>.verify` holding `{"verdict":"ok","at":"<RFC 3339 UTC>"}`
  (recommended). No `State` writer can drop it; it follows the `.starting` file's life (written by its verb, removed
  by Stop and Delete). It is rebuildable (the next hourly run), so it needs no backup assertion.
- b. A field of `State`. Rejected: the writers that build a fresh `State` (deploy.go:205, deploy.go:539,
  add.go:101) would each have to carry it, and so would every later one.
- c. Parse the maintenance feed. Rejected: it holds failures and forgets, never a pass.

Recorded by the verb, not by `Verify`: `runVerify` records each result before its `release()` (hooks_cli.go:65), so
the timer, the bench and a person running `catena-apps verify` record, and the update lane's verify of a version it
then rolls back does not leave a red dot for the version that runs.

The detail of the answer is not kept: it is the app's own output, and the feed already carries its last line.

### Freshness bound

A verdict older than 3 hours reads unknown. The timer runs hourly; a run may wait up to an hour for the host lock and
the unit may run up to 3 hours in all (catena-app-verify.service:10-17). Three hours is the longest one run may take,
so a verdict past it means at least two hourly runs did not reach the app. The constant lives once, in shell/appview,
with that reason beside it.

### Health mapping

On a tile with `NoAddress` only: `ok` within the bound is healthy, `fail` within the bound is unhealthy, anything else
(none, stopped, starting, no file, too old) is unknown. A starting app already shows the starting dot (`MarkStarting`).
Routed tiles keep Gatus.

### Talk and the relay (step 3)

A Nextcloud verify check of Talk's relay entries is dropped: see Step 3 and Decision 2.

### Service tiles

The default apps (the monitoring apps) also label no address, so their rows carry `no_route_host` too. `MarkServices`
(apps.go:264-287) turns them into service tiles: it clears `NoAddress` beside `NotRouted` and sets their health from
Gatus by the converge's address, so it runs after `MarkVerified` (server.go:1052-1057) and overwrites it.

## Steps

### Step 1 [Open] catena-admin: an app with no web address is a background tile, not a fault (S)

Files:
- shell/appintent/appintent.go: `const NoRouteHost = "no_route_host"` beside `Inactive` (:28-29), with its comment.
- shell/apps/apps.go: `Tile.NoAddress bool` with its comment beside `NotRouted` (:44-46, :78); in `appTiles`
  (:395-460) a reason equal to `appintent.NoRouteHost` sets mode admin-only, groups admin and `NoAddress`, no
  `NotRouted`, no badge; the doc comments of `appTiles` and `NotRoutedBadge` (:382) say which tiles carry it.
- shell/web/templates/pages/apps.tmpl: after the sign-in block (:59-63),
  `{{ if $tile.NoAddress }}<div class="tile-no-address text-secondary">{{ $r.T "apps.no_address" }}</div>{{ end }}`.
- shell/web/translations/en.yml and fr.yml: add `apps.no_address`, delete `apps.not_routed.no_route_host` (:550).
- shell/preview/stats.go: one app with no address in the preview's app-intent and view, so the preview shows the
  line in both themes.

Proposed text (plain words, no app name, no label name):
- EN: `no_address: "No web address: this application works in the background and has no page to open."`
- FR: `no_address: "Pas d'adresse web : cette application travaille en arrière-plan et n'a pas de page à ouvrir."`
  (match the spacing before the colon the FR file uses at :550).

Tests:
- shell/apps/apps_test.go: drop `worker`/`no_route_host` from
  `TestAnAppThatGetsNoAddressIsAnAdminOnlyTileThatSaysWhy` (:148-182); add
  `TestAnAppThatDeclaresNoAddressIsABackgroundTileWithNoNotRoutedBadge` (admin-only, no URL, `NoAddress`, empty
  `NotRouted`, no `NotRoutedBadge`, health unknown, staff see none).
- shell/web/server_test.go: `TestTileBadgesRenderTranslated` (:440-475) uses `no_route_host` for the "not reachable"
  badge: switch that row to `main_route_unclear`; `TestEveryNotRoutedReasonRendersTranslated` (:480-523) drops
  `no_route_host` from `appReasons`; add `TestAnAppWithNoAddressSaysItRunsInTheBackground` (EN and FR sentence of
  `apps.no_address` present, neither not-routed sentence nor badge, no link, staff do not see it).
- shell/web/translations_test.go: no change (`TestEveryTranslationKeyIsRead` and parity cover the new key).

Gates (cwd /var/tmp/issues/473/catena-admin, after `CATENA_CE_SOURCE=../catena-ce bash scripts/vendor-catena-ce.sh`):

    flock /tmp/catena-gates.lock env GOCACHE=/tmp/claude-1000/gocache go test -count=1 -p 2 ./shell/apps/... ./shell/appintent/... ./shell/web/... ./shell/preview/...
    flock /tmp/catena-gates.lock env GOCACHE=/tmp/claude-1000/gocache go vet ./shell/apps/... ./shell/appintent/... ./shell/web/... ./shell/preview/...
    node ../contracts/scripts/check-prose.mjs
    node ../contracts/scripts/check-unicode.mjs
    node ../contracts/scripts/check-core-app-names.mjs --exclude central/ .

Commit message:

    fix(apps): show an app with no web address as a background app

    An application whose services declare no web address (a relay, a
    scanner) got the not-reachable badge and a line telling the admin to
    add a route label. Its tile now says that it works in the background,
    with no badge. The no_route_host code of the app-intent file is kept
    for its other readers.

### Step 2 [Open] catena-admin: the tile of an app with no web address shows its last verify (M)

Depends on step 1 (`NoAddress`).

Files:
- payload/engines/apps/hooks.go (or a new verified.go beside starting.go): `verifyPath(stack)`
  (`<StateRoot>/<stack>.verify`), `recordVerify(res VerifyResult, at time.Time)` (`writeAtomic`, 0600, after
  `MkdirAll(StateRoot, 0o700)` as starting.go:57-62), `readVerify(stack)` (nil when missing or unreadable),
  `clearVerify(stack)`; a failed write is told on stderr and never fails the verify.
- payload/engines/apps/hooks_cli.go: before `release()` (:65), record each result with `cfg.Now` when set
  (applock.go:125-128), else `time.Now`; the doc comment (:12-17) says so.
- payload/engines/apps/deploy.go: `Stop` (:204-210) and `Delete` (:421-426) clear the file beside `clearStarting`.
- payload/engines/apps/view.go: `ViewApp.Verify *ViewVerify` (`json:"verify,omitempty"`, `Verdict`, `At`) with its
  comment; `viewApp` (:199-260) fills it from `readVerify`. `ViewVersion` stays 1: the field is optional and the panel
  ships in the same image.
- shell/appview/appview.go: `App.Verify *Verify` (`json:"verify"`), `type Verify struct{ Verdict, At string }`,
  `VerifyMaxAge = 3 * time.Hour` with the reason from "Freshness bound", and `func (a App) Health(now time.Time)
  string` (healthy, unhealthy or unknown).
- shell/apps/apps.go: `MarkVerified(tiles []Tile, view appview.View, now time.Time)` sets `Health` on each tile with
  `NoAddress` from `view.Of(t.App).Health(now)`; the package comment (:1-5) names the view as a source.
- shell/web/server.go: call `apps.MarkVerified(tiles, view, time.Now())` beside `MarkStarting` (:1052).
- shell/preview/stats.go: the preview's app with no address gets `"verify": {"verdict": "ok", "at": now}`.
- payload/README.md (the `apps` row, :14): `catena-apps verify` keeps each verdict and its time beside the state
  (`<stack>.verify`) until the next one, Stop or Delete; the view lists the last verdict and when it ran.

Tests (style of the files beside them):
- payload/engines/apps/hooks_test.go: `TestVerifyKeepsEachVerdictWithItsTimeForTheView` (`run verify -stdin` and
  `--all` over an ok, a failing and a no-hook app: the file holds verdict and time, the view file lists them, a
  stack with no folder writes none); `TestTheUpdateLanesVerifyKeepsNoVerdict` (a direct `Verify` call writes no
  file); `TestAVerdictThatCannotBeWrittenDoesNotFailTheVerify` (StateRoot under a file, as starting_test.go:322).
- payload/engines/apps/apps_test.go or deploy tests: `TestStopAndDeleteForgetTheLastVerify`.
- payload/engines/apps/view_test.go: `TestTheViewNamesTheLastVerifyAndWhenItRan` (present after a verify, absent
  without one, absent after Stop).
- shell/appview/appview_test.go: `TestHealthReadsTheLastVerifyWithinThreeHours` (ok and fail inside the bound, ok
  past it, none, stopped, starting, no verify, an unparsable time).
- shell/apps/apps_test.go: `TestMarkVerifiedGivesABackgroundTileItsLastVerifyAndLeavesRoutedTilesAlone` (a routed
  tile keeps its Gatus health; a background tile goes healthy, unhealthy, unknown).
- shell/web/server_test.go: `TestABackgroundAppsDotIsItsLastVerify` (stats dir with app-intent and apps-view.json:
  `status-healthy` for a fresh ok, `status-unknown` for a stale one).

Gates (same cwd and vendor step as step 1):

    flock /tmp/catena-gates.lock env GOCACHE=/tmp/claude-1000/gocache go test -count=1 -p 2 ./payload/engines/apps/... ./payload/engines/stackupdate/... ./shell/appview/... ./shell/apps/... ./shell/web/... ./shell/preview/...
    flock /tmp/catena-gates.lock env GOCACHE=/tmp/claude-1000/gocache go vet ./payload/engines/apps/... ./shell/appview/... ./shell/apps/... ./shell/web/... ./shell/preview/...
    node ../contracts/scripts/check-prose.mjs
    node ../contracts/scripts/check-unicode.mjs
    node ../contracts/scripts/check-core-app-names.mjs --exclude central/ .

(stackupdate runs because it calls `apps.Verify`, whose behaviour must stay as it is.)

Commit message:

    feat(apps): give the tile of an app with no web address its last verify

    catena-apps verify keeps each application's verdict and when it ran in
    <stack>.verify beside its state, and the view file lists it. The tile
    of an application with no web address, which Gatus cannot probe, is
    green after a passing verify, red after a failing one, and grey when
    none ran in the last three hours. The update lane's own verify keeps
    no verdict, so a version it rolls back leaves no red dot.

### Step 3 [Open] catena-templates: the relay's page says where Talk lists it (S)

The Nextcloud verify check of the earlier draft is dropped: a failing verify rolls an update back
(catena-admin payload/engines/stackupdate/folders.go:199-216), so a check that fails when a person took the relay
out of Talk would roll back every Nextcloud update, against "updates never revert what a person changed". Talk's
own settings page is where a person sees the wiring (Nextcloud Talk docs, TURN page: "Nextcloud admin panel > Talk
settings", fields "STUN servers" and "TURN server").

One app (coturn), presentation class, no block field: `requires_catena` stays.

Files:
- blueprints/coturn/app.json: `en.setup_steps` and `fr.setup_steps`, step 3 gains one sentence:
  - EN: "In Nextcloud, the Talk page of the administration settings lists the relay among its STUN and TURN servers."
  - FR: "Dans Nextcloud, la page Talk des paramètres d'administration liste le relais parmi ses serveurs STUN et TURN."
- catalog.json and index.html: regenerated by `make render`.

Gates (cwd /var/tmp/issues/473/catena-templates, `uv sync` once):

    flock /tmp/catena-gates.lock make render
    flock /tmp/catena-gates.lock make lint
    flock /tmp/catena-gates.lock make test
    node ../contracts/scripts/check-prose.mjs
    node ../contracts/scripts/check-unicode.mjs

Commit message:

    docs(coturn): say where Nextcloud Talk lists the relay

    The setup steps name the Talk settings page, where a person sees the
    relay among Talk's STUN and TURN servers.

### Step 4 [Open] Bench, scenarios and docs: names to search (S)

ops is not cloned. Search ops `automation/test_bench` (`scenarios/`, `scenarios/_*.py`, `orchestrator/`,
`fixtures/`), contracts, catena-templates `blueprints/*/test.yml`, and the docs repo for:

| Name | Change |
|---|---|
| `not-routed`, `not reachable`, `Not reachable: no service of this app declares` | no longer on a tile of an app with no address |
| `apps.not_routed.no_route_host`, `Inaccessible : aucun service` | key and FR text removed |
| `apps.no_address`, `tile-no-address` | new key and class |
| `status-unknown` / `status-healthy` near a relay or scanner tile | the dot now follows the verify |
| `apps-view.json`, `starting_until` (strict parsers of the view) | new optional field `verify` |
| `/var/lib/catena/apps/` and `.starting` (state listings, wipe lists) | new file `<stack>.verify` |
| `Nextcloud answers and Apache serves status.php` | line kept without a relay, new line with one |
| `talk:turn`, `turn_wired` | verify now lists Talk's entries |

Search done (ops cloned at /var/tmp/issues/473/ops): no scenario asserts the tile of an app with no address or
`apps.not_routed.no_route_host`; app_several_addresses.py asserts `tile-not-routed` for route-level `catena_host`
(unchanged) and reads app-intent `no_route_host` (kept); orchestrator/app_deploy.py:520 reads apps-view.json
leniently (a new optional field passes); catalog_apps_read_green_in_gatus.py stage-1 expects every deployed catalog
app routed, so a run that deploys the relay or the scanner fails it before and after this change (follow-up issue).

Scenarios to read first: `admin_apps_tab_admin_full`, `admin_apps_tab_staff_filtered` (catena-ce
TEST_SCENARIOS.md:14-15), `host_network_app_ports` (stage-3-verify-hook-ok, catena-ce SPEC.md:515),
`apps_folder_round_trip` (stage-6-starting-until-ready, SPEC.md:517), and any scenario that wipes
`/var/lib/catena/apps`. The feature index (ops internal_docs/generated/audit/feature-grid.md) was not read, as ops
is not cloned: read its rows for the app verify and the Apps tiles before building.

Bench to prove it: `admin_apps_tab_admin_full` and `host_network_app_ports`, once the search confirms they assert
the tile or the verify; the Nextcloud change is proved by its app-tests verify leg.

## Proposed text for reserved paths

catena-ce SPEC.md, after the row at :516:

    | On the Apps page, an application with no web address says that it works in the background, and its status dot shows the last answer of its hourly verify command: green when it passed, red when it failed, grey when none came in the last three hours | **no bench yet** |

ops automation/audit/features.yml (shape to match the file's entries; ops not read): a feature "tile health of an
application with no web address" anchored on catena-admin shell/apps/apps.go `MarkVerified`,
shell/appview/appview.go `App.Health` and payload/engines/apps/hooks_cli.go `runVerify`, with no scenario yet; the
missing bench coverage becomes an ops issue.

## Out of scope

- Whether the relay template's setup steps work on a plain deploy (the issue's second question): not covered here.
- The signaling server entry Talk also carries: the verify does not check it.
- Health from the verify for routed apps: they keep Gatus.

## Decisions (coordinator, from the rules and the code)

1. The dot of an app with no web address reads the last verify kept on disk (a). The card rule allows a slow
   source (a scan, a check that runs a hook for up to a minute) to keep its own result for the cards to read; the
   verify file changes only when a verify runs. No dot (b) leaves the issue's grey-dot complaint open; a Gatus port
   probe (c) cannot probe the scanner and does not prove a relay grants a call.
2. Talk's relay entries: no verify check (Step 3). A failing verify rolls an update back
   (stackupdate/folders.go:199-216), so any answer that fails on a person's change in Talk blocks Nextcloud updates.
3. After a deploy the dot stays grey until the next hourly verify (a): grey is "not known yet", which is true; ending
   every deploy with a verify (b) makes each deploy wait for the hook.
