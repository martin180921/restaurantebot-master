from sqlalchemy import Column, Integer, String, Boolean, Text, TIMESTAMP
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class Menu(Base):
    __tablename__ = "menu"
    id      = Column(Integer, primary_key=True, autoincrement=True)
    nombre  = Column(String(100), nullable=False)
    precio  = Column(Integer, nullable=False)
    activo  = Column(Boolean, nullable=False, default=True)
    orden   = Column(Integer, nullable=False, default=0)


class Sesion(Base):
    __tablename__ = "sesiones"
    numero      = Column(String(50), primary_key=True)
    estado      = Column(String(30), nullable=False, default="inicio")
    carrito     = Column(Text, nullable=False, default="[]")
    actualizado = Column(TIMESTAMP, nullable=False, server_default=func.now())


class Pedido(Base):
    __tablename__ = "pedidos"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    numero_cliente  = Column(String(50), nullable=False)
    items           = Column(Text, nullable=False)
    total           = Column(Integer, nullable=False)
    estado          = Column(String(30), nullable=False, default="pendiente")
