"""Vista de Nuevo pedido (POS de meseros): arma un pedido de MESA con la misma
taxonomía de 4 secciones que la app del cliente.

  #1 Plato del Día — configurable por plato (entrada / principio / proteína / N
     acompañamientos con repetición). Si pides más de uno, se repite la config.
  #2 Especiales — precio plano + descripción.
  #3 A la carta — con sub-grupo de Bebidas.
  #4 Nota general del pedido.

Produce el MISMO JSON de items por secciones que la app del cliente (utils.items lo
sabe pintar/imprimir). Es un pedido de mesa: tipo_entrega='mesa', sin recargo; el
cobro se hace luego con el modal de Cobrar existente.
"""
import streamlit as st
from sqlalchemy import text
import json
import html

from db import (engine, titulo_seccion, cargar_mesas_activas, componentes_activos_por_grupo,
                cargar_catalogo, disponibles, precio_plato_dia,
                num_acompanamientos, fmt_money, flash, drain_toasts,
                fee_entrega, upsert_cliente, aplicar_inventario, SinStock,
                siguiente_num_dia, resumen_disponibilidad_componentes, agotado_por_stock,
                stock_int, STOCK_BAJO, GRUPO_LABEL)
from utils.print_jobs import enqueue_comanda


# ── DB: crear pedido de mesa ────────────────────────────────────────────────────
def crear_pedido_manual(mesa_id: int, mesa_nombre: str, items: list, total: int,
                        nota_general=None) -> bool:
    """Crea un pedido de mesa con numeración diaria atómica (siguiente_num_dia) y descuento
    de inventario con guarda. Devuelve True si se creó; False si se rechazó por falta de
    stock (en ese caso NADA quedó insertado y se avisa qué se agotó)."""
    try:
        with engine.begin() as conn:
            # num_dia atómico ANTES del INSERT: toma el lock del día y serializa la creación.
            num = siguiente_num_dia(conn)
            nuevo_id = conn.execute(text("""
                INSERT INTO pedidos
                  (num_dia, numero_cliente, items, total, estado, mesa_id, tipo_entrega, nota_general)
                VALUES (:num, :numero, :items, :total, 'pendiente', :mesa_id, 'mesa', :ng)
                RETURNING id
            """), {
                "num":     num,
                "numero":  mesa_nombre,
                "items":   json.dumps(items, ensure_ascii=False),
                "total":   total,
                "mesa_id": mesa_id,
                "ng":      (nota_general or None),
            }).scalar_one()
            # Descuento inmediato del inventario (mismo txn que el INSERT → atómico): si una
            # opción rastreada no alcanza, SinStock revienta el txn y el pedido NO se crea
            # (no se sobrevende lo que ya está en cocina).
            aplicar_inventario(conn, items, -1)
    except SinStock as e:
        flash(f"🚫 Se agotó: {e}. Quítalo del pedido y vuelve a confirmar.", "🚫")
        return False
    # Flujo de cocina simplificado: ya no hay "Iniciar preparación", así que la comanda
    # se imprime al confirmar el pedido. Tolera fallos: no debe romper la creación.
    enqueue_comanda(int(nuevo_id))
    flash(f"Pedido para {mesa_nombre} creado", "✅")
    return True


