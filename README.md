# blog-pipeline

GUI de promoción artículo-a-artículo para [leonvillamayor.blog](https://leonvillamayor.blog).

**Estado**: Fase 1 (read-only dashboard). Ver `documentacion/plan_implantacion.md` del repo del blog para el roadmap.

## Modelo

- **Pending dev** → feature branches `drafts/<slug>` en `leonvillamayor/blog`. Acción: merge a `dev`.
- **In dev** → artículos en `origin/dev`. Acción: cherry-pick a `preprod` (artículo-level) o delete.
- **In preprod** → artículos en `origin/preprod`. Acción: cherry-pick a `main` o delete.
- **In prod** → artículos en `origin/main`. Acción: delete (cascade).

Cambios visuales/organizativos (layouts, schema, hugo.toml, doc) se promueven via los workflows existentes `promote-to-preprod.yml` / `promote-to-prod.yml`. La GUI los muestra en panel separado pero NO interviene.

## Arquitectura

```
┌─ pipeline.leonvillamayor.blog (CF Tunnel + Access OTP)
│       │
│       ▼
│   Caddy reverse proxy (LXC blog 10.0.30.103)
│       │
│       ▼
│   uvicorn :8000 ──► FastAPI app
│                        │
│                        ├── GitHub API (PAT fine-grained)
│                        ├── /opt/blog-pipeline/data/repo (clon local)
│                        └── Cloudflare API (cfut_ token)
```

## Desarrollo local

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# editar .env con tu PAT y rutas locales
uvicorn app.main:app --reload
# → http://127.0.0.1:8000
```

## Despliegue (LXC blog)

```bash
# 1. Crear usuario de servicio
useradd -r -s /bin/false -m -d /opt/blog-pipeline blog-pipeline

# 2. Clonar y montar venv
sudo -u blog-pipeline git clone https://github.com/leonvillamayor/blog-pipeline.git /opt/blog-pipeline
cd /opt/blog-pipeline
sudo -u blog-pipeline python3.12 -m venv .venv
sudo -u blog-pipeline .venv/bin/pip install -e .

# 3. Config + secretos
sudo install -d -m 0750 -o blog-pipeline -g blog-pipeline /etc/blog-pipeline
sudo install -m 0640 -o blog-pipeline -g blog-pipeline .env.example /etc/blog-pipeline/env
# editar /etc/blog-pipeline/env con valores reales

# 4. Systemd
sudo cp systemd/blog-pipeline.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now blog-pipeline.service

# 5. Cloudflare Tunnel
sudo apt install cloudflared
sudo cp tunnel/cloudflared.yml.example /etc/cloudflared/config.yml
# editar con TUNNEL_ID real
sudo cloudflared service install <TOKEN>

# 6. Caddy reverse proxy (añadir vhost a /etc/caddy/Caddyfile)
# Ver documentacion/caddy-vhost.example.txt
```

## Endpoints

| Path | Función |
|---|---|
| `GET /` | Dashboard HTML (HTMX) |
| `GET /api/state` | Estado JSON del pipeline |
| `GET /healthz` | Health check |

## License

MIT.
