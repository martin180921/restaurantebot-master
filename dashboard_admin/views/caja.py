"""Vista de Caja: cierre de caja por turno (arqueo con apertura y conteo).

Un turno se abre con un 'monto de apertura' (base en efectivo). Durante el turno,
los cobros del libro 'pagos' se acumulan por método. Al cerrar se congela el arqueo:
    total_esperado = monto_apertura + efectivo_esperado   (lo que debe haber en caja)
    diferencia     = efectivo_real − total_esperado        (sobrante / faltante)
También se registran las transferencias verificadas en banco (transferencia_real)
para contrastarlas con lo esperado, aunque NO entran a la caja física de efectivo.
Hay como máximo un turno con estado='abierto' a la vez.
"""
import streamlit as st
from sqlalchemy import text
import html

import auth
from db import engine, fmt_money, flash


# ── DB ───────────────────────────────────────────────────────────────────────────
def cierre_activo():
    """El turno con estado='abierto' como dict, o None. Tolerante a fallos."""
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT id, fecha_apertura, monto_apertura FROM cierres_caja "
                "WHERE estado = 'abierto' ORDER BY id DESC LIMIT 1"
            )).mappings().first()
        return dict(row) if row else None
    except Exception:
        return None


def ingresos_esperados(fecha_apertura) -> dict:
    """{efectivo, transferencia} cobrados desde 'fecha_apertura' (libro 'pagos').

    efectivo_esperado      = SUM(monto) WHERE metodo='efectivo'      AND fecha >= apertura
    transferencia_esperada = SUM(monto) WHERE metodo='transferencia' AND fecha >= apertura
    Tolerante a fallos → ceros si la tabla aún no existe.
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT metodo, COALESCE(SUM(monto), 0) AS total FROM pagos "
                "WHERE fecha >= :ini GROUP BY metodo"
            ), {"ini": fecha_apertura}).mappings().all()
        por_metodo = {r["metodo"]: int(r["total"]) for r in rows}
    except Exception:
        por_metodo = {}
    return {
        "efectivo": por_metodo.get("efectivo", 0),
        "transferencia": por_metodo.get("transferencia", 0),
    }


def abrir_caja(monto_apertura: int) -> bool:
    """Abre un turno si no hay otro con estado='abierto' (guard atómico)."""
    # Candado de capacidad (RBAC): solo admin/caja gestionan el arqueo.
    if not auth.can("manage_caja"):
        return False
    with engine.begin() as conn:
        existe = conn.execute(text(
            "SELECT 1 FROM cierres_caja WHERE estado = 'abierto' LIMIT 1"
        )).first()
        if existe:
            return False
        conn.execute(text(
            "INSERT INTO cierres_caja (monto_apertura, estado) VALUES (:m, 'abierto')"
        ), {"m": int(monto_apertura)})
    return True


def cerrar_caja(cierre_id: int, efectivo_esperado: int, transferencia_esperada: int,
                efectivo_real: int, transferencia_real: int, diferencia: int):
    """Congela el arqueo y cierra el turno (estado='cerrado', fecha_cierre=NOW())."""
    # Candado de capacidad (RBAC): solo admin/caja gestionan el arqueo.
    if not auth.can("manage_caja"):
        return
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE cierres_caja SET "
            "  fecha_cierre = NOW(), "
            "  efectivo_esperado = :ee, transferencia_esperada = :te, "
            "  efectivo_real = :er, transferencia_real = :tr, "
            "  diferencia = :dif, estado = 'cerrado' "
            "WHERE id = :id AND estado = 'abierto'"
        ), {
            "ee": int(efectivo_esperado), "te": int(transferencia_esperada),
            "er": int(efectivo_real), "tr": int(transferencia_real),
            "dif": int(diferencia), "id": int(cierre_id),
        })


def cierres_recientes(n: int = 8):
    """Últimos turnos cerrados para el historial. Tolerante a fallos."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, fecha_apertura, fecha_cierre, monto_apertura, "
                "       efectivo_esperado, transferencia_esperada, "
                "       efectivo_real, transferencia_real, diferencia "
                "FROM cierres_caja WHERE estado = 'cerrado' ORDER BY id DESC LIMIT :n"
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


