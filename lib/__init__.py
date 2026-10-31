"""Build library for the Catena template catalog.

An app's metadata file (`blueprints/<id>/app.json` or `sources/<id>.json`) and
its compose are canonical. The generated files (`templates.json`, `catalog.json`,
`index.html`, each blueprint's README and placeholder logo) come from them by
`lib.render` and are gated for drift in CI.

Modules:
  model         -- load + validate the metadata files and composes, expose the entry model
  render        -- write blueprints/ + templates.json + catalog.json + index.html
  readme        -- one template's README from its prose
  quiesce_lint  -- security lint over the backup quiesce hooks
  postgres_pins -- Postgres pins the update engine can order
  swarm_lint    -- swarm stack file compatibility gate
  changes       -- changed paths -> the app ids they touch
  provenance    -- an image's project: age and releases
  app_checks    -- the per-app app-ci checks' commands
  versions      -- app version tags and the one-app scope check
  importers     -- write untested entries from a third-party registry
"""
