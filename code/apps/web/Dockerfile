FROM node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
# 国内镜像源加速
RUN npm config set registry https://registry.npmmirror.com && npm install
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
RUN printf 'server {\n  listen 80;\n  root /usr/share/nginx/html;\n  index index.html;\n  client_max_body_size 50m;\n  # 用 Docker 内置 DNS，10s 刷新，避免容器重建后 IP 变化导致 502\n  resolver 127.0.0.11 valid=10s ipv6=off;\n  location /api/ { proxy_pass http://api:8000/api/; proxy_set_header Host $host; proxy_buffering off; proxy_read_timeout 300s; }\n  location / { try_files $uri /index.html; }\n}' > /etc/nginx/conf.d/default.conf
EXPOSE 80
