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

from db import (engine, cargar_mesas_activas, componentes_activos_por_grupo,
                cargar_catalogo, disponibles, precio_plato_dia,
                num_acompanamientos, fmt_money, flash, drain_toasts,
                fee_entrega, upsert_cliente)
from utils.print_jobs import enqueue_comanda


# ── DB: crear pedido de mesa ────────────────────────────────────────────────────
def crear_pedido_manual(mesa_id: int, mesa_nombre: str, items: list, total: int,
                        nota_general=None):
    with engine.begin() as conn:
        nuevo_id = conn.execute(text("""
            INSERT INTO pedidos
              (numero_cliente, items, total, estado, mesa_id, tipo_entrega, nota_general)
            VALUES (:numero, :items, :total, 'pendiente', :mesa_id, 'mesa', :ng)
            RETURNING id
        """), {
            "numero":  mesa_nombre,
            "items":   json.dumps(items, ensure_ascii=False),
            "total":   total,
            "mesa_id": mesa_id,
            "ng":      (nota_general or None),
        }).scalar_one()
    # Flujo de cocina simplificado: ya no hay "Iniciar preparación", así que la comanda
    # se imprime al confirmar el pedido. Tolera fallos: no debe romper la creación.
    enqueue_comanda(int(nuevo_id))
    flash(f"Pedido para {mesa_nombre} creado", "✅")


# ── DB: crear pedido de domicilio / para llevar ─────────────────────────────────
# Mismo contrato de datos que la app pública del cliente (app_cliente.guardar_pedido):
# tipo_entrega + datos del cliente + método de pago/cambio + recargo. NO registra el
# cobro real (eso sigue en el modal de Cobrar del Monitor); metodo_pago/paga_con son
# informativos para que el repartidor lleve el cambio. Aparece en Monitor → Pedidos web.
def crear_pedido_entrega(tipo: str, items: list, total: int, *, cliente_nombre,
                         cliente_telefono, direccion, metodo_pago, paga_con, fee,
                         nota_general=None):
    etq = "Domicilio" if tipo == "domicilio" else "Para llevar"
    nombre = (cliente_nombre or "").strip()
    telefono = (cliente_telefono or "").strip()
    numero = nombre or telefono or etq          # numero_cliente es NOT NULL (legado)
    with engine.begin() as conn:
        nuevo_id = conn.execute(text("""
            INSERT INTO pedidos
              (numero_cliente, items, total, estado, tipo_entrega, cliente_nombre,
               cliente_telefono, direccion, metodo_pago, paga_con, fee, nota_general)
            VALUES (:nc, :items, :total, 'pendiente', :te, :cn, :ct, :dir, :mp, :pc, :fee, :ng)
            RETURNING id
        """), {
            "nc": numero, "items": json.dumps(items, ensure_ascii=False),
            "total": int(total), "te": tipo, "cn": (nombre or None),
            "ct": (telefono or None), "dir": ((direccion or "").strip() or None),
            "mp": metodo_pago, "pc": int(paga_con or 0), "fee": int(fee or 0),
            "ng": (nota_general or None),
        }).scalar_one()
    enqueue_comanda(int(nuevo_id))
    # Alimenta la base de clientes (para autocompletar futuros pedidos). Tolerante.
    if telefono:
        try:
            upsert_cliente(telefono, nombre or None,
                           (direccion or None) if tipo == "domicilio" else None)
        except Exception:
            pass
    flash(f"Pedido {etq} creado", "✅")


# ── Helpers de catálogo ─────────────────────────────────────────────────────────
def _catalogo_seccion(df_cat, categoria):
    """[{id, nombre, precio, descripcion}] ofrecibles hoy de una categoría."""
    if df_cat is None or df_cat.empty:
        return []
    sub = disponibles(df_cat[df_cat["categoria"] == categoria])
    out = []
    for _, r in sub.iterrows():
        out.append({"id": int(r["id"]), "nombre": r["nombre"],
                    "precio": int(r["precio"]), "descripcion": r.get("descripcion")})
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
    txt = " · ".join(p for p in partes if p)
    if it.get("nota"):
        txt += f" · Nota: {it['nota']}"
    return txt


