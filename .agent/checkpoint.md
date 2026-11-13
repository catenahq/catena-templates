# Checkpoint: ops issue 473

## Done

- Read the job, runbook, catena-templates AGENTS.md, the coturn blueprint, the panel code path of the message.
- Cloned catena-admin and catena-ce (read only) under /var/tmp/issues/473/.

## Cause (proven)

- The catalog contract says an application with no web address (a scanner, a relay) has no service labelled
  vps.route.host: catena-templates/AGENTS.md:57-58. coturn follows it (blueprints/coturn/services.yml has no
  vps.route.host; x-catena.yml names version_from). clamav has the same shape.
- The app checker accepts an app with no route: catena-admin payload/engines/applint/catena.go k3/k4 raise no error
  for zero routes.
- dashboard-sync marks every app with no address not_routed=no_route_host: catena-admin
  payload/lib/app_intent.py:320-321.
- The panel turns any not_routed into an admin-only tile with the not-routed badge ("not reachable") and the line
  apps.not_routed.no_route_host: catena-admin shell/apps/apps.go:432-434, shell/web/translations/en.yml:546,550,
  shell/web/templates/pages/apps.tmpl:64-66. The text names a label to add, which is wrong for a relay.
- The dot is grey because a tile's health comes only from Gatus by host (shell/apps/apps.go:443-445); an app with
  no address has no Gatus endpoint, so Health stays "unknown".
- The verify hook (coturn's asks turnserver for a real TURN allocation) runs hourly
  (catena-ce ansible/reconcile/roles/host_maintenance/README.md:118, catena-app-verify.timer) but its verdict is
  kept nowhere the panel reads: payload/engines/apps/hooks.go Verify only reports failures to the maintenance feed.
- Nextcloud wires Talk to the relay in blueprints/nextcloud/hooks/configure.sh:274-293 (wire_turn) but its verify
  (hooks/verify.sh) does not check it; element's verify.py:21-26 does check its relay wiring.

Verdict: the panel is at fault, not coturn's declaration.

## Hypotheses

1. coturn's declaration lacks a label: ruled out by AGENTS.md:57-58 and applint accepting zero routes.
2. Panel treats "no address" as a fault and has no health source for it: confirmed (above).

3. Nextcloud verify should check Talk's relay entries: dropped, a failing verify rolls an update back
   (catena-admin payload/engines/stackupdate/folders.go:199-216), so a person's change in Talk would block updates.

## Plan

- .agent/plan.md written by fleet:planner-researcher, revised by the coordinator (Step 3 now coturn's setup steps;
  questions settled under "Decisions").
- catena-admin clone on branch agent/473-coturn-shows-not-reachable-no-service-of; ops cloned read only for the
  bench search (no scenario asserts the no-address tile).

## Next

- Step 1 committed in catena-admin (fix(apps): show an app with no web address as a background app), reviewed.
- Step 3 committed in catena-templates (docs(coturn): say where Nextcloud Talk lists the relay), reviewed, make
  render/lint/test (1945 passed), prose and unicode clean.
- Step 2 committed in catena-admin 4ccb919e after review; the review's race on --all (verdicts recorded after
  VerifyAll released stack locks) and the view.go comment are fixed. Go tests of payload/engines/apps,
  stackupdate, shell/appview, apps, web, preview pass; vet clean.
- All steps done; result.json written. Follow-up for the owner: ops catalog_apps_read_green_in_gatus stage-1
  expects every deployed catalog app routed, so a run that deploys the relay or the scanner fails it.

## Commands that work (Go)

- `flock /tmp/catena-gates.lock env GOCACHE=/tmp/claude-1000/gocache go test -C /var/tmp/issues/473/catena-admin -count=1 -p 2 ./shell/apps/...`
  (a cd does not persist between Bash calls).

## Commands that work

- token-savior projects: /var/tmp/issues/473/catena-admin, /var/tmp/issues/473/catena-templates,
  /var/tmp/issues/473/catena-ce
