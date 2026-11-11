Image findings accepted for 30 days sit in `.scanctl/baseline.sarif`; `scanctl accept` re-dates them after each scheduled run whose entries expire or whose pins moved. The mechanism is in scanctl's README, [Accepting image findings](https://github.com/catenahq/scanctl#accepting-image-findings).

## actualbudget

- `actualbudget/actual-server:26.9.0-alpine`: a newer tag carries fixes: 26.11.0-alpine, moving through https://github.com/catenahq/catena-templates/pull/192. Remaining: nodejs 24.18.1-r0 (fixed in 24.22.1-r0) and follow-redirects 1.16.0 (fixed in 1.16.1).

## chatwoot

- `chatwoot/chatwoot:v4.17.1-ce`: a newer tag carries fixes: v4.19.1-ce, moving through https://github.com/catenahq/catena-templates/pull/193. Remaining: pcre2 10.43-r0 (fixed in 10.43-r1) and libexpat 2.8.3-r0 (fixed in 2.10.0-r0).
- `pgvector/pgvector:0.8.7-pg18-trixie`: no fixed upstream image: it is the newest pg18 tag of its line (2026-11-06) and carries stdlib v1.24.6 (fixed in 1.25.13) and apt 3.0.3 (fixed in 3.0.4).

## collabora

- `collabora/code:26.04.5.2.1`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries dompurify 3.4.14 (fixed in 3.4.17).

## coturn

- `coturn/coturn:4.18.0-alpine`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries bind-libs 9.20.27-r0 (fixed in 9.20.29-r0) and libbson 1.30.6-r0 (fixed in 1.30.14-r0).

## docuseal

- `docuseal/docuseal:3.2.6`: a newer tag carries fixes: 3.3.5, moving through https://github.com/catenahq/catena-templates/pull/194. Remaining: libexpat 2.8.4-r0 (fixed in 2.10.0-r0) and actionpack 8.1.3.1 (fixed in 8.1.4.1).

## easyappointments

- `alextselegidis/easyappointments:1.6.1`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries linux-libc-dev 6.12.113-1 (fixed in 6.12.115-2) and apt 3.0.3 (fixed in 3.0.4).

## element

- `awesometechnologies/synapse-admin:0.11.4`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries libexpat 2.7.4-r0 (fixed in 2.10.0-r0) and brotli-libs 1.2.0-r0 (fixed in 1.2.1-r0).
- `jitsi/jigasi:stable-11031`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries apt 2.6.1 (fixed in 2.6.2) and libbrotli1 1.0.9-2+b6 (fixed in 1.0.9-2+deb12u1).
- `matrixdotorg/synapse:v1.161.0`: a newer tag carries fixes: v1.164.2, moving through https://github.com/catenahq/catena-templates/pull/195. Remaining: pillow 12.3.0 (fixed in 13.0.0) and Authlib 1.6.12 (fixed in 1.9.0).
- `vectorim/element-web:v1.12.33`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries perl 5.42.2-r1 (fixed in 5.42.3-r0) and zlib 1.3.2-r0 (fixed in 1.3.2-r1).

## erpnext

- `frappe/erpnext:v16.53.0`: a newer tag carries fixes: v16.54.0, moving through https://github.com/catenahq/catena-templates/pull/173. Remaining: stdlib v1.19 (fixed in 1.25.13) and brace-expansion 5.0.9 (fixed in 5.0.10).

## erpnext-helpdesk

- `ghcr.io/catenahq/erpnext-helpdesk:v16.53.0-1`: a newer tag carries fixes: v16.53.0-2, moving through https://github.com/catenahq/catena-templates/pull/174 (pin pull request https://github.com/catenahq/catena-templates/pull/186). Remaining: chromium-common 156.0.8078.47-1~deb13u1 (fixed in 156.0.8078.73-1~deb13u1) and stdlib v1.19 (fixed in 1.25.13).

## espocrm

- `espocrm/espocrm:10.0.11`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries linux-libc-dev 6.12.113-1 (fixed in 6.12.115-2) and phpoffice/phpspreadsheet 5.7.0 (fixed in 5.10.0).

## healthchecks

- `healthchecks/healthchecks:v5.0.3`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries apt 3.0.3 (fixed in 3.0.4) and oauthlib 4.0.0 (fixed in 4.0.1).

## immich

- `ghcr.io/immich-app/immich-machine-learning:v3.2.4`: a newer tag exists, v3.3.6, and no engine-bump pull request moves it yet. Remaining: pillow 12.3.0 (fixed in 13.0.0) and apt 2.6.1 (fixed in 2.6.2).
- `ghcr.io/immich-app/immich-server:v3.2.2`: a newer tag exists, v3.3.6, and no engine-bump pull request moves it yet. Remaining: undici 6.26.0 (fixed in 6.29.1) and apt 3.0.3 (fixed in 3.0.4).
- `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0`: a newer tag exists beyond the service's update label: 17-vectorchord0.4.3-pgvectors0.3.0. Remaining: apt 2.6.1 (fixed in 2.6.2) and stdlib v1.18.2 (fixed in 1.26.10).

