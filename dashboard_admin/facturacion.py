"""Facturación electrónica (bloque C del plan, ver docs/plan_facturacion_y_datafono.md).

APAGADA por defecto (ajuste 'facturacion_electronica' en Ajustes). Con el flag OFF el
recibo sigue siendo la CUENTA no fiscal de siempre: nada de este módulo se ejecuta desde
el cobro (ver views/pedidos.py::dialog_cobrar).

Un proveedor de facturación (PAC — Factus, Alegra, Siigo…) se esconde detrás de la
interfaz ProveedorFactura; ProveedorSimulado devuelve un CUFE/QR de mentira (sin llamar a
la DIAN ni a ningún PAC) para poder MOSTRARLE al restaurante cómo se vería su factura
antes de contratar un proveedor real. Cambiar el ajuste 'proveedor_factura' de 'simulado'
a uno real no toca la UI ni el recibo — solo el adapter que se instancia aquí.

Libro 'documentos_fiscales' (append-only, mismo espíritu que auditoria.py): una fila por
documento emitido/rechazado/anulado, enlazada a los pedidos que cobró. Los MEDIOS DE PAGO
de cada documento se leen del libro 'pagos' existente (pedidos.py) — no se duplican aquí.
"""
import json
import secrets

from sqlalchemy import text

from db import engine, RESTAURANTE_ID, facturacion_electronica, proveedor_factura


# ── Proveedores (interfaz enchufable) ────────────────────────────────────────────
class ProveedorFactura:
    """Contrato que debe cumplir cualquier PAC real. proveedor_activo() decide cuál
    instanciar según el ajuste 'proveedor_factura'."""

    nombre = "base"

    def emitir(self, doc: dict) -> dict:
        """doc: {tipo, monto, pedido_ids, cliente_doc, cliente_nombre, cliente_email}.
        Debe devolver {numero, cufe, qr_texto, pdf_url, estado, respuesta_cruda}."""
        raise NotImplementedError

    def anular(self, numero: str, motivo: str) -> dict:
        """Nota crédito / anulación de un documento ya emitido. Devuelve {estado}."""
        raise NotImplementedError


class ProveedorSimulado(ProveedorFactura):
    """DEMO: genera un CUFE y un QR de mentira sin llamar a ningún servicio externo.
    Sirve para que el restaurante vea e imprima cómo luciría su factura antes de
    contratar un PAC real. El 'numero'/'cufe' que produce NO es válido ante la DIAN."""

    nombre = "simulado"

    def emitir(self, doc: dict) -> dict:
        consecutivo = secrets.randbelow(900000) + 100000
        cufe = secrets.token_hex(24)
        return {
            "numero": f"SETP{consecutivo}",
            "cufe": cufe,
            "qr_texto": f"https://catalogo-vpfe.dian.gov.co/document/searchqr?documentkey={cufe}",
            "pdf_url": None,
            "estado": "emitido",
            "respuesta_cruda": {"simulado": True,
                               "nota": "CUFE de prueba, no valido ante la DIAN"},
        }

    def anular(self, numero: str, motivo: str) -> dict:
        return {"estado": "anulado"}


# Proveedores reales (Factus, Alegra, Siigo…) se agregan aquí cuando el Registro de
# Decisiones de Notion resuelva cuál. Nombre desconocido/sin implementar → simulado
# (nunca deja al restaurante sin poder probar la función).
_PROVEEDORES = {"simulado": ProveedorSimulado}


def proveedor_activo() -> ProveedorFactura:
    """Instancia del proveedor configurado en Ajustes."""
    return _PROVEEDORES.get(proveedor_factura(), ProveedorSimulado)()


