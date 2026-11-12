Image findings accepted for 30 days sit in `.scanctl/baseline.sarif`; `scanctl accept` re-dates them after each scheduled run whose entries expire or whose pins moved. The mechanism is in scanctl's README, [Accepting image findings](https://github.com/catenahq/scanctl#accepting-image-findings).

## beszel

- `henrygd/beszel-agent:0.21.1-alpine@sha256:4100e26f7b5e6ab89e73699b2366417795f5046ab36bc452daf1f105766b40a0`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries krb5-libs 1.22.2-r1 (fixed in 1.22.3-r0) and golang.org/x/net v0.61.0 (fixed in 0.62.0).
- `henrygd/beszel:0.21.1`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries golang.org/x/net v0.61.0 (fixed in 0.62.0).

## chatwoot

- `chatwoot/chatwoot:v4.19.1-ce`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries musl-utils 1.2.5-r9 (fixed in 1.2.5-r12) and rack 3.2.6 (fixed in 3.2.8).
- `pgvector/pgvector:0.8.7-pg18-trixie`: no fixed upstream image: it is the newest pg18 tag of its line (2026-11-12) and carries stdlib v1.24.6 (fixed in 1.26.10) and apt 3.0.3 (fixed in 3.0.4).

## collabora

- `collabora/code:26.04.5.2.1`: a newer tag exists, 26.04.6.1.1, and no engine-bump pull request moves it yet. Remaining: dompurify 3.4.14 (fixed in 3.4.17).

## coturn

- `coturn/coturn:4.18.0-alpine`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries bind-libs 9.20.27-r0 (fixed in 9.20.30-r0) and libbson 1.30.6-r0 (fixed in 1.30.14-r0).

## easyappointments

- `alextselegidis/easyappointments:1.6.1`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries linux-libc-dev 6.12.113-1 (fixed in 6.12.115-2) and apt 3.0.3 (fixed in 3.0.4).

## element

- `awesometechnologies/synapse-admin:0.11.4`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries libexpat 2.7.4-r0 (fixed in 2.10.0-r0) and brotli-libs 1.2.0-r0 (fixed in 1.2.1-r0).
- `jitsi/jigasi:stable-11031`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries apt 2.6.1 (fixed in 2.6.2) and libbrotli1 1.0.9-2+b6 (fixed in 1.0.9-2+deb12u1).
- `matrixdotorg/synapse:v1.165.0`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries zlib1g 1:1.3.dfsg+really1.3.1-1+b1 (fixed in 1:1.3.dfsg+really1.3.1-1+deb13u1).
- `vectorim/element-web:v1.12.34`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries musl 1.2.6-r2 (fixed in 1.2.6-r3).

## erpnext-helpdesk

- `ghcr.io/catenahq/erpnext-helpdesk:v16.53.0-2`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries libminizip1t64 1:1.3.dfsg+really1.3.1-1+b1 (fixed in 1:1.3.dfsg+really1.3.1-1+deb13u1).

## espocrm

- `espocrm/espocrm:10.1.1`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries symfony/http-client v7.4.3 (fixed in 7.4.23).

## gatus

- `twinproduction/gatus:v5.38.0`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries golang.org/x/net v0.58.0 (fixed in 0.62.0).

## healthchecks

- `healthchecks/healthchecks:v5.0.3`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries apt 3.0.3 (fixed in 3.0.4) and oauthlib 4.0.0 (fixed in 4.0.1).

## immich

- `ghcr.io/immich-app/immich-machine-learning:v3.2.4`: a newer tag exists, v3.4.1, and no engine-bump pull request moves it yet. Remaining: pillow 12.3.0 (fixed in 13.0.0) and apt 2.6.1 (fixed in 2.6.2).
- `ghcr.io/immich-app/immich-server:v3.2.2`: a newer tag exists, v3.4.1, and no engine-bump pull request moves it yet. Remaining: undici 6.26.0 (fixed in 6.29.1) and apt 3.0.3 (fixed in 3.0.4).
- `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0`: a newer tag exists beyond the service's update label: 17-vectorchord0.4.3-pgvectors0.3.0. Remaining: apt 2.6.1 (fixed in 2.6.2) and stdlib v1.18.2 (fixed in 1.26.10).