## invoiceninja

- `invoiceninja/invoiceninja-debian:5.13.53`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries linux-libc-dev 6.12.113-1 (fixed in 6.12.115-2) and imagemagick-7-common 8:7.1.1.43+dfsg1-1+deb13u13 (fixed in 8:7.1.1.43+dfsg1-1+deb13u14).

## kimai

- `kimai/kimai2:2.67.0`: a newer tag carries fixes: 2.71.0, moving through https://github.com/catenahq/catena-templates/pull/196. Remaining: linux-libc-dev 6.1.180-1 (fixed in 6.1.190-1) and phpoffice/phpspreadsheet 2.4.7 (fixed in 2.4.8).

## mailserver

- `ghcr.io/docker-mailserver/docker-mailserver:16.0.1`: a newer tag exists, 17.0.0, and no engine-bump pull request moves it yet. Remaining: stdlib v1.26.4 (fixed in 1.26.10) and github.com/klauspost/compress v1.18.0 (fixed in 1.18.7).
- `roundcube/roundcubemail:1.7.5-apache`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries linux-libc-dev 6.12.113-1 (fixed in 6.12.115-2) and imagemagick-7-common 8:7.1.1.43+dfsg1-1+deb13u13 (fixed in 8:7.1.1.43+dfsg1-1+deb13u14).

## mautic

- `mautic/mautic:7.2.2-apache`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries undici 6.28.0 (fixed in 6.29.1) and apt 2.6.1 (fixed in 2.6.2).

## n8n

- `n8nio/n8n:2.41.7`: a newer tag carries fixes: 3.1.0, moving through https://github.com/catenahq/catena-templates/pull/197. Remaining: undici 6.28.0 (fixed in 6.29.1) and follow-redirects 1.16.0 (fixed in 1.16.1).

## nextcloud

- `ghcr.io/nextcloud-releases/aio-talk:20261029_171813`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries stdlib v1.26.1 (fixed in 1.26.10) and github.com/klauspost/compress v1.18.4 (fixed in 1.18.7).
- `nextcloud:34.0.5-apache`: a newer tag exists, 35.0.2-apache, and no engine-bump pull request moves it yet. Remaining: symfony/http-foundation v6.4.42 (fixed in 6.4.46).

## onlyoffice

- `onlyoffice/documentserver:9.4.0.1`: a newer tag carries fixes: 10.0.0.1, moving through https://github.com/catenahq/catena-templates/pull/198. Remaining: apt 2.8.3 (fixed in 2.8.4ubuntu0.1) and libbrotli1 1.1.0-2build2 (fixed in 1.1.0-2ubuntu0.1).

## outline

- `outlinewiki/outline:1.10.1`: a newer tag carries fixes: 1.12.0, moving through https://github.com/catenahq/catena-templates/pull/199. Remaining: undici 6.26.0 (fixed in 6.29.1) and @modelcontextprotocol/sdk 1.29.0 (fixed in 1.32.0).

## plane

- `makeplane/plane-admin:v1.4.3`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries libexpat 2.9.0-r0 (fixed in 2.10.0-r0) and curl 8.22.0-r0 (fixed in 8.23.0-r0).
- `makeplane/plane-backend:v1.4.3`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries djangorestframework 3.17.2 (fixed in 3.18.2) and PyJWT 2.13.0 (fixed in 2.14.0).
- `makeplane/plane-frontend:v1.4.3`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries libexpat 2.9.0-r0 (fixed in 2.10.0-r0) and curl 8.22.0-r0 (fixed in 8.23.0-r0).
- `makeplane/plane-live:v1.4.3`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries undici 7.29.0 (fixed in 6.29.1) and toml 4.3.0 (fixed in 5.0.1).
- `makeplane/plane-proxy:v1.4.3`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries brotli-libs 1.2.0-r0 (fixed in 1.2.1-r0) and stdlib v1.26.8 (fixed in 1.26.10).
- `makeplane/plane-space:v1.4.3`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries toml 4.3.0 (fixed in 5.0.1) and follow-redirects 1.16.0 (fixed in 1.16.1).
- `pgsty/silo:RELEASE.2026-09-16T00-00-00Z`: a newer tag exists beyond the service's update label: RELEASE.2026-10-26T00-00-00Z. Remaining: stdlib v1.27.1 (fixed in 1.27.3).

## portainer

- `portainer/portainer-ce:2.45.3`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries github.com/moby/buildkit v0.25.1 (fixed in 0.28.1).

## postiz

- `ghcr.io/gitroomhq/postiz-app:v2.24.0`: a newer tag carries fixes: v2.25.3, moving through https://github.com/catenahq/catena-templates/pull/165. Remaining: undici 6.28.1 (fixed in 6.29.1) and linux-libc-dev 6.1.187-1 (fixed in 6.1.190-1).
- `temporalio/admin-tools:1.31.4`: a newer tag carries fixes: 1.32.2, moving through https://github.com/catenahq/catena-templates/pull/165. Remaining: stdlib v1.26.8 (fixed in 1.26.9) and github.com/labstack/echo/v4 v4.15.4 (fixed in 4.16.1).
- `temporalio/server:1.31.4`: a newer tag carries fixes: 1.32.2, moving through https://github.com/catenahq/catena-templates/pull/165. Remaining: stdlib v1.26.8 (fixed in 1.26.9).

