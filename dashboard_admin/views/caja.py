"""Vista de Caja: arqueo de turno (apertura con fondo + cierre con conteo).

Un turno se abre con un 'fondo inicial' (base en efectivo). Durante el turno, los
cobros en efectivo del libro 'pagos' se acumulan. Al cerrar:
    esperado_en_caja = fondo_inicial + efectivo cobrado en el turno
    diferencia       = efectivo_contado − esperado_en_caja   (sobrante / faltante)
Las transferencias NO entran a la caja física (se muestran solo como contexto).
Hay como máximo un turno abierto a la vez (cerrado IS NULL).
"""
import streamlit as st
from sqlalchemy import text
import html
from datetime import datetime

from db import engine, fmt_money, flash


# ── DB ───────────────────────────────────────────────────────────────────────────
def turno_abierto():
    """El turno abierto (cerrado IS NULL) como dict, o None."""
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT id, abierto, fondo_inicial FROM turnos_caja "
                "WHERE cerrado IS NULL ORDER BY id DESC LIMIT 1"
            )).mappings().first()
        return dict(row) if row else None
    except Exception:
        return None


def abrir_turno(fondo_inicial: int):
    """Abre un turno si no hay otro abierto (guard atómico)."""
    with engine.begin() as conn:
        existe = conn.execute(text(
            "SELECT 1 FROM turnos_caja WHERE cerrado IS NULL LIMIT 1"
        )).first()
        if existe:
            return False
        conn.execute(text("INSERT INTO turnos_caja (fondo_inicial) VALUES (:f)"),
                     {"f": int(fondo_inicial)})
    return True