# ── Secciones del configurador ──────────────────────────────────────────────────
def _seccion_plato_dia(comp, precio, n):
    """Configurador del Plato del Día. Devuelve (items, ok)."""
    st.markdown('<div class="section-title">🍛 Plato del Día</div>', unsafe_allow_html=True)
    faltan = [g for g in ("entrada", "principio", "proteina", "acompanamiento") if not comp.get(g)]
    if faltan:
        st.markdown('<p style="color:#9ca3af; font-size:0.85rem;">No disponible hoy: faltan '
                    'opciones activas. Configúralas en 🍔 Menú → Plato del Día.</p>',
                    unsafe_allow_html=True)
        return [], True

    st.caption(f"${fmt_money(precio)} c/u · elige {n} acompañamientos (puedes repetir)")

    qty = int(st.session_state.get("pd_qty_pos", 0))
    c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
    with c1:
        st.markdown('<div style="padding:6px 0; font-size:0.9rem;">¿Cuántos platos del día?</div>',
                    unsafe_allow_html=True)
    with c2:
        if st.button("−", key="pdpos_qty_minus", use_container_width=True):
            st.session_state["pd_qty_pos"] = max(0, qty - 1)
            st.rerun(scope="fragment")
    with c3:
        st.markdown(f'<div style="text-align:center; padding:6px 0; font-weight:700;">{qty}</div>',
                    unsafe_allow_html=True)
    with c4:
        if st.button("+", key="pdpos_qty_plus", use_container_width=True):
            st.session_state["pd_qty_pos"] = qty + 1
            st.rerun(scope="fragment")

    plates, ok = [], True
    for i in range(qty):
        st.markdown(f'<div style="border-left:3px solid #1a1a1a; padding:2px 0 2px 10px; '
                    f'margin:10px 0 4px 0; font-weight:700; font-size:0.9rem;">Plato #{i+1}</div>',
                    unsafe_allow_html=True)

        st.markdown('<div style="font-size:0.75rem; color:#6b7280;">Entrada</div>', unsafe_allow_html=True)
        entrada = st.radio("Entrada", [e["nombre"] for e in comp["entrada"]],
                           key=f"pdpos_{i}_entrada", label_visibility="collapsed", horizontal=True)
        st.markdown('<div style="font-size:0.75rem; color:#6b7280;">Principio</div>', unsafe_allow_html=True)
        principio = st.radio("Principio", [p["nombre"] for p in comp["principio"]],
                             key=f"pdpos_{i}_principio", label_visibility="collapsed", horizontal=True)
        st.markdown('<div style="font-size:0.75rem; color:#6b7280;">Carnes o Proteína</div>',
                    unsafe_allow_html=True)
        proteina = st.radio("Proteína", [p["nombre"] for p in comp["proteina"]],
                            key=f"pdpos_{i}_proteina", label_visibility="collapsed", horizontal=True)

        cuentas = st.session_state.setdefault(f"pdpos_{i}_acomp", {})
        elegidos = sum(cuentas.values())
        st.markdown(f'<div style="font-size:0.75rem; color:#6b7280; margin-top:6px;">'
                    f'Acompañamientos ({elegidos}/{n})</div>', unsafe_allow_html=True)
        for a in comp["acompanamiento"]:
            aid = str(a["id"])
            c = int(cuentas.get(aid, 0))
            ac1, ac2, ac3, ac4 = st.columns([3, 1, 1, 1])
            with ac1:
                st.markdown(f'<div style="padding:4px 0; font-size:0.88rem;">{html.escape(str(a["nombre"]))}</div>',
                            unsafe_allow_html=True)
            with ac2:
                if st.button("−", key=f"pdpos_{i}_acm_{aid}", use_container_width=True):
                    if c > 0:
                        cuentas[aid] = c - 1
                        if cuentas[aid] == 0:
                            del cuentas[aid]
                    st.rerun(scope="fragment")
            with ac3:
                st.markdown(f'<div style="text-align:center; padding:4px 0; font-weight:600;">{c}</div>',
                            unsafe_allow_html=True)
            with ac4:
                if st.button("+", key=f"pdpos_{i}_acp_{aid}", use_container_width=True,
                             disabled=elegidos >= n):
                    cuentas[aid] = c + 1
                    st.rerun(scope="fragment")

        nota = st.text_input("Nota", key=f"pdpos_{i}_nota", label_visibility="collapsed",
                             placeholder="Nota para este plato (opcional)")

        acomp_list = []
        for a in comp["acompanamiento"]:
            acomp_list += [a["nombre"]] * int(cuentas.get(str(a["id"]), 0))
        if elegidos != n:
            ok = False
            st.markdown(f'<p style="color:#b45309; font-size:0.8rem;">Elige exactamente {n} '
                        f'acompañamientos para el Plato #{i+1}.</p>', unsafe_allow_html=True)

        plates.append({
            "tipo": "plato_dia", "nombre": "Plato del Día", "precio": int(precio),
            "cantidad": 1,
            "config": {"entrada": entrada, "principio": principio, "proteina": proteina,
                       "acompanamientos": acomp_list},
            "nota": (nota or "").strip(),
        })
    return plates, ok