def _pill(bg: str, borde: str, color: str, texto: str):
    """Píldora de estado (cuadrada / faltante / sobrante)."""
    st.markdown(
        f'<div style="background:{bg}; border:1px solid {borde}; border-radius:10px; '
        f'padding:10px 14px; color:{color}; font-weight:600;">{texto}</div>',
        unsafe_allow_html=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: CAJA · CIERRE DE TURNO
# ══════════════════════════════════════════════════════════════════════════════
def render():
    # Guard de capacidad (defensa en profundidad): el router ya oculta Caja al mesero,
    # pero validamos también aquí por si la vista quedó forzada por query param.
    if not auth.can("manage_caja"):
        st.error("🔒 Acceso denegado")
        st.stop()
    # Botón slate (formal) para finalizar el turno, vía estilo por st-key.
    st.markdown(
        "<style>"
        ".st-key-btn_finalizar_cierre button{background:#1e293b !important;"
        "border-color:#1e293b !important;color:#fff !important;}"
        ".st-key-btn_finalizar_cierre button:hover{background:#0f172a !important;"
        "border-color:#0f172a !important;color:#fff !important;}"
        "</style>",
        unsafe_allow_html=True,
    )
    st.markdown('<div class="section-title">💰 Caja · cierre de turno</div>',
                unsafe_allow_html=True)

    cierre = cierre_activo()

    # ── ESTADO A: caja cerrada (sin turno activo) ───────────────────────────────
    if not cierre:
        st.markdown(
            '<div class="order-card" style="text-align:center; padding:1.6rem 1rem;">'
            '<div style="font-size:2rem; line-height:1;">🔒</div>'
            '<div style="font-family:\'Syne\',sans-serif; font-size:1.3rem; '
            'font-weight:800; color:#1a1a1a; margin-top:6px;">La caja se encuentra cerrada.</div>'
            '<div style="color:#6b7280; font-size:0.9rem; margin-top:4px;">Define la base en '
            'efectivo e inicia un nuevo turno para comenzar a operar.</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.markdown("<br>", unsafe_allow_html=True)

        monto_apertura = int(st.number_input(
            "Monto de Apertura (Base en Efectivo)", min_value=0, value=0, step=1000,
            format="%d", key="monto_apertura_nuevo",
            help="Efectivo con el que arranca la caja al inicio del turno.",
        ) or 0)
        if st.button("🟢 Abrir Caja / Iniciar Turno", key="btn_abrir_caja",
                     type="primary", use_container_width=True):
            if abrir_caja(monto_apertura):
                flash(f"Caja abierta · base ${fmt_money(monto_apertura)}", "🟢")
            else:
                flash("Ya hay un turno abierto", "⚠️")
            st.rerun()

    # ── ESTADO B: caja abierta (turno activo) ───────────────────────────────────
    else:
        monto_apertura = int(cierre["monto_apertura"])
        esp = ingresos_esperados(cierre["fecha_apertura"])
        efvo_esp = esp["efectivo"]
        transf_esp = esp["transferencia"]
        total_esperado = monto_apertura + efvo_esp

        # Banner de turno abierto.
        st.markdown(f"""
        <div class="order-card" style="border-left:4px solid #16a34a; margin-bottom:1rem;">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <div>
              <div class="order-id">Turno abierto</div>
              <div style="font-family:'Syne',sans-serif; font-size:1.2rem; font-weight:800; color:#1a1a1a;">
                Desde las {_hora(cierre["fecha_apertura"])}</div>
            </div>
            <div style="text-align:right;">
              <div class="metric-label">Esperado en caja</div>
              <div class="order-total" style="font-size:1.4rem;">${fmt_money(total_esperado)}</div>
            </div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        # Métricas en vivo del turno.
        m1, m2, m3 = st.columns(3)
        _metric(m1, f"${fmt_money(monto_apertura)}", "Base de Apertura")
        _metric(m2, f"${fmt_money(efvo_esp)}", "💵 Ventas Efectivo Esperadas", "metric-green")
        _metric(m3, f"${fmt_money(transf_esp)}", "💳 Ventas Transferencia Esperadas", "metric-blue")

        cols = st.columns([3, 1])
        with cols[1]:
            if st.button("🔄 Actualizar", key="btn_caja_refrescar", use_container_width=True):
                st.rerun()

        st.divider()

        # ── Formulario de cierre ────────────────────────────────────────────────
        with st.container(border=True):
            st.markdown(
                '<div style="font-family:\'Syne\',sans-serif; font-size:1.05rem; '
                'font-weight:800; color:#1a1a1a; margin-bottom:2px;">🔒 Formulario de Cierre de Caja</div>'
                '<div style="color:#6b7280; font-size:0.85rem; margin-bottom:10px;">Cuenta el dinero '
                'físico y verifica las transferencias en el banco antes de finalizar el turno.</div>',
                unsafe_allow_html=True,
            )

            f1, f2 = st.columns(2)
            with f1:
                efectivo_real = int(st.number_input(
                    "Efectivo Físico Contado ($)", min_value=0, value=total_esperado,
                    step=1000, format="%d", key=f"efectivo_real_{cierre['id']}",
                    help="Dinero en efectivo realmente contado en la caja.",
                ) or 0)
            with f2:
                transferencia_real = int(st.number_input(
                    "Transferencias Verificadas en Banco ($)", min_value=0, value=transf_esp,
                    step=1000, format="%d", key=f"transferencia_real_{cierre['id']}",
                    help="Transferencias confirmadas en la cuenta bancaria.",
                ) or 0)

            # Cálculo en vivo de la diferencia de caja (solo efectivo).
            diferencia = efectivo_real - total_esperado
            st.markdown("<div style='height:4px;'></div>", unsafe_allow_html=True)
            if diferencia == 0:
                _pill("#dcfce7", "#86efac", "#14532d", "✅ Caja cuadrada perfectamente.")
            elif diferencia < 0:
                _pill("#fef3c7", "#fcd34d", "#92400e",
                      f"⚠️ Faltante en caja: -${fmt_money(-diferencia)}")
            else:
                _pill("#dbeafe", "#93c5fd", "#1e3a8a",
                      f"ℹ️ Sobrante en caja: +${fmt_money(diferencia)}")

            # Contraste de transferencias (contexto; no afecta la caja física).
            dif_transf = transferencia_real - transf_esp
            if dif_transf == 0:
                transf_txt = "coinciden con lo esperado"
            elif dif_transf < 0:
                transf_txt = f"faltan ${fmt_money(-dif_transf)} por verificar"
            else:
                transf_txt = f"hay ${fmt_money(dif_transf)} de más sobre lo esperado"
            st.caption(f"💳 Transferencias verificadas vs. esperadas: {transf_txt}.")

            st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)
            if st.button("Finalizar Turno y Cerrar Caja", key="btn_finalizar_cierre",
                         use_container_width=True):
                cerrar_caja(int(cierre["id"]), efvo_esp, transf_esp,
                            efectivo_real, transferencia_real, diferencia)
                if diferencia == 0:
                    estado = "cuadrada"
                elif diferencia < 0:
                    estado = f"faltante ${fmt_money(-diferencia)}"
                else:
                    estado = f"sobrante ${fmt_money(diferencia)}"
                flash(f"Turno cerrado · caja {estado}", "💰")
                st.rerun()

    # ── Historial de turnos cerrados ────────────────────────────────────────────
    recientes = cierres_recientes()
    if recientes:
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="section-title">Turnos cerrados</div>', unsafe_allow_html=True)
        for t in recientes:
            dif = int(t["diferencia"] or 0)
            if dif == 0:
                color, etiqueta = "#16a34a", "cuadrada"
            elif dif > 0:
                color, etiqueta = "#2563eb", f"sobrante ${fmt_money(dif)}"
            else:
                color, etiqueta = "#dc2626", f"faltante ${fmt_money(-dif)}"
            efectivo_real = int(t["efectivo_real"]) if t["efectivo_real"] is not None else 0
            transf_real = int(t["transferencia_real"]) if t["transferencia_real"] is not None else 0
            st.markdown(f"""
            <div class="order-card" style="border-left:4px solid {color};">
              <div style="display:flex; justify-content:space-between; align-items:center;">
                <div style="font-size:0.85rem; color:#374151;">
                  {_fechahora(t["fecha_apertura"])} → {_fechahora(t["fecha_cierre"])}
                  <span style="color:#9ca3af;"> · base ${fmt_money(t["monto_apertura"])} · efectivo ${fmt_money(efectivo_real)} · transf. ${fmt_money(transf_real)}</span>
                </div>
                <div style="color:{color}; font-weight:700; font-size:0.85rem;">{html.escape(etiqueta)}</div>
              </div>
            </div>
            """, unsafe_allow_html=True)