def totales_turno(abierto, cerrado=None) -> dict:
    """{metodo: total} de los cobros entre 'abierto' y 'cerrado' (o ahora) desde
    'pagos'. Tolerante a fallos → {} si la tabla aún no existe."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT metodo, COALESCE(SUM(monto), 0) AS total FROM pagos "
                "WHERE fecha >= :ini AND (:fin IS NULL OR fecha <= :fin) GROUP BY metodo"
            ), {"ini": abierto, "fin": cerrado}).mappings().all()
        return {r["metodo"]: int(r["total"]) for r in rows}
    except Exception:
        return {}


def cerrar_turno(turno_id: int, efectivo_contado: int, nota: str = ""):
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE turnos_caja SET cerrado = NOW(), efectivo_contado = :c, nota = :n "
            "WHERE id = :id AND cerrado IS NULL"
        ), {"c": int(efectivo_contado), "n": (nota or "").strip() or None, "id": int(turno_id)})


def turnos_recientes(n: int = 8):
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, abierto, cerrado, fondo_inicial, efectivo_contado "
                "FROM turnos_caja WHERE cerrado IS NOT NULL ORDER BY id DESC LIMIT :n"
            ), {"n": n}).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── Helpers de formato ───────────────────────────────────────────────────────────
def _hora(dt) -> str:
    try:
        return dt.strftime("%H:%M")
    except Exception:
        return "—"


def _fechahora(dt) -> str:
    try:
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return "—"


def _metric(col, valor_html: str, label: str, clase: str = ""):
    col.markdown(
        f'<div class="metric-card"><div class="metric-value {clase}" '
        f'style="font-size:clamp(0.9rem,1.6vw,2rem); white-space:nowrap;">{valor_html}</div>'
        f'<div class="metric-label">{label}</div></div>',
        unsafe_allow_html=True,
    )


# ── Modal: cerrar turno ──────────────────────────────────────────────────────────
@st.dialog("💰 Cerrar turno")
def _dialog_cerrar(turno_id: int, fondo: int, efectivo: int):
    esperado = fondo + efectivo
    st.markdown(
        f'<div style="font-size:0.85rem; color:#374151;">'
        f'Fondo inicial <b>${fmt_money(fondo)}</b> + efectivo del turno '
        f'<b>${fmt_money(efectivo)}</b></div>'
        f'<div style="font-size:0.78rem; color:#6b7280; text-transform:uppercase; '
        f'letter-spacing:0.04em; margin-top:8px;">Esperado en caja</div>'
        f'<div style="font-family:\'Syne\',sans-serif; font-size:1.9rem; font-weight:800; '
        f'color:#1a1a1a; line-height:1.1;">${fmt_money(esperado)}</div>',
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    contado = int(st.number_input(
        "Efectivo contado en caja", min_value=0, value=esperado, step=1000,
        format="%d", key=f"contado_{turno_id}",
        help="Cuenta el dinero físico en la caja e ingrésalo aquí.",
    ) or 0)
    dif = contado - esperado
    if dif == 0:
        st.markdown('<div style="background:#dcfce7; border:1px solid #86efac; '
                    'border-radius:10px; padding:10px 14px; color:#14532d; font-weight:600;">'
                    '✓ Caja cuadrada.</div>', unsafe_allow_html=True)
    elif dif > 0:
        st.markdown('<div style="background:#dbeafe; border:1px solid #93c5fd; '
                    'border-radius:10px; padding:10px 14px; color:#1e3a8a; font-weight:600;">'
                    f'Sobrante: ${fmt_money(dif)}</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div style="background:#fee2e2; border:1px solid #fca5a5; '
                    'border-radius:10px; padding:10px 14px; color:#7f1d1d; font-weight:600;">'
                    f'Faltante: ${fmt_money(-dif)}</div>', unsafe_allow_html=True)

    nota = st.text_input("Nota (opcional)", key=f"nota_turno_{turno_id}",
                         placeholder="Ej: se retiraron $50.000 para compras…")

    st.markdown("<br>", unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    with c1:
        if st.button("✓ Cerrar turno", key=f"btn_confirmar_cerrar_turno_{turno_id}",
                     type="primary", use_container_width=True):
            cerrar_turno(turno_id, contado, nota)
            estado = "cuadrada" if dif == 0 else (f"sobrante ${fmt_money(dif)}" if dif > 0
                                                  else f"faltante ${fmt_money(-dif)}")
            flash(f"Turno cerrado · caja {estado}", "💰")
            st.rerun()
    with c2:
        if st.button("Volver", key=f"volver_cerrar_turno_{turno_id}", use_container_width=True):
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: CAJA
# ══════════════════════════════════════════════════════════════════════════════
def render():
    st.markdown('<div class="section-title">💰 Caja · turno</div>', unsafe_allow_html=True)

    turno = turno_abierto()

    if not turno:
        # ── Sin turno abierto: abrir uno ────────────────────────────────────────
        st.markdown(
            '<p style="color:#6b7280; font-size:0.9rem;">No hay ningún turno abierto. '
            'Abre uno con el fondo inicial (base en efectivo de la caja).</p>',
            unsafe_allow_html=True,
        )
        c1, c2 = st.columns([2, 1])
        with c1:
            fondo = int(st.number_input("Fondo inicial", min_value=0, value=0, step=1000,
                                        format="%d", key="fondo_inicial_nuevo") or 0)
        with c2:
            st.markdown('<div style="height:28px;"></div>', unsafe_allow_html=True)
            if st.button("💰 Abrir turno", key="btn_confirmar_abrir_turno",
                         type="primary", use_container_width=True):
                if abrir_turno(fondo):
                    flash(f"Turno abierto · fondo ${fmt_money(fondo)}", "💰")
                else:
                    flash("Ya hay un turno abierto", "⚠️")
                st.rerun()
    else:
        # ── Turno abierto: estado + cierre ──────────────────────────────────────
        fondo = int(turno["fondo_inicial"])
        tot = totales_turno(turno["abierto"])
        efvo = tot.get("efectivo", 0)
        transf = tot.get("transferencia", 0)
        otros = sum(v for k, v in tot.items() if k not in ("efectivo", "transferencia"))
        efvo += otros  # cualquier método inesperado se cuenta como efectivo en caja
        esperado = fondo + efvo

        st.markdown(f"""
        <div class="order-card" style="border-left:4px solid #16a34a; margin-bottom:1rem;">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <div>
              <div class="order-id">Turno abierto</div>
              <div style="font-family:'Syne',sans-serif; font-size:1.2rem; font-weight:800; color:#1a1a1a;">
                Desde las {_hora(turno["abierto"])}</div>
            </div>
            <div style="text-align:right;">
              <div class="metric-label">Esperado en caja</div>
              <div class="order-total" style="font-size:1.4rem;">${fmt_money(esperado)}</div>
            </div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        m1, m2, m3, m4 = st.columns(4)
        _metric(m1, f"${fmt_money(fondo)}", "Fondo inicial")
        _metric(m2, f"${fmt_money(efvo)}", "💵 Efectivo turno", "metric-green")
        _metric(m3, f"${fmt_money(transf)}", "💳 Transferencia", "metric-blue")
        _metric(m4, f"${fmt_money(efvo + transf)}", "Cobrado turno")

        st.markdown("<br>", unsafe_allow_html=True)
        a1, a2 = st.columns([2, 1])
        with a1:
            if st.button("💰 Cerrar turno (arqueo)", key=f"abrir_cerrar_turno_{turno['id']}",
                         type="primary", use_container_width=True):
                _dialog_cerrar(int(turno["id"]), fondo, efvo)
        with a2:
            if st.button("🔄 Actualizar", key="caja_refrescar", use_container_width=True):
                st.rerun()

    # ── Historial de turnos cerrados ────────────────────────────────────────────
    recientes = turnos_recientes()
    if recientes:
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="section-title">Turnos cerrados</div>', unsafe_allow_html=True)
        for t in recientes:
            efvo = totales_turno(t["abierto"], t["cerrado"]).get("efectivo", 0)
            esperado = int(t["fondo_inicial"]) + efvo
            contado = int(t["efectivo_contado"]) if t["efectivo_contado"] is not None else esperado
            dif = contado - esperado
            if dif == 0:
                color, etiqueta = "#16a34a", "cuadrada"
            elif dif > 0:
                color, etiqueta = "#2563eb", f"sobrante ${fmt_money(dif)}"
            else:
                color, etiqueta = "#dc2626", f"faltante ${fmt_money(-dif)}"
            st.markdown(f"""
            <div class="order-card" style="border-left:4px solid {color};">
              <div style="display:flex; justify-content:space-between; align-items:center;">
                <div style="font-size:0.85rem; color:#374151;">
                  {_fechahora(t["abierto"])} → {_fechahora(t["cerrado"])}
                  <span style="color:#9ca3af;"> · fondo ${fmt_money(t["fondo_inicial"])} · contado ${fmt_money(contado)}</span>
                </div>
                <div style="color:{color}; font-weight:700; font-size:0.85rem;">{html.escape(etiqueta)}</div>
              </div>
            </div>
            """, unsafe_allow_html=True)