## invoiceninja

- `invoiceninja/invoiceninja-debian:5.13.53`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries linux-libc-dev 6.12.113-1 (fixed in 6.12.115-2) and imagemagick-7-common 8:7.1.1.43+dfsg1-1+deb13u13 (fixed in 8:7.1.1.43+dfsg1-1+deb13u14).

## kimai

- `kimai/kimai2:2.71.0`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries symfony/http-client v6.4.48 (fixed in 6.4.50).

## mailserver

- `ghcr.io/docker-mailserver/docker-mailserver:16.0.1`: a newer tag exists, 17.0.0, and no engine-bump pull request moves it yet. Remaining: stdlib v1.26.4 (fixed in 1.26.10) and github.com/klauspost/compress v1.18.0 (fixed in 1.18.7).

## mautic

- `mautic/mautic:7.2.2-apache`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries undici 6.28.0 (fixed in 6.29.1) and apt 2.6.1 (fixed in 2.6.2).

## n8n

- `n8nio/n8n:3.2.0`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries openssh 10.3_p1-r1 (fixed in 10.3_p1-r2).

## nextcloud

- `ghcr.io/nextcloud-releases/aio-talk:20261029_171813`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries stdlib v1.26.1 (fixed in 1.26.10) and github.com/klauspost/compress v1.18.4 (fixed in 1.18.7).
- `nextcloud:35.0.2-apache`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries libopenexr-3-1-30 3.1.13-2 (fixed in 3.2.12-1~deb13u1) and zlib1g 1:1.3.dfsg+really1.3.1-1+b1 (fixed in 1:1.3.dfsg+really1.3.1-1+deb13u1).

## outline

- `outlinewiki/outline:1.12.0`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries zlib1g 1:1.3.dfsg+really1.3.1-1+b1 (fixed in 1:1.3.dfsg+really1.3.1-1+deb13u1).

## plane

- `makeplane/plane-admin:v1.4.3`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries libexpat 2.9.0-r0 (fixed in 2.10.0-r0) and curl 8.22.0-r0 (fixed in 8.23.0-r0).
- `makeplane/plane-backend:v1.4.3`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries djangorestframework 3.17.2 (fixed in 3.18.2) and PyJWT 2.13.0 (fixed in 2.14.0).
- `makeplane/plane-frontend:v1.4.3`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries libexpat 2.9.0-r0 (fixed in 2.10.0-r0) and curl 8.22.0-r0 (fixed in 8.23.0-r0).
- `makeplane/plane-live:v1.4.3`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries undici 7.29.0 (fixed in 7.30.1) and toml 4.3.0 (fixed in 5.0.1).
- `makeplane/plane-proxy:v1.4.3`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries brotli-libs 1.2.0-r0 (fixed in 1.2.1-r0) and stdlib v1.26.8 (fixed in 1.26.10).
- `makeplane/plane-space:v1.4.3`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries toml 4.3.0 (fixed in 5.0.1) and follow-redirects 1.16.0 (fixed in 1.16.1).
- `pgsty/silo:RELEASE.2026-09-16T00-00-00Z`: a newer tag exists beyond the service's update label: RELEASE.2026-11-09T00-00-00Z. Remaining: stdlib v1.27.1 (fixed in 1.27.3) and golang.org/x/net v0.59.0 (fixed in 0.62.0).

## portainer

- `portainer/portainer-ce:2.45.3`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries github.com/moby/buildkit v0.25.1 (fixed in 0.28.1) and github.com/docker/cli v28.5.1+incompatible (fixed in 29.2.0).

## postiz

- `ghcr.io/gitroomhq/postiz-app:v2.25.3`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries node-forge 1.4.0 (fixed in 1.4.1).
- `temporalio/admin-tools:1.32.2`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries golang.org/x/net v0.60.0 (fixed in 0.62.0).
- `temporalio/server:1.32.2`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries golang.org/x/net v0.60.0 (fixed in 0.62.0).

## rocketchat-oidc

