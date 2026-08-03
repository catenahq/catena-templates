# Serves the generated catalog over HTTP.
#
# The canonical URL a client's Portainer reads is
# raw.githubusercontent.com. This image exists for the two cases that
# cannot use it: an air-gapped or egress-restricted host that mirrors the
# catalog internally, and a local preview of a catalog change before it
# is tagged.
#
#   docker build -t catena-templates:local .
#   docker run --rm -p 8080:80 catena-templates:local
#   # then point Portainer's App Templates URL at
#   # http://<host>:8080/templates.json
#
# Only generated artifacts are copied in: sources/ is the build input and
# has no business being served.

FROM nginx:1.29-alpine

COPY templates.json /usr/share/nginx/html/templates.json
COPY catalog.json /usr/share/nginx/html/catalog.json
COPY index.html /usr/share/nginx/html/index.html
COPY blueprints /usr/share/nginx/html/blueprints

# Portainer fetches templates.json cross-origin from the browser session
# that is configuring it, so the mirror has to say so explicitly.
RUN printf '%s\n' \
    'server {' \
    '  listen 80;' \
    '  root /usr/share/nginx/html;' \
    '  add_header Access-Control-Allow-Origin "*";' \
    '  location / { autoindex on; }' \
    '}' > /etc/nginx/conf.d/default.conf

EXPOSE 80
