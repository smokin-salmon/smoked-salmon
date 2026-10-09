# salmon web front end

The browser side of `salmon web`: Svelte 5 and Vite. Its build is committed in `src/salmon/webui/static/`, so
salmon installs and the Docker image carry it without Node; Node is needed only to change it.

```bash
cd webui
npm ci --ignore-scripts   # the Node version is in .node-version
npm run check             # svelte-check and tsc
npm run build             # writes src/salmon/webui/static/
```

Commit the rebuilt `src/salmon/webui/static/` with any change here: CI builds it again from the committed
source and lockfile, and fails if the result differs.

To work on it with live reload, run `salmon web` and `npm run dev`, then open http://localhost:5173 and log in
with the printed token. Vite passes `/api` on to `salmon web` on port 55155. `salmon web --dev` also accepts
requests sent from that page straight to port 55155 (CORS).