def _seccion_catalogo(productos, tipo, titulo, con_desc=False):
    """Sección de catálogo con stepper por producto; devuelve los items elegidos."""
    st.markdown(f'<div class="section-title">{titulo}</div>', unsafe_allow_html=True)
    if not productos:
        st.markdown('<p style="color:#9ca3af; font-size:0.85rem;">Sin opciones disponibles.</p>',
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
            desc = (f'<div style="font-size:0.76rem; color:#9ca3af; font-style:italic;">'
                    f'{html.escape(str(p.get("descripcion") or ""))}</div>'
                    if con_desc and p.get("descripcion") else "")
            st.markdown(
                f'<div style="padding:6px 0;"><span style="font-size:0.9rem; color:#1a1a1a;">'
                f'{html.escape(str(p["nombre"]))}</span>{desc}'
                f'<div style="font-size:0.82rem; color:#6b7280;">${fmt_money(p["precio"])}</div></div>',
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
                if st.button("+", key=f"mas_{key}", use_container_width=True):
                    carrito[key] = qty + 1
                    st.rerun(scope="fragment")
        if qty > 0:
            elegidos.append({"tipo": tipo, "id": pid, "nombre": p["nombre"],
                             "precio": int(p["precio"]), "cantidad": qty})
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
    st.session_state.setdefault("pd_qty_pos", 0)

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
                st.markdown('<p style="color:#9ca3af; font-size:0.85rem;">No hay mesas activas. '
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
        items_esp = _seccion_catalogo(_catalogo_seccion(df_cat, "especial"), "especial",
                                      "⭐ Especiales", con_desc=True)
        items_alc = _seccion_catalogo(_catalogo_seccion(df_cat, "a_la_carta"), "item",
                                      "🍽️ A la carta")
        items_beb = _seccion_catalogo(_catalogo_seccion(df_cat, "bebida"), "bebida",
                                      "🥤 Bebidas")
        items = plates + items_esp + items_alc + items_beb

    with col_resumen:
        st.markdown('<div class="section-title">Resumen</div>', unsafe_allow_html=True)

        if not items:
            st.markdown('<p style="color:#9ca3af; font-size:0.85rem;">Agrega platos para ver el resumen.</p>',
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
                <div style="padding:6px 0; border-bottom:1px solid #e5e7eb; font-size:0.85rem;">
                    <div style="display:flex; justify-content:space-between;">
                      <span style="color:#1a1a1a;">1× Plato del Día</span>
                      <span style="color:#6b7280;">${fmt_money(it["precio"])}</span>
                    </div>
                    <div style="font-size:0.74rem; color:#9ca3af;">{html.escape(_cfg_text(it))}</div>
                </div>""", unsafe_allow_html=True)
            else:
                sub = int(it["precio"]) * int(it["cantidad"])
                st.markdown(f"""
                <div style="display:flex; justify-content:space-between; padding:6px 0;
                            border-bottom:1px solid #e5e7eb; font-size:0.85rem;">
                    <span style="color:#1a1a1a;">{it["cantidad"]}x {html.escape(str(it["nombre"]))}</span>
                    <span style="color:#6b7280;">${fmt_money(sub)}</span>
                </div>""", unsafe_allow_html=True)

        # Línea de recargo (solo entrega).
        if fee:
            recargo_lbl = "Domicilio" if es_domicilio else "Para llevar"
            st.markdown(f"""
            <div style="display:flex; justify-content:space-between; padding:6px 0; font-size:0.85rem;">
                <span style="color:#6b7280;">Recargo · {recargo_lbl}</span>
                <span style="color:#6b7280;">${fmt_money(fee)}</span>
            </div>""", unsafe_allow_html=True)

        if es_mesa:
            destino = mesa_labels[mesa_id]
        else:
            destino = cli_nombre or cli_tel or ("Domicilio" if es_domicilio else "Para llevar")
        st.markdown(f"""
        <div style="display:flex; justify-content:space-between; padding:12px 0 4px 0;">
            <span style="font-family:'Syne',sans-serif; font-weight:700; color:#1a1a1a;">Total</span>
            <span style="font-family:'Syne',sans-serif; font-size:1.2rem; font-weight:800; color:#1a1a1a;">${fmt_money(total)}</span>
        </div>
        <div style="font-size:0.78rem; color:#9ca3af; margin-bottom:0.5rem;">{html.escape(str(destino))}</div>
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
                crear_pedido_manual(mesa_id, mesa_labels[mesa_id], items, total,
                                    (nota_general or "").strip())
            else:
                crear_pedido_entrega(tipo_val, items, total, cliente_nombre=cli_nombre,
                                     cliente_telefono=cli_tel, direccion=cli_dir,
                                     metodo_pago=metodo_pago, paga_con=paga_con, fee=fee,
                                     nota_general=(nota_general or "").strip())
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
    st.session_state["pd_qty_pos"] = 0
    for k in [k for k in st.session_state if str(k).startswith("pdpos_")]:
        del st.session_state[k]
    for k in ("nota_general_pos", "ent_nombre", "ent_tel", "ent_dir",
              "ent_metodo", "ent_pagacon"):
        st.session_state.pop(k, None)
