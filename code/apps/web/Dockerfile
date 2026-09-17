FROM node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
# 国内镜像源加速
RUN npm config set registry https://registry.npmmirror.com && npm install
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
# nginx 配置：proxy_pass 用变量形式触发 resolver 动态解析，避免 api 容器重启后 IP 变化导致 502
# 不带 URI 后缀，否则 nginx 会用 /api/ 替换请求路径导致 404
RUN printf 'server {\n  listen 80;\n  root /usr/share/nginx/html;\n  index index.html;\n  client_max_body_size 50m;\n  resolver 127.0.0.11 valid=10s ipv6=off;\n  set $api_backend api:8000;\n  location /api/ { proxy_pass http://$api_backend; proxy_set_header Host $host; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Real-IP $remote_addr; proxy_buffering off; proxy_read_timeout 300s; }\n  location / { try_files $uri /index.html; }\n}' > /etc/nginx/conf.d/default.conf
EXPOSE 80