# ── DB: crear pedido de domicilio / para llevar ─────────────────────────────────
# Mismo contrato de datos que la app pública del cliente (app_cliente.guardar_pedido):
# tipo_entrega + datos del cliente + método de pago/cambio + recargo. NO registra el
# cobro real (eso sigue en el modal de Cobrar del Monitor); metodo_pago/paga_con son
# informativos para que el repartidor lleve el cambio. Aparece en Monitor → Pedidos web.
def crear_pedido_entrega(tipo: str, items: list, total: int, *, cliente_nombre,
                         cliente_telefono, direccion, metodo_pago, paga_con, fee,
                         nota_general=None) -> bool:
    """Crea un pedido de domicilio / para llevar. Devuelve True si se creó; False si se
    rechazó por falta de stock (nada queda insertado)."""
    etq = "Domicilio" if tipo == "domicilio" else "Para llevar"
    nombre = (cliente_nombre or "").strip()
    telefono = (cliente_telefono or "").strip()
    numero = nombre or telefono or etq          # numero_cliente es NOT NULL (legado)
    try:
        with engine.begin() as conn:
            num = siguiente_num_dia(conn)
            nuevo_id = conn.execute(text("""
                INSERT INTO pedidos
                  (num_dia, numero_cliente, items, total, estado, tipo_entrega, cliente_nombre,
                   cliente_telefono, direccion, metodo_pago, paga_con, fee, nota_general)
                VALUES (:num, :nc, :items, :total, 'pendiente', :te, :cn, :ct, :dir, :mp, :pc, :fee, :ng)
                RETURNING id
            """), {
                "num": num, "nc": numero, "items": json.dumps(items, ensure_ascii=False),
                "total": int(total), "te": tipo, "cn": (nombre or None),
                "ct": (telefono or None), "dir": ((direccion or "").strip() or None),
                "mp": metodo_pago, "pc": int(paga_con or 0), "fee": int(fee or 0),
                "ng": (nota_general or None),
            }).scalar_one()
            # Descuento de inventario en el mismo txn (igual que en los pedidos de mesa).
            aplicar_inventario(conn, items, -1)
    except SinStock as e:
        flash(f"🚫 Se agotó: {e}. Quítalo del pedido y vuelve a confirmar.", "🚫")
        return False
    enqueue_comanda(int(nuevo_id))
    # Alimenta la base de clientes (para autocompletar futuros pedidos). Tolerante.
    if telefono:
        try:
            upsert_cliente(telefono, nombre or None,
                           (direccion or None) if tipo == "domicilio" else None)
        except Exception:
            pass
    flash(f"Pedido {etq} creado", "✅")
    return True


# ── Helpers de catálogo ─────────────────────────────────────────────────────────
def _catalogo_seccion(df_cat, categoria):
    """[{id, nombre, precio, descripcion, stock}] ofrecibles hoy de una categoría.
    OCULTACIÓN ESTRICTA del a la carta: un plato con control de stock que llega a 0
    desaparece por completo de la carta del mesero (no se muestra ni en gris)."""
    if df_cat is None or df_cat.empty:
        return []
    sub = disponibles(df_cat[df_cat["categoria"] == categoria])
    out = []
    for _, r in sub.iterrows():
        s = stock_int(r.get("stock"))
        if s is not None and s <= 0:
            continue
        out.append({"id": int(r["id"]), "nombre": r["nombre"],
                    "precio": int(r["precio"]), "descripcion": r.get("descripcion"),
                    "stock": s})
    return out


def _cfg_text(it) -> str:
    """Configuración de un plato del día como texto corto para el resumen."""
    cfg = it.get("config") or {}
    partes = [cfg.get("entrada"), cfg.get("principio"), cfg.get("proteina")]
    ac = cfg.get("acompanamientos") or []
    if ac:
        orden, cnt = [], {}
        for a in ac:
            if a not in cnt:
                orden.append(a)
            cnt[a] = cnt.get(a, 0) + 1
        partes.append(", ".join(f"{cnt[a]}x {a}" for a in orden))
    if cfg.get("bebida"):
        partes.append(str(cfg.get("bebida")))
    txt = " · ".join(p for p in partes if p)
    if it.get("nota"):
        txt += f" · Nota: {it['nota']}"
    return txt


# ── Secciones del configurador ──────────────────────────────────────────────────
# Cada Plato del Día del carrito es una INSTANCIA con id único de seguimiento:
# st.session_state["pd_instancias"] = [uid, ...] y pd_seq genera los uids. Así cada
# plato se borra individualmente (🗑) sin descuadrar a los demás, porque sus widgets
# se llavean por uid (pdpos_<uid>_*) y no por índice posicional.
# "Ninguno" encabeza entrada/principio/proteína: si se elige, ese paso se guarda como
# None → utils.items lo omite (no imprime ni inserta nada para ese slot) y no exige el
# ingrediente. Entrada/principio/proteína se eligen con BOTONES (no st.radio) para poder
# deshabilitar individualmente las opciones agotadas y mostrar las porciones restantes;
# el valor por defecto es el primer componente DISPONIBLE (ver _selector_grupo).
NINGUNO = "Ninguno"


def _eliminar_plato_dia(uid) -> None:
    """Quita una instancia de Plato del Día del carrito y purga el estado de sus
    widgets (selectores/steppers/nota) para que no quede 'pegado' a un uid reutilizado."""
    insts = st.session_state.get("pd_instancias", [])
    if uid in insts:
        insts.remove(uid)
    for k in [k for k in st.session_state if str(k).startswith(f"pdpos_{uid}_")]:
        del st.session_state[k]


