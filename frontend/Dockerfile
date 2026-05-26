# =============================================================================
# Frontend image — SIM-401
#
# Multi-stage: build the Vite/React app, then serve the static bundle from
# nginx using the shared SIM-381 config (which also proxies /api, /ws, /auth,
# /docs to the `baseball_app` upstream and SPA-falls-back to index.html).
#
# Build context is the REPO ROOT (the runtime stage copies the shared
# deploy/nginx/nginx.conf), so build with:
#   docker build -f frontend/Dockerfile -t ghcr.io/<org>/<repo>-frontend .
# =============================================================================

# ---- Stage 1: build the static bundle ----
FROM node:20-alpine AS build
WORKDIR /app

# Install deps against the lockfile first (better layer caching).
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# Build the app (tsc --noEmit && vite build → /app/dist).
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: serve via nginx ----
FROM nginx:1.27-alpine AS runtime

# The SPA bundle at the web root the nginx.conf serves (root /var/www/baseball-sim).
COPY --from=build /app/dist /var/www/baseball-sim
# The shared reverse-proxy + SPA-fallback config (SIM-381).
COPY deploy/nginx/nginx.conf /etc/nginx/nginx.conf

EXPOSE 80

# nginx.conf serves /health via the app upstream; a container-level check that
# the static root is being served avoids coupling liveness to the backend.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://localhost:80/index.html || exit 1

CMD ["nginx", "-g", "daemon off;"]
