"""Vista de Nuevo pedido: creación manual de pedidos desde el panel."""
import streamlit as st
from sqlalchemy import text
import json
import html

from db import engine, cargar_menu, fmt_money


# ── DB: crear pedido manual ────────────────────────────────────────────────────
def crear_pedido_manual(mesa: int, items: list, total: int):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pedidos (numero_cliente, items, total, estado)
            VALUES (:numero, :items, :total, 'pendiente')
        """), {
            "numero": f"Mesa {mesa}",
            "items":  json.dumps(items, ensure_ascii=False),
            "total":  total
        })


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: NUEVO PEDIDO
# ══════════════════════════════════════════════════════════════════════════════
def render():
    df_menu  = cargar_menu()
    df_activo = df_menu[df_menu["activo"] == True].reset_index(drop=True)

    col_form, col_resumen = st.columns([3, 2])

    with col_form:
        st.markdown('<div class="section-title">Nuevo pedido</div>', unsafe_allow_html=True)
        mesa = st.number_input("Número de mesa", min_value=1, step=1, value=1, key="mesa_input")

        st.markdown("<div style='margin-top:1rem; margin-bottom:0.5rem; font-size:0.8rem; color:#6b7280; text-transform:uppercase; letter-spacing:1px;'>Selecciona los platos</div>", unsafe_allow_html=True)

        if df_activo.empty:
            st.markdown('<p style="color:#9ca3af; font-size:0.85rem;">No hay platos activos en el menú.</p>', unsafe_allow_html=True)
        else:
            if "carrito_manual" not in st.session_state:
                st.session_state["carrito_manual"] = {}

            for _, row in df_activo.iterrows():
                pid    = int(row["id"])
                nombre = row["nombre"]
                precio = int(row["precio"])
                qty    = st.session_state["carrito_manual"].get(pid, 0)

                # Fix 1: tighter columns [4, 1, 1] to keep controls close to name
                col_nombre, col_precio, col_qty = st.columns([4, 1, 1])
                with col_nombre:
                    st.markdown(f'<div style="padding:8px 0; font-size:0.9rem; color:#1a1a1a;">{html.escape(str(nombre))}</div>', unsafe_allow_html=True)
                with col_precio:
                    st.markdown(f'<div style="padding:8px 0; font-size:0.85rem; color:#6b7280; white-space:nowrap;">${fmt_money(precio)}</div>', unsafe_allow_html=True)
                with col_qty:
                    c_menos, c_num, c_mas = st.columns([1, 1, 1])
                    with c_menos:
                        if st.button("−", key=f"menos_{pid}"):
                            if qty > 0:
                                st.session_state["carrito_manual"][pid] = qty - 1
                                if st.session_state["carrito_manual"][pid] == 0:
                                    del st.session_state["carrito_manual"][pid]
                            st.rerun()
                    with c_num:
                        st.markdown(f'<div style="text-align:center; padding:4px 0; font-size:0.9rem; color:#1a1a1a; font-weight:600;">{qty}</div>', unsafe_allow_html=True)
                    with c_mas:
                        if st.button("+", key=f"mas_{pid}"):
                            st.session_state["carrito_manual"][pid] = qty + 1
                            st.rerun()

    with col_resumen:
        st.markdown('<div class="section-title">Resumen</div>', unsafe_allow_html=True)

        carrito      = st.session_state.get("carrito_manual", {})
        items_pedido = []
        total_pedido = 0

        if not carrito:
            st.markdown('<p style="color:#9ca3af; font-size:0.85rem;">Agrega platos para ver el resumen.</p>', unsafe_allow_html=True)
        else:
            for pid, qty in carrito.items():
                row = df_activo[df_activo["id"] == pid]
                if not row.empty:
                    nombre   = row.iloc[0]["nombre"]
                    precio   = int(row.iloc[0]["precio"])
                    subtotal = precio * qty
                    total_pedido += subtotal
                    items_pedido.append({"id": str(pid), "nombre": nombre, "precio": precio, "cantidad": qty})
                    st.markdown(f"""
                    <div style="display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px solid #e5e7eb; font-size:0.85rem;">
                        <span style="color:#1a1a1a;">{qty}x {html.escape(str(nombre))}</span>
                        <span style="color:#6b7280;">${fmt_money(subtotal)}</span>
                    </div>
                    """, unsafe_allow_html=True)

            st.markdown(f"""
            <div style="display:flex; justify-content:space-between; padding:12px 0 4px 0;">
                <span style="font-family:'Syne',sans-serif; font-weight:700; color:#1a1a1a;">Total</span>
                <span style="font-family:'Syne',sans-serif; font-size:1.2rem; font-weight:800; color:#1a1a1a;">${fmt_money(total_pedido)}</span>
            </div>
            <div style="font-size:0.78rem; color:#9ca3af; margin-bottom:1rem;">Mesa {int(mesa)}</div>
            """, unsafe_allow_html=True)

            if st.button("✓ Confirmar pedido", type="primary", key="btn_confirmar_manual"):
                crear_pedido_manual(int(mesa), items_pedido, total_pedido)
                st.session_state["carrito_manual"] = {}
                st.success(f"Pedido para Mesa {int(mesa)} creado ✓")
                st.rerun()

        if carrito:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("🗑 Limpiar", key="btn_limpiar"):
                st.session_state["carrito_manual"] = {}
                st.rerun()
