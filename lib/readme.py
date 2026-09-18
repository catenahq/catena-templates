"""Render one template's README from its source prose.

The README ships inside the blueprint directory, beside the compose file
Portainer deploys, so whoever reads the stackfile reads what it is for in
the same place. Both languages live in one file because the directory
holds one README per template.

Jinja is resolved to a placeholder before anything is written. The prose
and the env defaults are authored against the Ansible render that runs on
a managed host, and an expression that reached the README would read as
the literal value a reader is meant to type.
"""
from __future__ import annotations

import re

from .model import Entry

# Keyed by sso_mode. Two-tuple: (EN, FR).
SSO_LABEL: dict[str, tuple[str, str]] = {
    "pre-wired": (
        "Pre-wired. The login page shows \"Sign in with Keycloak\" out of "
        "the box, with no post-deploy step.",
        "Pré-câblé. La page de connexion affiche \"Se connecter avec "
        "Keycloak\" d'emblée, sans étape post-déploiement.",
    ),
    "post-deploy-ui": (
        "Enabled from the application's own admin screens: the `OIDC_*` "
        "values from the Environment tab are pasted in once.",
        "À activer depuis les écrans d'administration de l'application : "
        "les valeurs `OIDC_*` de l'onglet Environment y sont collées une "
        "fois.",
    ),
    "jackson-curl": (
        "Enabled by a one-time request to the bundled BoxyHQ Jackson "
        "broker. The exact command is in the setup steps below.",
        "À activer par une requête unique vers le broker BoxyHQ Jackson "
        "intégré. La commande exacte figure dans les étapes ci-dessous.",
    ),
    "auto": (
        "Wired automatically. The managed converge runs an idempotent "
        "hook that registers Keycloak inside the application on every "
        "pass, so there is no post-deploy step.",
        "Câblé automatiquement. La convergence gérée exécute un hook "
        "idempotent qui enregistre Keycloak dans l'application à chaque "
        "passage, sans étape post-déploiement.",
    ),
    "none": (
        "Not available. This application's community edition has no OIDC "
        "support, so each account keeps a per-application login.",
        "Non disponible. L'édition communautaire de cette application n'a "
        "pas de support OIDC : chaque compte garde un identifiant propre "
        "à l'application.",
    ),
    "n/a": (
        "Not applicable. This template has no sign-in screen "
        "(server-to-server use only).",
        "Sans objet. Ce modèle n'a pas d'écran de connexion (usage "
        "serveur à serveur uniquement).",
    ),
}

DOCS_SITE = "https://docs.catena.run"

_LOOKUP_RE = re.compile(r"\{\{\s*lookup\(\s*['\"]password['\"].*?\}\}", re.DOTALL)

# `{{ var | default(...) }}` collapses to `{{ var }}` first so the
# hostname map below matches a defaulted variable too.
_JINJA_DEFAULT_FILTER_RE = re.compile(
    r"\{\{\s*([a-zA-Z_]+)\s*\|\s*default\([^)]*\)\s*\}\}"
)
# Secret refs are recognised by NAME, not by prefix: the names carry no
# common prefix, and a prefix rule silently stops matching the day one is
# renamed, publishing the raw expression as the value to type.
_JINJA_SECRET_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+_(?:secret|password))\s*\}\}")

_JINJA_HOSTNAME_SUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\{\{\s*cloudflare_zone\s*\}\}"), "yourdomain.com"),
    (re.compile(r"\{\{\s*healthchecks_hostname\s*\}\}"), "checks.yourdomain.com"),
    (re.compile(r"\{\{\s*dokploy_admin_hostname\s*\}\}"), "admin.yourdomain.com"),
    (re.compile(r"\{\{\s*keycloak_hostname\s*\}\}"), "auth.yourdomain.com"),
    (re.compile(r"\{\{\s*gatus_hostname\s*\}\}"), "monitor.yourdomain.com"),
    (re.compile(r"\{\{\s*catena_admin_hostname\s*\}\}"), "dash.yourdomain.com"),
    (re.compile(r"\{\{\s*infrastructure_gatus_hostname\s*\}\}"),
     "monitor.yourdomain.com"),
    (re.compile(r"\{\{\s*infrastructure_dash_hostname\s*\}\}"), "dash.yourdomain.com"),
    (re.compile(r"\{\{\s*recovery_hostname\s*\}\}"), "recovery.yourdomain.com"),
    (re.compile(r"\{\{\s*nextcloud_hostname\s*\}\}"), "nextcloud.yourdomain.com"),
    (re.compile(r"\{\{\s*outline_hostname\s*\}\}"), "wiki.yourdomain.com"),
    (re.compile(r"\{\{\s*rocketchat_hostname\s*\}\}"), "chat.yourdomain.com"),
    (re.compile(r"\{\{\s*espocrm_hostname\s*\}\}"), "crm.yourdomain.com"),
    (re.compile(r"\{\{\s*coturn_hostname\s*\}\}"), "turn.yourdomain.com"),
    (re.compile(r"\{\{\s*keycloak_realm\s*\}\}"), "catena"),
    # The inventory's `public_ip` fact, resolved on the real host at
    # deploy time. The bundled Jitsi block in rocketchat-oidc advertises
    # it as its ICE candidate.
    (re.compile(r"\{\{\s*public_ip\s*\}\}"), "<your-server-public-ip>"),
)