def _stock_suffix(stock) -> str:
    """' (12 disp.)' si lleva control; ' · Agotado' en 0; '' si es ilimitado (None)."""
    if stock is None:
        return ""
    return f" ({int(stock)} disp.)" if int(stock) > 0 else " · Agotado"


def _default_grupo_sel(opciones) -> str:
    """Selección por defecto de un grupo: la primera opción DISPONIBLE (con stock o
    ilimitada). Si todas están agotadas, cae en 'Ninguno' (deja el plato válido)."""
    for o in opciones:
        if not agotado_por_stock(o.get("stock")):
            return o["nombre"]
    return NINGUNO


def _selector_grupo(uid, grupo, opciones, label, *, default_ninguno=False):
    """Selector de UNA opción (entrada/principio/proteína) en botones — no st.radio —
    para poder deshabilitar individualmente las opciones agotadas (los radios de
    Streamlit no permiten desactivar opciones sueltas) y mostrar las porciones que
    quedan. 'Ninguno' siempre disponible. Devuelve el nombre elegido, o None (Ninguno).
    default_ninguno=True arranca en 'Ninguno' (para extras opcionales, p. ej. la
    entrada/bebida incluidas en un especial); por defecto arranca en la 1ª disponible."""
    key = f"pdpos_{uid}_{grupo}"
    if key not in st.session_state:
        st.session_state[key] = NINGUNO if default_ninguno else _default_grupo_sel(opciones)
    sel = st.session_state.get(key)
    st.markdown(f'<div style="font-size:0.75rem; color:#6b6b64; margin-top:6px;">{label}</div>',
                unsafe_allow_html=True)
    botones = [{"nombre": NINGUNO, "stock": None, "disabled": False}]
    for o in opciones:
        botones.append({"nombre": o["nombre"], "stock": o.get("stock"),
                        "disabled": agotado_por_stock(o.get("stock"))})
    por_fila = 3
    for inicio in range(0, len(botones), por_fila):
        fila = botones[inicio:inicio + por_fila]
        cols = st.columns(len(fila))
        for col, b in zip(cols, fila):
            with col:
                nombre = b["nombre"]
                activo = (sel == nombre)
                etiqueta = f"{'● ' if activo else ''}{nombre}{_stock_suffix(b['stock'])}"
                if st.button(etiqueta, key=f"pdpos_{uid}_{grupo}_opt_{nombre}",
                             use_container_width=True, disabled=b["disabled"],
                             type=("primary" if activo else "secondary")):
                    st.session_state[key] = nombre
                    st.rerun(scope="fragment")
    return None if sel == NINGUNO else sel


def _render_alerta_cocina():
    """Tablero proactivo de disponibilidad SOBRE el botón de armar el Plato del Día:
    avisa qué micro-componentes están agotados (0) y cuáles quedan pocos (≤STOCK_BAJO),
    para que el mesero lo sepa ANTES de acercarse a la mesa. No oculta nunca el plato."""
    r = resumen_disponibilidad_componentes()
    ago, bajos = r["agotados"], r["bajos"]
    if not ago and not bajos:
        return
    bloques = []
    if ago:
        chips = " ".join(
            f'<span style="display:inline-block; background:#fee2e2; color:#991b1b; '
            f'border:1px solid #fecaca; border-radius:999px; padding:2px 10px; margin:2px; '
            f'font-size:0.78rem; font-weight:600;">'
            f'{html.escape(GRUPO_LABEL.get(a["grupo"], a["grupo"]))}: {html.escape(a["nombre"])}</span>'
            for a in ago)
        bloques.append(f'<div style="margin-bottom:4px;">'
                       f'<b style="color:#991b1b;">🚫 Agotado:</b> {chips}</div>')
    if bajos:
        chips = " ".join(
            f'<span style="display:inline-block; background:#fef3c7; color:#92400e; '
            f'border:1px solid #fde68a; border-radius:999px; padding:2px 10px; margin:2px; '
            f'font-size:0.78rem; font-weight:600;">'
            f'{html.escape(b["nombre"])} ({int(b["stock"])})</span>'
            for b in bajos)
        bloques.append(f'<div><b style="color:#92400e;">⚠️ Quedan pocos:</b> {chips}</div>')
    st.markdown(
        '<div style="background:#fffbeb; border:1px solid #fde68a; border-radius:12px; '
        'padding:0.7rem 0.9rem; margin-bottom:0.8rem;">'
        '<div style="font-family:\'DM Sans\',sans-serif; font-weight:600; font-size:0.85rem; '
        'color:#26262b; margin-bottom:6px;">🔔 Alerta de cocina · Plato del Día</div>'
        + "".join(bloques) + '</div>',
        unsafe_allow_html=True,
    )