# ── Libro 'documentos_fiscales' ──────────────────────────────────────────────────
def registrar_documento(pedido_ids, tipo: str, monto: int, resultado: dict,
                        cliente_doc=None, cliente_nombre=None, cliente_email=None) -> int:
    """Inserta la fila del documento (emitido o rechazado) y devuelve su id."""
    ids = [int(i) for i in (pedido_ids or [])]
    with engine.begin() as conn:
        return int(conn.execute(text("""
            INSERT INTO documentos_fiscales
                (pedido_ids, tipo, numero, cufe, qr_texto, estado, proveedor,
                 cliente_doc, cliente_nombre, cliente_email, monto, respuesta_cruda,
                 restaurante_id)
            VALUES
                (:pedido_ids, :tipo, :numero, :cufe, :qr_texto, :estado, :proveedor,
                 :cliente_doc, :cliente_nombre, :cliente_email, :monto,
                 CAST(:resp AS JSONB), :rid)
            RETURNING id
        """), {
            "pedido_ids": json.dumps(ids),
            "tipo": str(tipo)[:10],
            "numero": resultado.get("numero"),
            "cufe": resultado.get("cufe"),
            "qr_texto": resultado.get("qr_texto"),
            "estado": resultado.get("estado", "borrador"),
            "proveedor": proveedor_factura(),
            "cliente_doc": (str(cliente_doc).strip()[:30] or None) if cliente_doc else None,
            "cliente_nombre": (str(cliente_nombre).strip()[:120] or None) if cliente_nombre else None,
            "cliente_email": (str(cliente_email).strip()[:120] or None) if cliente_email else None,
            "monto": int(monto),
            "resp": json.dumps(resultado.get("respuesta_cruda") or {},
                              ensure_ascii=False, default=str),
            "rid": int(RESTAURANTE_ID),
        }).scalar_one())


def emitir_para_cobro(pedido_ids, monto: int, tipo: str = "pos",
                      cliente_doc=None, cliente_nombre=None, cliente_email=None):
    """Emite (con el proveedor activo) y registra el documento de un cobro RECIÉN
    COMMITEADO. None si la facturación está apagada (nada que hacer). Tolerante a
    fallos del proveedor: si emitir() lanza, igual se registra el intento como
    'rechazado' con el error en 'respuesta_cruda', en vez de tumbar el cobro (que YA
    quedó asentado en 'pagos' antes de llegar aquí)."""
    if not facturacion_electronica():
        return None
    doc = {"tipo": tipo, "monto": int(monto), "pedido_ids": [int(i) for i in pedido_ids],
          "cliente_doc": cliente_doc, "cliente_nombre": cliente_nombre,
          "cliente_email": cliente_email}
    try:
        resultado = proveedor_activo().emitir(doc)
    except Exception as exc:
        resultado = {"estado": "rechazado", "numero": None, "cufe": None, "qr_texto": None,
                    "respuesta_cruda": {"error": str(exc)[:300]}}
    registrar_documento(pedido_ids, tipo, monto, resultado,
                        cliente_doc, cliente_nombre, cliente_email)
    return resultado


def documentos_recientes(n: int = 100, restaurante_id=None) -> list:
    """Últimos documentos fiscales (emitidos/rechazados/anulados), más nuevo primero.
    Tolerante a fallos → lista vacía si la tabla aún no existe."""
    rid = int(restaurante_id if restaurante_id is not None else RESTAURANTE_ID)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, pedido_ids, tipo, numero, cufe, qr_texto, estado, proveedor,
                       cliente_doc, cliente_nombre, cliente_email, monto, fecha
                FROM documentos_fiscales
                WHERE restaurante_id = :rid
                ORDER BY fecha DESC LIMIT :n
            """), {"rid": rid, "n": int(n)}).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []


def anular_documento(doc_id: int, motivo: str) -> tuple:
    """Anula (nota crédito) un documento ya EMITIDO, vía el proveedor que lo emitió.
    Devuelve (ok, mensaje)."""
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT numero, estado, proveedor FROM documentos_fiscales WHERE id = :id"
        ), {"id": int(doc_id)}).mappings().first()
    if not row:
        return False, "Documento no encontrado."
    if row["estado"] != "emitido":
        return False, f"Solo se pueden anular documentos emitidos (estado actual: {row['estado']})."
    cls = _PROVEEDORES.get(row["proveedor"], ProveedorSimulado)
    try:
        resultado = cls().anular(row["numero"], motivo)
    except Exception as exc:
        return False, f"No se pudo anular: {exc}"
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE documentos_fiscales SET estado = :e WHERE id = :id"
        ), {"e": resultado.get("estado", "anulado"), "id": int(doc_id)})
    return True, "Documento anulado."
