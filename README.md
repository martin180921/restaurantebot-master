# OLO — plataforma tecnológica para el pequeño comercio

Sistema para restaurantes independientes: pedidos por WhatsApp, POS en mesa, cocina e impresión de comandas. Este repositorio es el producto; la estrategia, la marca y los backlogs viven en el hub **OLO** de Notion.

> **One Thing:** hacer que administrar un pequeño negocio sea simple. Si una función no simplifica la vida del cliente, no entra.

## Componentes

| Carpeta | Servicio | Tecnología | Corre en |
|---|---|---|---|
| `whatsapp_bot/` | Bot de pedidos por WhatsApp | FastAPI + Twilio | Railway |
| `dashboard_admin/` | Panel: POS, cocina, caja, menú, empleados | Streamlit | Railway |
| `app_cliente/` | Carta digital / pedidos web del comensal | Streamlit | Railway |
| `print_agent/` | Agente de impresión de comandas (impresora térmica) | Python | PC del restaurante |
| `scripts/` | Flota: aprovisionar, respaldar, restaurar, monitorear | Python (`psycopg2`) | Tu PC / cron |
| `db/` | Esquema PostgreSQL compartido por los servicios | SQL | — |

Cómo se conectan las piezas: [docs/ARQUITECTURA.md](docs/ARQUITECTURA.md).

## Modelo de despliegue

**Un deploy por restaurante**: cada restaurante tiene su propio proyecto en Railway con su base PostgreSQL y sus tres servicios. Nada se comparte entre restaurantes. Fase actual: **Piloto Fase 1** (meta: 5 restaurantes activos).

- Levantar un restaurante nuevo: [docs/REPLICAR.md](docs/REPLICAR.md)
- Respaldos diarios: [docs/BACKUPS.md](docs/BACKUPS.md)
- Monitoreo de la flota: [docs/MONITOREO.md](docs/MONITOREO.md)
- Cuando algo falla (runbook de soporte): [docs/SOPORTE.md](docs/SOPORTE.md)

## Desarrollo local

Cada servicio tiene su propio `requirements.txt` y toma su configuración de variables de entorno (hay un `.env.example` en cada carpeta; cópialo a `.env` y complétalo).

```bash
# Bot de WhatsApp
cd whatsapp_bot && pip install -r requirements.txt && uvicorn main:app --reload

# Panel de administración
cd dashboard_admin && pip install -r requirements.txt && streamlit run panel.py

# Carta digital
cd app_cliente && pip install -r requirements.txt && streamlit run cliente_app.py
```

El agente de impresión tiene su propio [README](print_agent/README.md) e instalador de 1 clic (`instalar_agente.bat`).

## Ramas

- `main` — producción. No se toca directo: lo que llega aquí ya pasó por `staging`.
- `staging` — pruebas.
- `feature/...` / `feat/...` — trabajo en curso; se integra vía `staging`.

Convenciones de trabajo y reglas del proyecto: [CLAUDE.md](CLAUDE.md).
