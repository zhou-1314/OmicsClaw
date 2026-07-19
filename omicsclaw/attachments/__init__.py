"""Public Interface for immutable accepted Attachment storage."""

from .models import (
    AttachmentBatchCommitment,
    AttachmentBatchPublication,
    AttachmentReferenceV1,
    InboundAttachmentSource,
    SourceAttachmentDescriptorV1,
)
from .store import (
    AttachmentError,
    AttachmentIntegrityError,
    AttachmentNotAcceptedError,
    AttachmentRejectedError,
    AttachmentStore,
    AttachmentStoreClosedError,
    AttachmentStoreError,
    AttachmentStoreRecoveryResult,
    AttachmentValidationError,
)

__all__ = [
    "AttachmentBatchCommitment",
    "AttachmentBatchPublication",
    "AttachmentError",
    "AttachmentIntegrityError",
    "AttachmentNotAcceptedError",
    "AttachmentRejectedError",
    "AttachmentReferenceV1",
    "AttachmentStore",
    "AttachmentStoreClosedError",
    "AttachmentStoreError",
    "AttachmentStoreRecoveryResult",
    "AttachmentValidationError",
    "InboundAttachmentSource",
    "SourceAttachmentDescriptorV1",
]
