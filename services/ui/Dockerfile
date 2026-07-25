FROM node:22.14.0-alpine

WORKDIR /app
COPY package.json pnpm-lock.yaml* pnpm-workspace.yaml* ./
RUN npm install -g pnpm@11.9.0 && pnpm install --frozen-lockfile=false
COPY . .
EXPOSE 5173
CMD ["pnpm", "dev", "--host", "0.0.0.0"]
