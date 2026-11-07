"""Erreurs du moteur d'intégration, indépendantes du TMS concerné."""


class TmsError(Exception):
    """Erreur générale lors d'un échange avec un TMS."""


class TmsUnavailableError(TmsError):
    """Le TMS ne répond pas ou signale une erreur serveur."""


class TmsAuthError(TmsError):
    """Le TMS refuse les identifiants fournis."""


class TmsResponseError(TmsError):
    """La réponse du TMS est refusée, illisible, incomplète ou inattendue."""


class TmsConfigError(Exception):
    """La configuration d'un TMS est absente ou invalide."""
