# OKU — reglas de trabajo

Este repo es el producto de **OKU** (antes "RestauranteBot"). La estrategia, marca y backlogs viven en el hub OKU de Notion; aquí solo código y documentación técnica.

## Filtro de producto

Antes de agregar cualquier función, módulo o proceso: **¿simplifica la vida del restaurante?** Si no, no entra. Preferir confiabilidad y adopción rápida sobre cantidad de funciones.

## Ramas y flujo

- `main` = **producción**. Nunca commitear ni mergear directo; cada restaurante del piloto despliega desde aquí.
- `staging` = pruebas. Todo pasa por aquí antes de `main`.
- Trabajo en curso en ramas `feature/...` o `feat/...` → merge a `staging` → probar → `main`.
- Borrar las ramas ya mergeadas (mantener el repo simple).

## Modelo de despliegue

Un deploy por restaurante (Railway): base PostgreSQL propia + 3 servicios (`whatsapp_bot`, `dashboard_admin`, `app_cliente`) + `print_agent` en el PC del local, identificado por `RESTAURANTE_ID`. Nada se comparte entre restaurantes. Fase actual: **Piloto Fase 1**, meta 5 restaurantes.

## Convención de commits

Prefijo por área, en minúsculas, mensaje en español describiendo el efecto para el usuario:

```
bot: ...        # whatsapp_bot
panel: ...      # dashboard_admin (POS, cocina, caja, menú)
cliente: ...    # app_cliente (carta digital / pedidos web)
impresion: ...  # print_agent y print_jobs
infra: ...      # scripts de flota, esquema, deploy, docs
fase1: ...      # trabajo transversal de replicabilidad del piloto
```

Si el commit resuelve un Bug / Mejora / Nueva función del tablero 💻 Tareas de Código (Notion), dilo en el cuerpo del mensaje.

## Dónde se anota qué

- **Bugs, mejoras, actualizaciones** → tablero *💻 Tareas de Código* en Notion (no TODOs sueltos en el código).
- **Decisiones grandes** (arquitectura, precios, alcance) → *🧭 Registro de Decisiones* en Notion, antes de implementar.
- **Cómo operar el sistema** (replicar, respaldar, monitorear, arquitectura) → `docs/` en este repo.

## Verificación

Por defecto, checks ligeros: compilar/importar y un script dirigido al cambio. No levantar base de datos ni navegador salvo que se pida.

## Datos sensibles

Nunca commitear `.env`, URLs de base con credenciales, ni respaldos (`backups/`, `*.sql.gz`). Los `.sql` de mantenimiento en `scripts/sql/` — `reset_datos.sql` es **destructivo**, solo para reiniciar un restaurante de prueba.