def _seccion_plato_dia(comp, precio, n):
    """Configurador del Plato del Día. Devuelve (items, ok)."""
    st.markdown(titulo_seccion('🍛 Plato del Día'), unsafe_allow_html=True)
    faltan = [g for g in ("entrada", "principio", "proteina", "acompanamiento") if not comp.get(g)]
    if faltan:
        st.markdown('<p style="color:#a3a39b; font-size:0.85rem;">No disponible hoy: faltan '
                    'opciones activas. Configúralas en 🍔 Menú → Plato del Día.</p>',
                    unsafe_allow_html=True)
        return [], True

    # Alerta proactiva de cocina (agotados / quedan pocos) ANTES de armar el plato.
    _render_alerta_cocina()

    st.caption(f"${fmt_money(precio)} c/u · elige {n} acompañamientos (puedes repetir)")

    instancias = st.session_state.setdefault("pd_instancias", [])
    if st.button("➕ Agregar plato del día", key="pdpos_add", use_container_width=True):
        st.session_state["pd_seq"] = int(st.session_state.get("pd_seq", 0)) + 1
        instancias.append(st.session_state["pd_seq"])
        st.rerun(scope="fragment")

    plates, ok = [], True
    for pos, uid in enumerate(list(instancias)):
        c_tit, c_del = st.columns([5, 1])
        with c_tit:
            st.markdown(f'<div style="border-left:3px solid #26262b; padding:2px 0 2px 10px; '
                        f'margin:10px 0 4px 0; font-weight:700; font-size:0.9rem;">Plato #{pos+1}</div>',
                        unsafe_allow_html=True)
        with c_del:
            if st.button("🗑", key=f"pdpos_{uid}_del", use_container_width=True,
                         help="Quitar este plato del día"):
                _eliminar_plato_dia(uid)
                st.rerun(scope="fragment")

        # Selectores con porciones restantes; las opciones en 0 quedan deshabilitadas.
        entrada   = _selector_grupo(uid, "entrada",   comp["entrada"],   "Entrada")
        principio = _selector_grupo(uid, "principio", comp["principio"], "Principio")
        proteina  = _selector_grupo(uid, "proteina",  comp["proteina"],  "Carnes o Proteína")
        # Bebida del día (incluida en el precio plano): solo se ofrece si el restaurante
        # configuró opciones de bebida para el Plato del Día. "Ninguno" permite omitirla.
        bebida = (_selector_grupo(uid, "bebida", comp["bebida"], "Bebida")
                  if comp.get("bebida") else None)

        cuentas = st.session_state.setdefault(f"pdpos_{uid}_acomp", {})
        elegidos = sum(cuentas.values())
        st.markdown(f'<div style="font-size:0.75rem; color:#6b6b64; margin-top:6px;">'
                    f'Acompañamientos ({elegidos}/{n})</div>', unsafe_allow_html=True)
        for a in comp["acompanamiento"]:
            aid = str(a["id"])
            stock_a = a.get("stock")
            agot = agotado_por_stock(stock_a)
            c = int(cuentas.get(aid, 0))
            ac1, ac2, ac3, ac4 = st.columns([3, 1, 1, 1])
            with ac1:
                color = "#a3a39b" if agot else "#26262b"
                st.markdown(f'<div style="padding:4px 0; font-size:0.88rem; color:{color};">'
                            f'{html.escape(str(a["nombre"]))}{_stock_suffix(stock_a)}</div>',
                            unsafe_allow_html=True)
            with ac2:
                if st.button("−", key=f"pdpos_{uid}_acm_{aid}", use_container_width=True):
                    if c > 0:
                        cuentas[aid] = c - 1
                        if cuentas[aid] == 0:
                            del cuentas[aid]
                    st.rerun(scope="fragment")
            with ac3:
                st.markdown(f'<div style="text-align:center; padding:4px 0; font-weight:600;">{c}</div>',
                            unsafe_allow_html=True)
            with ac4:
                # Tope: ya se eligieron n, o el componente está agotado, o ya se tomaron
                # todas sus porciones rastreadas en este plato.
                tope_stock = (stock_a is not None and c >= int(stock_a))
                if st.button("+", key=f"pdpos_{uid}_acp_{aid}", use_container_width=True,
                             disabled=(elegidos >= n or agot or tope_stock)):
                    cuentas[aid] = c + 1
                    st.rerun(scope="fragment")

        nota = st.text_input("Nota", key=f"pdpos_{uid}_nota", label_visibility="collapsed",
                             placeholder="Nota para este plato (opcional)")

        acomp_list = []
        for a in comp["acompanamiento"]:
            acomp_list += [a["nombre"]] * int(cuentas.get(str(a["id"]), 0))
        if elegidos != n:
            ok = False
            st.markdown(f'<p style="color:#b45309; font-size:0.8rem;">Elige exactamente {n} '
                        f'acompañamientos para el Plato #{pos+1}.</p>', unsafe_allow_html=True)

        cfg = {"entrada": entrada, "principio": principio, "proteina": proteina,
               "acompanamientos": acomp_list}
        if bebida:
            cfg["bebida"] = bebida
        plates.append({
            "tipo": "plato_dia", "nombre": "Plato del Día", "precio": int(precio),
            "cantidad": 1, "config": cfg, "nota": (nota or "").strip(),
        })
    return plates, ok


