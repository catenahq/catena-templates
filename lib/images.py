"""The image refs the blueprints pin, and the ones a change moves."""
from __future__ import annotations

import yaml


def service_images(compose_text: str) -> dict[str, str]:
    """Map each service of one compose file to its image ref."""
    doc = yaml.safe_load(compose_text)
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
        return {}
    out: dict[str, str] = {}
    for name, svc in doc["services"].items():
        if isinstance(svc, dict) and isinstance(svc.get("image"), str) and svc["image"].strip():
            out[str(name)] = svc["image"].strip()
    return out


def changed_images(head: dict[str, str], base: dict[str, str]) -> list[dict[str, str]]:
    """Pair every image a change moves with the one it replaces.

    head and base map a template id to its compose text on either side. A
    service absent at the base pairs with "": there is nothing it replaces,
    so its scan is graded on its own. One entry per distinct pair, sorted.
    """
    pairs: set[tuple[str, str]] = set()
    for tid, text in head.items():
        before = service_images(base[tid]) if tid in base else {}
        for service, image in service_images(text).items():
            if before.get(service) != image:
                pairs.add((image, before.get(service, "")))
    return [{"image": new, "base_image": old} for new, old in sorted(pairs)]
