"""Control-state error hierarchy."""


class ControlStateError(RuntimeError):
    """Base class for control-state failures."""


class ControlDatabaseOwnedError(ControlStateError):
    """Another process already owns the configured Control Database."""


class ControlIntegrityError(ControlStateError):
    """Existing Control Database state failed an integrity invariant."""


class AutoAgentCapacityError(ControlStateError):
    """The bounded durable AutoAgent audit authority has no admission room."""


class AutoAgentActiveCapacityError(AutoAgentCapacityError):
    """The bounded set of concurrently running AutoAgent sessions is full."""


class RunIntegrityIncidentError(ControlIntegrityError):
    """A rejected Run mutation was durably recorded as an integrity incident."""

    def __init__(self, message: str, *, incident_id: str) -> None:
        super().__init__(message)
        self.incident_id = incident_id


class RepositoryClosedError(ControlStateError):
    """A command was attempted after repository shutdown."""


class DeliveryCapacityExceededError(ControlStateError):
    """A Delivery admission would exceed the bounded outstanding capacity."""


class DeliveryResendNotSettledError(ControlStateError):
    """Resend was requested for a Delivery that still has a live Item."""


class TurnConversationUnavailableError(ControlIntegrityError):
    """A Conversation is quarantined after losing safe execution ownership."""


__all__ = [
    "AutoAgentActiveCapacityError",
    "AutoAgentCapacityError",
    "ControlDatabaseOwnedError",
    "ControlIntegrityError",
    "DeliveryCapacityExceededError",
    "DeliveryResendNotSettledError",
    "RunIntegrityIncidentError",
    "ControlStateError",
    "RepositoryClosedError",
    "TurnConversationUnavailableError",
]