def _seccion_catalogo(productos, tipo, titulo, con_desc=False):
    """Sección de catálogo con stepper por producto; devuelve los items elegidos."""
    st.markdown(titulo_seccion(titulo), unsafe_allow_html=True)
    if not productos:
        st.markdown('<p style="color:#a3a39b; font-size:0.85rem;">Sin opciones disponibles.</p>',
                    unsafe_allow_html=True)
        return []
    carrito = st.session_state["carrito_manual"]
    elegidos = []
    for p in productos:
        pid = int(p["id"])
        key = f"{tipo}:{pid}"
        qty = carrito.get(key, 0)
        c_nom, c_qty = st.columns([3, 2])
        with c_nom:
            desc = (f'<div style="font-size:0.76rem; color:#a3a39b; font-style:italic;">'
                    f'{html.escape(str(p.get("descripcion") or ""))}</div>'
                    if con_desc and p.get("descripcion") else "")
            st.markdown(
                f'<div style="padding:6px 0;"><span style="font-size:0.9rem; color:#26262b;">'
                f'{html.escape(str(p["nombre"]))}</span>{desc}'
                f'<div style="font-size:0.82rem; color:#6b6b64;">${fmt_money(p["precio"])}'
                f'{_stock_suffix(p.get("stock"))}</div></div>',
                unsafe_allow_html=True,
            )
        with c_qty:
            m, q, pl = st.columns([1, 1, 1])
            with m:
                if st.button("−", key=f"menos_{key}", use_container_width=True):
                    if qty > 0:
                        carrito[key] = qty - 1
                        if carrito[key] == 0:
                            del carrito[key]
                    st.rerun(scope="fragment")
            with q:
                st.markdown(f'<div style="text-align:center; padding:4px 0; font-weight:600;">{qty}</div>',
                            unsafe_allow_html=True)
            with pl:
                # No se puede pedir más de lo que queda en stock (si lleva control).
                tope = (p.get("stock") is not None and qty >= int(p["stock"]))
                if st.button("+", key=f"mas_{key}", use_container_width=True, disabled=tope):
                    carrito[key] = qty + 1
                    st.rerun(scope="fragment")
        if qty > 0:
            elegidos.append({"tipo": tipo, "id": pid, "nombre": p["nombre"],
                             "precio": int(p["precio"]), "cantidad": qty})
    return elegidos