## rocketchat-oidc

- `docker.io/rocketchat/rocket.chat:8.8.1`: a newer tag carries fixes: 8.8.2 (8.9.1 is newer), moving through https://github.com/catenahq/catena-templates/pull/200. Remaining: body-parser 1.20.3 (fixed in 1.20.9) and node-forge 1.4.0 (fixed in 1.4.1).
- `mongo:8.3.13`: no fixed upstream image: it is the newest 8.3 tag of its line (2026-11-06) and carries apt 2.8.3 (fixed in 2.8.4ubuntu0.1) and stdlib v1.24.6 (fixed in 1.26.10).

## twenty

- `twentycrm/twenty:v2.39.5`: a newer tag exists, v2.56.1, and no engine-bump pull request moves it yet. Remaining: undici 7.29.0 (fixed in 6.29.1) and follow-redirects 1.16.0 (fixed in 1.16.1).

## umami

- `ghcr.io/umami-software/umami:3.4.1`: no fixed upstream image: it is the newest tag of its line (2026-11-10) and carries undici 6.28.0 (fixed in 6.29.1) and curl 8.22.0-r0 (fixed in 8.23.0-r0).

## windshift

- `ghcr.io/windshiftapp/windshift:v0.8.9`: a newer tag carries fixes: v0.9.3, moving through https://github.com/catenahq/catena-templates/pull/201. Remaining: stdlib v1.27.0 (fixed in 1.27.3).

## zammad

- `docker.elastic.co/elasticsearch/elasticsearch:8.19.23`: a newer tag exists, 8.19.24, and no engine-bump pull request moves it yet. Remaining: apt 2.8.3 (fixed in 2.8.4ubuntu0.1) and libbrotli1 1.1.0-2build2 (fixed in 1.1.0-2ubuntu0.1).
- `memcached:1.6.42-alpine3.23`: no fixed upstream image: it is the newest alpine3.23 tag of its line (2026-11-06) and carries zlib 1.3.2-r0 (fixed in 1.3.2-r1).
- `zammad/zammad:7.0.2-0000`: a newer tag carries fixes: 7.2.2-0043 (7.2.2-0045 is newer), moving through https://github.com/catenahq/catena-templates/pull/182. Remaining: libheif-plugin-dav1d 1.19.8-1 (fixed in 1.23.4-1~deb13u1) and nginx 1.26.3-3+deb13u6 (fixed in 1.26.3-3+deb13u10).

## Shared by several apps

- `jitsi/jicofo:stable-11031`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries apt 2.6.1 (fixed in 2.6.2) and libbrotli1 1.0.9-2+b6 (fixed in 1.0.9-2+deb12u1). Pinned by element, rocketchat-oidc.
- `jitsi/jvb:stable-11031`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries bind9-dnsutils 1:9.18.49-1~deb12u1 (fixed in 1:9.18.49-1~deb12u3) and apt 2.6.1 (fixed in 2.6.2). Pinned by element, rocketchat-oidc.
- `jitsi/prosody:stable-11031`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries apt 2.6.1 (fixed in 2.6.2) and libbrotli1 1.0.9-2+b6 (fixed in 1.0.9-2+deb12u1). Pinned by element, rocketchat-oidc.
- `jitsi/web:stable-11031`: no fixed upstream image: it is the newest tag of its line (2026-11-06) and carries bind9-dnsutils 1:9.18.49-1~deb12u1 (fixed in 1:9.18.49-1~deb12u3) and apt 2.6.1 (fixed in 2.6.2). Pinned by element, rocketchat-oidc.
- `mariadb:11.8.9`: no fixed upstream image: it is the newest 11.8 tag of its line (2026-11-06) and carries apt 2.8.3 (fixed in 2.8.4ubuntu0.1) and stdlib v1.24.6 (fixed in 1.26.10). Pinned by erpnext, erpnext-helpdesk.
- `mariadb:12.3.3`: no fixed upstream image: it is the newest 12.3 tag of its line (2026-11-06) and carries apt 2.8.3 (fixed in 2.8.4ubuntu0.1) and stdlib v1.24.6 (fixed in 1.26.10). Pinned by espocrm, kimai, mautic, wordpress.
- `mariadb:13.0.2`: no fixed upstream image: it is the newest 13.0 tag of its line (2026-11-06) and carries apt 3.2.0 (fixed in 3.2.1ubuntu0.1) and stdlib v1.24.6 (fixed in 1.26.10). Pinned by easyappointments, invoiceninja.
- `postgres:18.6-alpine`: no fixed upstream image: it is the newest 18 tag of its line (2026-11-06) and carries stdlib v1.24.6 (fixed in 1.26.10). Pinned by docuseal, element, n8n, nextcloud, outline, plane, postiz, twenty, windshift, zammad.