# A README is read on GitHub, where a root-relative link resolves against
# github.com. Anything pointing into the docs site becomes absolute.
_ROOT_RELATIVE_LINK_RE = re.compile(r"(\]\(|href=[\"'])/(?!/)")


def strip_jinja(text: str) -> str:
    text = _JINJA_DEFAULT_FILTER_RE.sub(lambda m: f"{{{{ {m.group(1)} }}}}", text)
    for pattern, replacement in _JINJA_HOSTNAME_SUBS:
        text = pattern.sub(replacement, text)
    return _JINJA_SECRET_RE.sub(lambda m: f"<your-{m.group(1)}>", text)


def absolutize_links(text: str) -> str:
    return _ROOT_RELATIVE_LINK_RE.sub(lambda m: f"{m.group(1)}{DOCS_SITE}/", text)


def _env_table(env_defaults: list[str], locale: str) -> str:
    if not env_defaults:
        return ("_No environment variables to configure._" if locale == "en"
                else "_Aucune variable d'environnement à configurer._")
    rows = ["| Variable | Default |\n|---|---|" if locale == "en"
            else "| Variable | Valeur par défaut |\n|---|---|"]
    for line in env_defaults:
        if "=" not in line:
            continue
        key, raw = line.split("=", 1)
        value = _LOOKUP_RE.sub("<auto-generated random value>", raw)
        if value == "":
            cell = ("_set before deploy_" if locale == "en"
                    else "_à définir avant déploiement_")
        elif "<auto-generated" in value:
            cell = ("_auto-generated random value_" if locale == "en"
                    else "_valeur aléatoire auto-générée_")
        else:
            cell = f"`{value}`"
        rows.append(f"| `{key}` | {cell} |")
    return "\n".join(rows)


def _section(entry: Entry, locale: str) -> str:
    prose = entry.prose(locale)
    catena = entry.catena
    domain = catena["domain"]
    sso = SSO_LABEL.get(catena["sso_mode"], ("?", "?"))[0 if locale == "en" else 1]
    replaces = ", ".join(f"**{name}**" for name in prose["replaces"])
    extra = prose.get("extra_notes", "").rstrip()

    if locale == "en":
        body = f"""## English

{prose['what_it_is']}

- **Upstream project:** <{catena['upstream_url']}>
- **Replaces:** {replaces or "_nothing in particular_"}
- **Sign-in (SSO):** {sso}
- **Address:** `{domain['host']}`, served from `{domain['service']}:{domain['port']}`

The address is attached when the template is deployed. A different one is
arranged beforehand, on request.

### Setup steps

{prose['setup_steps'].rstrip()}
"""
        if extra:
            body += f"\n{extra}\n"
        body += f"""
### Environment variables

These are the fields the deploy form presents in the **App Templates**
panel. Random secrets are minted on the host when the template is first
seeded, so none of them has to be generated by hand.

{_env_table(catena['env_defaults'], 'en')}
"""
        return body

    body = f"""## Français

{prose['what_it_is']}

- **Projet original :** <{catena['upstream_url']}>
- **Remplace :** {replaces or "_rien en particulier_"}
- **Connexion (SSO) :** {sso}
- **Adresse :** `{domain['host']}`, servie par `{domain['service']}:{domain['port']}`

L'adresse est attachée au déploiement du modèle. Une autre se convient au
préalable, sur demande.

### Étapes de configuration

{prose['setup_steps'].rstrip()}
"""
    if extra:
        body += f"\n{extra}\n"
    body += f"""
### Variables d'environnement

Ce sont les champs présentés par le formulaire de déploiement du panneau
**App Templates**. Les secrets aléatoires sont générés sur l'hôte au
premier semis du modèle : aucun n'est à produire à la main.

{_env_table(catena['env_defaults'], 'fr')}
"""
    return body


def render_readme(entry: Entry) -> str:
    """The template's README, EN then FR, as one file."""
    files = [f"`{entry.compose_path.name}` -- the stack file this template deploys"]
    if entry.quiesce:
        files.append(
            "`quiesce.yml` -- the pause and resume hooks the backup chain "
            "runs around a snapshot of this application"
        )

    parts = [
        f"<!-- Generated by lib/render.py from sources/{entry.slug}.json. "
        f"Do not edit; `make` regenerates it and CI fails on drift. -->",
        "",
        f"# {entry.raw['title']}",
        "",
        *(f"- {line}" for line in files),
        "",
        _section(entry, "en").rstrip(),
        "",
        "---",
        "",
        _section(entry, "fr").rstrip(),
        "",
    ]
    return absolutize_links(strip_jinja("\n".join(parts)))