- `docker.io/rocketchat/rocket.chat:8.8.2`: a newer tag exists, 9.0.0, and no engine-bump pull request moves it yet. Remaining: node-forge 1.4.0 (fixed in 1.4.1) and moleculer 0.14.35 (fixed in 0.14.36).
- `mongo:8.3.13`: a newer tag exists, 8.3.17, and no engine-bump pull request moves it yet. Remaining: apt 2.8.3 (fixed in 2.8.4ubuntu0.1) and stdlib v1.24.6 (fixed in 1.26.10).

## twenty

- `twentycrm/twenty:v2.39.5`: a newer tag exists, v2.57.1, and no engine-bump pull request moves it yet. Remaining: undici 7.29.0 (fixed in 7.30.1) and follow-redirects 1.16.0 (fixed in 1.16.1).

## umami

- `ghcr.io/umami-software/umami:3.4.1`: a newer tag exists, 3.5.0, and no engine-bump pull request moves it yet. Remaining: undici 6.28.0 (fixed in 6.29.1) and curl 8.22.0-r0 (fixed in 8.23.0-r0).

## windshift

- `ghcr.io/windshiftapp/windshift:v0.9.3`: a newer tag exists, v0.9.4, and no engine-bump pull request moves it yet. Remaining: golang.org/x/net v0.60.0 (fixed in 0.62.0).

## zammad

- `memcached:1.6.42-alpine3.23`: no fixed upstream image: it is the newest alpine3.23 tag of its line (2026-11-12) and carries zlib 1.3.2-r0 (fixed in 1.3.2-r1) and libcrypto3 3.5.6-r0 (fixed in 3.5.8-r0).

## Shared by several apps

- `jitsi/jicofo:stable-11031`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries apt 2.6.1 (fixed in 2.6.2) and libbrotli1 1.0.9-2+b6 (fixed in 1.0.9-2+deb12u1). Pinned by element, rocketchat-oidc.
- `jitsi/jvb:stable-11031`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries bind9-dnsutils 1:9.18.49-1~deb12u1 (fixed in 1:9.18.49-1~deb12u3) and apt 2.6.1 (fixed in 2.6.2). Pinned by element, rocketchat-oidc.
- `jitsi/prosody:stable-11031`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries apt 2.6.1 (fixed in 2.6.2) and libbrotli1 1.0.9-2+b6 (fixed in 1.0.9-2+deb12u1). Pinned by element, rocketchat-oidc.
- `jitsi/web:stable-11031`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries bind9-dnsutils 1:9.18.49-1~deb12u1 (fixed in 1:9.18.49-1~deb12u3) and apt 2.6.1 (fixed in 2.6.2). Pinned by element, rocketchat-oidc.
- `mariadb:11.8.9`: no fixed upstream image: it is the newest 11.8 tag of its line (2026-11-12) and carries apt 2.8.3 (fixed in 2.8.4ubuntu0.1) and stdlib v1.24.6 (fixed in 1.26.10). Pinned by erpnext, erpnext-helpdesk.
- `mariadb:12.3.3`: no fixed upstream image: it is the newest 12.3 tag of its line (2026-11-12) and carries apt 2.8.3 (fixed in 2.8.4ubuntu0.1) and stdlib v1.24.6 (fixed in 1.26.10). Pinned by espocrm, kimai, mautic, wordpress.
- `mariadb:13.0.2`: no fixed upstream image: it is the newest 13.0 tag of its line (2026-11-12) and carries apt 3.2.0 (fixed in 3.2.1ubuntu0.1) and stdlib v1.24.6 (fixed in 1.26.10). Pinned by easyappointments, invoiceninja.
- `nginx:1.31.8-alpine`: no fixed upstream image: it is the newest tag of its line (2026-11-12) and carries libxslt 1.1.43-r3 (fixed in 1.1.43-r4). Pinned by invoiceninja, mailserver, wordpress.
- `postgres:18.6-alpine`: no fixed upstream image: it is the newest 18 tag of its line (2026-11-12) and carries stdlib v1.24.6 (fixed in 1.26.10) and krb5-libs 1.22.2-r1 (fixed in 1.22.3-r0). Pinned by docuseal, element, n8n, nextcloud, outline, plane, postiz, twenty, umami, windshift, zammad.