def _seccion_especiales(productos, comp):
    """Especiales: mismo stepper que el catálogo (precio + descripción) y, cuando el
    restaurante tiene entradas/bebidas del Plato del Día configuradas, dos selectores
    OPCIONALES (Entrada / Bebida) que van INCLUIDOS sin costo. La selección se comparte
    para todas las unidades del mismo especial (un solo config por producto). Si el
    restaurante no tiene esos componentes, el especial se comporta igual que antes."""
    st.markdown(titulo_seccion("⭐ Especiales"), unsafe_allow_html=True)
    if not productos:
        st.markdown('<p style="color:#a3a39b; font-size:0.85rem;">Sin opciones disponibles.</p>',
                    unsafe_allow_html=True)
        return []
    ofrece_entrada = bool(comp.get("entrada"))
    ofrece_bebida  = bool(comp.get("bebida"))
    carrito = st.session_state["carrito_manual"]
    elegidos = []
    for p in productos:
        pid = int(p["id"])
        key = f"especial:{pid}"
        qty = carrito.get(key, 0)
        c_nom, c_qty = st.columns([3, 2])
        with c_nom:
            desc = (f'<div style="font-size:0.76rem; color:#a3a39b; font-style:italic;">'
                    f'{html.escape(str(p.get("descripcion") or ""))}</div>'
                    if p.get("descripcion") else "")
            st.markdown(
                f'<div style="padding:6px 0;"><span style="font-size:0.9rem; color:#26262b;">'
                f'{html.escape(str(p["nombre"]))}</span>{desc}'
                f'<div style="font-size:0.82rem; color:#6b6b64;">${fmt_money(p["precio"])}'
                f'{_stock_suffix(p.get("stock"))}</div></div>',
                unsafe_allow_html=True,
            )
        with c_qty:
            m, q, pl = st.columns([1, 1, 1])
            with m:
                if st.button("−", key=f"menos_{key}", use_container_width=True):
                    if qty > 0:
                        carrito[key] = qty - 1
                        if carrito[key] == 0:
                            del carrito[key]
                    st.rerun(scope="fragment")
            with q:
                st.markdown(f'<div style="text-align:center; padding:4px 0; font-weight:600;">{qty}</div>',
                            unsafe_allow_html=True)
            with pl:
                tope = (p.get("stock") is not None and qty >= int(p["stock"]))
                if st.button("+", key=f"mas_{key}", use_container_width=True, disabled=tope):
                    carrito[key] = qty + 1
                    st.rerun(scope="fragment")

        cfg = {}
        # Entrada/bebida del Plato del Día incluidas: solo se ofrecen si el especial está
        # en el carrito y el restaurante tiene esos componentes. "Ninguno" por defecto.
        if qty > 0 and (ofrece_entrada or ofrece_bebida):
            uid = f"esp_{pid}"
            if ofrece_entrada:
                ent = _selector_grupo(uid, "entrada", comp["entrada"], "Entrada (incluida)",
                                      default_ninguno=True)
                if ent:
                    cfg["entrada"] = ent
            if ofrece_bebida:
                beb = _selector_grupo(uid, "bebida", comp["bebida"], "Bebida (incluida)",
                                      default_ninguno=True)
                if beb:
                    cfg["bebida"] = beb

        if qty > 0:
            item = {"tipo": "especial", "id": pid, "nombre": p["nombre"],
                    "precio": int(p["precio"]), "cantidad": qty}
            if cfg:
                item["config"] = cfg
            elegidos.append(item)
    return elegidos


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: NUEVO PEDIDO
# ══════════════════════════════════════════════════════════════════════════════
def render():
    # P4: corre como fragment; los +/- y el confirmar relanzan solo este bloque.
    _form_fragment()


