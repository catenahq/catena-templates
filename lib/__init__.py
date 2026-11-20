"""Build library for the Catena template catalog.

An app's card file (`blueprints/<id>/app.json`) and its compose are
canonical. The generated files (each blueprint's packed compose and placeholder
logo) come from them by `lib.render` and are gated for drift in CI. The catalog
document is printed by `build/catalog.py` and written nowhere.

Modules:
  model         -- load + validate the metadata files and composes, expose the entry model
  render        -- write blueprints/; build the catalog document and the preview page
  quiesce_lint  -- security lint over the backup quiesce hooks
  postgres_pins -- Postgres pins the update engine can order
  swarm_lint    -- swarm stack file compatibility gate
  changes       -- changed paths -> the app ids they touch
  provenance    -- an image's project: age and releases
  app_checks    -- the per-app app-ci checks' commands
  versions      -- app version tags and the one-app scope check
  importers     -- write untested entries from a third-party registry
"""
