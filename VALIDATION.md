<!-- AUTOGEN by `audit --write-public-specs` from the maintainers'
     audit manifest (features.yml) crossed with the live rehearsal-
     scenario mapping and this repository's CI workflows. Do not
     hand-edit; the maintainers' CI regenerates and fails on any
     drift (`audit --check-public-specs`). -->


# Validation

This catalog is validated on two axes: its own CI gates below (render idempotency, image scanning, content hygiene), and end-to-end deployment -- the templates are installed and exercised on disposable virtual machines by the Catena rehearsal suite, summarized in the [catena-ce validation sheet](https://github.com/catenahq/catena-ce/blob/main/VALIDATION.md).

## Continuous integration gates on this repository

Parsed from the committed workflow files; every job below runs on each change.

| Workflow | File | Jobs |
| --- | --- | --- |
| build-and-verify | `.github/workflows/build-and-verify.yml` | `render-idempotency`, `unicode-hygiene`, `lint-quiesce-tests` |
| security | `.github/workflows/security.yml` | `scanctl` |
| Trivy images (catalog) | `.github/workflows/trivy-images.yml` | `list-images`, `trivy-image` |

See [SPEC.md](SPEC.md) for what this repository promises and the machine-checked invariants behind each promise.
