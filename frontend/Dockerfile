# Stage 1: Build React SPA
FROM node:20-alpine AS build

WORKDIR /app

# Copy dependency manifests and install dependencies cleanly
COPY package.json package-lock.json ./
RUN npm ci

# Copy frontend source code
COPY . .

# Set build-time VITE_API_BASE_URL (defaults to /api so nginx proxies to backend)
ARG VITE_API_BASE_URL=/api
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

# Build production distribution
RUN npm run build

# Stage 2: Serve with lightweight Nginx
FROM nginx:alpine

# Replace default nginx configuration with custom SPA reverse-proxy configuration
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy static assets from build stage
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
