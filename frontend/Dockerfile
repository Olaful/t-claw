FROM node:20-alpine

WORKDIR /app

COPY vue/package.json ./vue/
COPY ui/package.json ./ui/

RUN cd ui && npm install
RUN cd vue && npm install

COPY ui/ ./ui/
COPY vue/ ./vue/

EXPOSE 5173

CMD ["npm", "--prefix", "vue", "run", "dev", "--", "--host"]
