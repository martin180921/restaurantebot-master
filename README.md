# RestauranteBot Master

Proyecto unificado con tres servicios independientes que comparten la misma base de datos PostgreSQL.

## Estructura

| Carpeta | Servicio | Tecnología |
|---|---|---|
| `whatsapp_bot/` | Bot de pedidos por WhatsApp | FastAPI + Twilio |
| `dashboard_admin/` | Panel de cocina y administración | Streamlit |
| `app_cliente/` | Carta digital para el comensal | Streamlit |
| `core_db/` | Modelos y conexión compartida | SQLAlchemy |

## Configuración

Cada servicio tiene su propio `.env`. Copia el `.env` de ejemplo y completa tus credenciales antes de correr.

### whatsapp_bot
```bash
cd whatsapp_bot
pip install -r requirements.txt
uvicorn main:app --reload
```

### dashboard_admin
```bash
cd dashboard_admin
pip install -r requirements.txt
streamlit run panel.py
```

### app_cliente
```bash
cd app_cliente
pip install -r requirements.txt
streamlit run cliente_app.py
```