@st.fragment
def _form_fragment():
    drain_toasts()

    st.session_state.setdefault("carrito_manual", {})
    st.session_state.setdefault("pd_instancias", [])
    st.session_state.setdefault("pd_seq", 0)

    mesas       = cargar_mesas_activas()
    mesa_ids    = [int(m["id"]) for m in mesas]
    mesa_labels = {int(m["id"]): m["nombre"] for m in mesas}

    df_cat = cargar_catalogo()
    comp   = componentes_activos_por_grupo()
    precio_pd = precio_plato_dia()
    n_ac      = num_acompanamientos()

    col_form, col_resumen = st.columns([3, 2])

    items, ok_pd = [], True
    mesa_id = None
    cli_nombre = cli_tel = cli_dir = ""
    with col_form:
        st.markdown('<div class="section-title">Nuevo pedido</div>', unsafe_allow_html=True)

        # Tipo de pedido: mesa (salón) o entrega (domicilio / para llevar).
        st.caption("Tipo de pedido")
        tipo_sel = st.radio("Tipo de pedido", ["🪑 Mesa", "🛵 Domicilio", "🛍️ Para llevar"],
                            horizontal=True, label_visibility="collapsed", key="tipo_pedido_pos")
        es_mesa      = tipo_sel.startswith("🪑")
        es_domicilio = tipo_sel.startswith("🛵")
        tipo_val     = "mesa" if es_mesa else ("domicilio" if es_domicilio else "para_llevar")

        if es_mesa:
            if not mesas:
                st.markdown('<p style="color:#a3a39b; font-size:0.85rem;">No hay mesas activas. '
                            'Crea una en la pestaña 🪑 Mesas.</p>', unsafe_allow_html=True)
                return
            mesa_id = st.selectbox("Mesa", options=mesa_ids,
                                   format_func=lambda i: mesa_labels[i], key="mesa_sel_manual")
        else:
            # Datos del cliente para la entrega (se guardan en el pedido y la base de clientes).
            cli_nombre = (st.text_input("Nombre del cliente", key="ent_nombre",
                                        placeholder="Nombre (opcional)") or "").strip()
            cli_tel = (st.text_input("Teléfono", key="ent_tel",
                                     placeholder="Para el contacto / guardar (opcional)") or "").strip()
            if es_domicilio:
                cli_dir = (st.text_area("Dirección de entrega", key="ent_dir",
                                        placeholder="Dirección + referencias") or "").strip()

        plates, ok_pd = _seccion_plato_dia(comp, precio_pd, n_ac)
        items_esp = _seccion_especiales(_catalogo_seccion(df_cat, "especial"), comp)
        items_alc = _seccion_catalogo(_catalogo_seccion(df_cat, "a_la_carta"), "item",
                                      "🍽️ A la carta")
        items_adi = _seccion_catalogo(_catalogo_seccion(df_cat, "adicional"), "adicional",
                                      "🍟 Adicionales", con_desc=True)
        items_beb = _seccion_catalogo(_catalogo_seccion(df_cat, "bebida"), "bebida",
                                      "🥤 Bebidas")
        items = plates + items_esp + items_alc + items_adi + items_beb

    with col_resumen:
        st.markdown('<div class="section-title">Resumen</div>', unsafe_allow_html=True)

        if not items:
            st.markdown('<p style="color:#a3a39b; font-size:0.85rem;">Agrega platos para ver el resumen.</p>',
                        unsafe_allow_html=True)
            return

        subtotal = sum(int(it["precio"]) * int(it["cantidad"]) for it in items)
        # Recargo de entrega: aplica a domicilio Y para llevar (mesa = 0), igual que la
        # app pública. total = subtotal + recargo.
        fee = 0 if es_mesa else fee_entrega()
        total = subtotal + fee
        for it in items:
            if it.get("tipo") == "plato_dia":
                st.markdown(f"""
                <div style="padding:6px 0; border-bottom:1px solid #ececec; font-size:0.85rem;">
                    <div style="display:flex; justify-content:space-between;">
                      <span style="color:#26262b;">1× Plato del Día</span>
                      <span style="color:#6b6b64;">${fmt_money(it["precio"])}</span>
                    </div>
                    <div style="font-size:0.74rem; color:#a3a39b;">{html.escape(_cfg_text(it))}</div>
                </div>""", unsafe_allow_html=True)
            else:
                sub = int(it["precio"]) * int(it["cantidad"])
                # Un especial puede traer entrada/bebida incluidas: se muestran debajo.
                extra = ""
                if it.get("config"):
                    cfg_txt = _cfg_text(it)
                    if cfg_txt:
                        extra = (f'<div style="font-size:0.74rem; color:#a3a39b;">'
                                 f'{html.escape(cfg_txt)}</div>')
                st.markdown(f"""
                <div style="padding:6px 0; border-bottom:1px solid #ececec; font-size:0.85rem;">
                    <div style="display:flex; justify-content:space-between;">
                      <span style="color:#26262b;">{it["cantidad"]}x {html.escape(str(it["nombre"]))}</span>
                      <span style="color:#6b6b64;">${fmt_money(sub)}</span>
                    </div>{extra}
                </div>""", unsafe_allow_html=True)

        # Línea de recargo (solo entrega).
        if fee:
            recargo_lbl = "Domicilio" if es_domicilio else "Para llevar"
            st.markdown(f"""
            <div style="display:flex; justify-content:space-between; padding:6px 0; font-size:0.85rem;">
                <span style="color:#6b6b64;">Recargo · {recargo_lbl}</span>
                <span style="color:#6b6b64;">${fmt_money(fee)}</span>
            </div>""", unsafe_allow_html=True)

        if es_mesa:
            destino = mesa_labels[mesa_id]
        else:
            destino = cli_nombre or cli_tel or ("Domicilio" if es_domicilio else "Para llevar")
        st.markdown(f"""
        <div style="display:flex; justify-content:space-between; padding:12px 0 4px 0;">
            <span style="font-family:'DM Sans',sans-serif; font-weight:600; color:#26262b;">Total</span>
            <span style="font-family:'DM Sans',sans-serif; font-size:1.2rem; font-weight:600; color:#26262b;">${fmt_money(total)}</span>
        </div>
        <div style="font-size:0.78rem; color:#a3a39b; margin-bottom:0.5rem;">{html.escape(str(destino))}</div>
        """, unsafe_allow_html=True)

        # Pago (solo entrega): método + con cuánto paga, para el cambio del repartidor. El
        # cobro real se registra luego en el Monitor (Cobrar); esto es informativo.
        metodo_pago, paga_con = None, 0
        if not es_mesa:
            st.caption("Método de pago")
            metodo_sel = st.radio("Método de pago", ["💵 Efectivo", "💳 Transferencia"],
                                  horizontal=True, label_visibility="collapsed", key="ent_metodo")
            es_efectivo = metodo_sel.startswith("💵")
            metodo_pago = "efectivo" if es_efectivo else "transferencia"
            if es_efectivo:
                paga_con = int(st.number_input("¿Con cuánto paga? (para el cambio)",
                                               min_value=0, value=0, step=1000, format="%d",
                                               key="ent_pagacon") or 0)
                if paga_con > 0 and paga_con >= total:
                    st.caption(f"Cambio: ${fmt_money(paga_con - total)}")

        nota_general = st.text_input("Nota general", key="nota_general_pos",
                                     label_visibility="collapsed",
                                     placeholder="Nota general del pedido (opcional)")

        if not ok_pd:
            st.markdown('<p style="color:#b45309; font-size:0.8rem;">Completa los acompañamientos '
                        'de cada Plato del Día antes de confirmar.</p>', unsafe_allow_html=True)

        falta_dir = es_domicilio and not cli_dir
        if falta_dir:
            st.markdown('<p style="color:#b45309; font-size:0.8rem;">Ingresa la dirección de '
                        'entrega antes de confirmar.</p>', unsafe_allow_html=True)

        if st.button("✓ Confirmar pedido", type="primary", key="btn_confirmar_manual",
                     use_container_width=True, disabled=(not ok_pd or falta_dir)):
            if es_mesa:
                ok = crear_pedido_manual(mesa_id, mesa_labels[mesa_id], items, total,
                                         (nota_general or "").strip())
            else:
                ok = crear_pedido_entrega(tipo_val, items, total, cliente_nombre=cli_nombre,
                                          cliente_telefono=cli_tel, direccion=cli_dir,
                                          metodo_pago=metodo_pago, paga_con=paga_con, fee=fee,
                                          nota_general=(nota_general or "").strip())
            # Solo vaciamos el carrito si el pedido se creó. Si se rechazó por falta de
            # stock, lo conservamos para que el mesero quite el ítem agotado y reintente.
            if ok:
                _limpiar_pedido()
            st.rerun(scope="fragment")

        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🗑 Limpiar", key="btn_limpiar", use_container_width=True):
            _limpiar_pedido()
            flash("Pedido vaciado", "🧹")
            st.rerun(scope="fragment")


def _limpiar_pedido():
    """Resetea el carrito, la cantidad, la config de platos del día y los datos de
    entrega (cliente/dirección/pago). Conserva el TIPO de pedido seleccionado."""
    st.session_state["carrito_manual"] = {}
    st.session_state["pd_instancias"] = []
    st.session_state["pd_seq"] = 0
    for k in [k for k in st.session_state if str(k).startswith("pdpos_")]:
        del st.session_state[k]
    for k in ("nota_general_pos", "ent_nombre", "ent_tel", "ent_dir",
              "ent_metodo", "ent_pagacon"):
        st.session_state.pop(k, None)
