from enum import StrEnum


class Role(StrEnum):
    """Perfis de usuário emitidos pelo serviço de Ordens."""

    ATENDENTE = "Atendente"
    MECANICO = "Mecanico"
    ADMINISTRADOR = "Administrador"
