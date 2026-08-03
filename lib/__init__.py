"""Build library for the Catena template catalog.

`sources/<id>.json` is canonical. Everything else in the repo root
(`templates.json`, `catalog.json`, `index.html`, `blueprints/`) is
generated from it by `lib.render` and gated for drift in CI.

Modules:
  model         -- load + validate sources/, expose the entry model
  render        -- write blueprints/ + templates.json + catalog.json + index.html
  quiesce_lint  -- security lint over the backup quiesce hooks
  postgres_pins -- central Postgres image pin enforcement
"""
