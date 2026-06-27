-- ============================================================
-- LIMPIAR DUPLICADOS DEL MENÚ — Restaurantebot
-- Quita las copias repetidas de platos y opciones que se
-- acumularon porque el bot re-sembraba el menú de ejemplo
-- cada vez que una sección quedaba vacía y el bot se reiniciaba.
--
-- DEJA UNA sola copia de cada plato/opción (la más antigua) y
-- borra las repetidas. NO toca platos con nombres únicos.
--
-- IMPORTANTE: primero despliega el bot con el arreglo de
-- "sembrar solo una vez". Si limpias antes, el bot podría volver
-- a llenar una sección vacía en el siguiente reinicio.
--
-- CÓMO EJECUTAR:
--   En Railway → tu servicio PostgreSQL → pestaña "Query"
--   → pega este script y ejecútalo.
-- ============================================================

-- ── PASO 1 (opcional): VER qué está duplicado antes de borrar ──
-- Ejecuta solo estos dos SELECT primero si quieres revisar.
--
--   SELECT grupo, nombre, COUNT(*) AS copias
--   FROM menu_componentes
--   GROUP BY grupo, nombre
--   HAVING COUNT(*) > 1
--   ORDER BY grupo, nombre;
--
--   SELECT categoria, nombre, COUNT(*) AS copias
--   FROM menu
--   GROUP BY categoria, nombre
--   HAVING COUNT(*) > 1
--   ORDER BY categoria, nombre;

-- ── PASO 2: BORRAR las copias repetidas (deja la más antigua) ──
BEGIN;

-- Opciones del Plato del Día (Fruta, Huevo, Res, Arroz, etc.):
-- conserva el id más bajo de cada (grupo, nombre) y borra el resto.
DELETE FROM menu_componentes a
USING menu_componentes b
WHERE a.grupo  = b.grupo
  AND a.nombre = b.nombre
  AND a.id > b.id;

-- Platos del catálogo (especiales / a la carta / bebidas):
-- conserva el id más bajo de cada (categoria, nombre) y borra el resto.
DELETE FROM menu a
USING menu b
WHERE a.categoria = b.categoria
  AND a.nombre    = b.nombre
  AND a.id > b.id;

COMMIT;

-- Después de esto: borra en el panel (🍔 Menú) los platos de ejemplo
-- que NO quieras (Hamburguesa, Pizza, etc.). Ya no volverán a aparecer.
