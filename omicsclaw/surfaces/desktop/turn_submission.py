"""Strict Desktop ``POST /v1/turns`` multipart ingress Adapter.

The Module owns only transport parsing and the process-local byte capability.
It never writes Workspace files, registers ``received_files``, computes a
durable identity, or renders provider media.  Those responsibilities remain in
the Attachment Store and Control Runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import threading
from typing import Any, AsyncIterator, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)
from multipart.exceptions import MultipartParseError
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request

from omicsclaw.attachments import SourceAttachmentDescriptorV1
from omicsclaw.control import RawContentBlockV1, RawInboundV1


DESKTOP_TURN_SUBMISSION_SCHEMA_VERSION = 1
DEFAULT_MAX_REQUEST_BYTES = 2 * 1024 * 1024
DEFAULT_MULTIPART_OVERHEAD_BYTES = 64 * 1024
DEFAULT_MULTIPART_READ_TIMEOUT_SECONDS = 60
DEFAULT_SOURCE_CHUNK_BYTES = 64 * 1024
DEFAULT_MAX_JSON_NESTING = 64
DESKTOP_MAX_ATTACHMENTS = 8
_OPAQUE_ID_PATTERN = r"^[0-9a-f]{32}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ALLOWED_IMAGE_MEDIA_TYPES = Literal[
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
]


class DesktopMultipartError(ValueError):
    """Stable transport rejection raised before Control acceptance."""

    def __init__(self, code: str, *, status_code: int = 422) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class _TransportTooLarge(MultiPartException):
    pass


class _TransportReadFailed(MultiPartException):
    pass


class _RequestPartTooLarge(MultiPartException):
    pass


class _InvalidRequestEncoding(MultiPartException):
    pass


class _BoundedMultiPartParser(MultiPartParser):
    """Bound field bytes without depending on Starlette's newer constructor.

    Starlette 0.27, which is still admitted by the repository's FastAPI floor,
    has the same parser callbacks but no ``max_part_size`` constructor argument.
    Keeping the limit in this Adapter preserves that compatible environment
    while still rejecting the JSON request part as soon as it crosses its cap.
    """

    def __init__(self, *args, max_request_part_size: int, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._max_request_part_size = max_request_part_size
        self.multipart_completed = False

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        message_bytes = data[start:end]
        if self._current_part.file is None:
            if (
                len(self._current_part.data) + len(message_bytes)
                > self._max_request_part_size
            ):
                raise _RequestPartTooLarge(
                    "Desktop multipart request document exceeded limit"
                )
            # Starlette 0.27 stores ``bytes`` here; newer releases use
            # ``bytearray``. In-place addition is valid for both shapes.
            self._current_part.data += message_bytes
        else:
            self._file_parts_to_write.append((self._current_part, message_bytes))

    def on_part_end(self) -> None:
        if self._current_part.file is None:
            try:
                value = bytes(self._current_part.data).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _InvalidRequestEncoding(
                    "Desktop multipart request document must be UTF-8"
                ) from exc
            self.items.append((self._current_part.field_name, value))
            return
        super().on_part_end()

    def on_end(self) -> None:
        self.multipart_completed = True


class DesktopRawContentBlockV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["text"]
    text: str = Field(max_length=1_000_000)


class DesktopSourceAttachmentDescriptorV1(BaseModel):
    """Desktop-tight descriptor; full size and digest are mandatory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt = Field(ge=1, le=1)
    ordinal: StrictInt = Field(ge=0, le=0xFFFFFFFF)
    source_attachment_id: str = Field(pattern=_OPAQUE_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=255)
    declared_media_type: _ALLOWED_IMAGE_MEDIA_TYPES
    declared_size: StrictInt = Field(gt=0, le=0xFFFFFFFFFFFFFFFF)
    declared_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_domain_contract(self) -> "DesktopSourceAttachmentDescriptorV1":
        self.to_domain()
        return self

    def to_domain(self) -> SourceAttachmentDescriptorV1:
        return SourceAttachmentDescriptorV1(
            schema_version=self.schema_version,
            ordinal=self.ordinal,
            source_attachment_id=self.source_attachment_id,
            display_name=self.display_name,
            declared_media_type=self.declared_media_type,
            declared_size=self.declared_size,
            declared_sha256=self.declared_sha256,
        )


class DesktopTurnSubmissionV1(BaseModel):
    """Implemented multipart subset of the accepted Desktop submission shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt = Field(ge=1, le=1)
    conversation_id: str | None = Field(default=None, pattern=_OPAQUE_ID_PATTERN)
    project_command: None = None
    content: tuple[DesktopRawContentBlockV1, ...] = Field(
        default_factory=tuple,
        max_length=32,
    )
    attachment_descriptors: tuple[DesktopSourceAttachmentDescriptorV1, ...] = Field(
        min_length=1,
        max_length=DESKTOP_MAX_ATTACHMENTS,
    )
    file_selections: tuple[Mapping[str, Any], ...] = Field(
        default_factory=tuple,
        max_length=32,
    )
    requested_options: Mapping[str, Any] = Field(default_factory=dict, max_length=32)
    retry_of_turn_id: None = None

    @model_validator(mode="after")
    def validate_implemented_subset(self) -> "DesktopTurnSubmissionV1":
        descriptors = self.attachment_descriptors
        if tuple(value.ordinal for value in descriptors) != tuple(
            range(len(descriptors))
        ):
            raise ValueError("attachment descriptor ordinals must be contiguous")
        source_ids = tuple(value.source_attachment_id for value in descriptors)
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("source_attachment_id values must be unique")
        if self.file_selections:
            raise ValueError("file selections are not supported")
        if self.requested_options:
            raise ValueError("requested options are not supported by this slice")
        return self

    def to_raw_inbound(
        self,
        *,
        source_request_id: str,
        source_namespace: str,
        reply_target: Mapping[str, Any],
    ) -> RawInboundV1:
        return RawInboundV1(
            schema_version=1,
            surface="desktop",
            source_namespace=source_namespace,
            source_request_id=source_request_id,
            reply_target=reply_target,
            content=tuple(
                RawContentBlockV1(kind=block.kind, text=block.text)
                for block in self.content
            ),
            explicit_conversation_id=self.conversation_id,
            attachments=tuple(
                descriptor.to_domain() for descriptor in self.attachment_descriptors
            ),
            requested_options={},
            retry_of_turn_id=self.retry_of_turn_id,
        )


class DesktopAttachmentReferenceV1(BaseModel):
    """Bounded read-only projection of one accepted Attachment Record.

    Carries reference facts only: no bytes, no staging or Blob path, and no
    provider handle.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    attachment_id: str = Field(pattern=_OPAQUE_ID_PATTERN)
    ordinal: StrictInt = Field(ge=0)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_size: StrictInt = Field(ge=0)
    display_name: str
    media_type: str


class DesktopTurnAcceptedV1(BaseModel):
    """Stable acceptance response shared by 200 duplicate and 202 novel paths."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    turn_id: str = Field(pattern=_OPAQUE_ID_PATTERN)
    conversation_id: str = Field(pattern=_OPAQUE_ID_PATTERN)
    status: Literal[
        "queued",
        "running",
        "succeeded",
        "failed",
        "canceled",
        "interrupted",
    ]
    duplicate: bool
    receipt_revision: StrictInt = Field(ge=0)
    accepted_at_ms: StrictInt = Field(ge=0)
    attachments: tuple[DesktopAttachmentReferenceV1, ...] = ()


class DesktopMultipartAttachmentSource:
    """One-shot ``UploadFile`` Adapter implementing InboundAttachmentSource."""

    def __init__(
        self,
        form: FormData,
        uploads: Mapping[str, UploadFile],
        *,
        chunk_bytes: int = DEFAULT_SOURCE_CHUNK_BYTES,
    ) -> None:
        if (
            not isinstance(chunk_bytes, int)
            or isinstance(chunk_bytes, bool)
            or chunk_bytes <= 0
        ):
            raise ValueError("chunk_bytes must be a positive integer")
        self._form = form
        self._uploads = dict(uploads)
        self._chunk_bytes = chunk_bytes
        self._opened: list[str] = []
        self._closed = False

    @property
    def opened_source_ids(self) -> tuple[str, ...]:
        return tuple(self._opened)

    @property
    def closed(self) -> bool:
        return self._closed

    async def open(self, source_attachment_id: str) -> AsyncIterator[bytes]:
        if self._closed:
            raise ValueError("Desktop multipart source is closed")
        upload = self._uploads.get(source_attachment_id)
        if upload is None:
            raise ValueError("Desktop multipart source ID is unknown")
        if source_attachment_id in self._opened:
            raise ValueError("Desktop multipart source was already opened")
        self._opened.append(source_attachment_id)
        try:
            await upload.seek(0)
            while True:
                chunk = await upload.read(self._chunk_bytes)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise TypeError("Desktop multipart source returned non-bytes")
                yield chunk
        finally:
            await upload.close()

    async def aclose(self) -> None:
        if self._closed:
            return
        first_error: BaseException | None = None
        try:
            # Attempt every close even if one temporary file reports an error.
            # ``UploadFile.close`` is idempotent, so files already closed by
            # ``open`` are safe to visit again.
            for upload in self._uploads.values():
                try:
                    await upload.close()
                except BaseException as exc:  # preserve cancellation after cleanup
                    if first_error is None:
                        first_error = exc
        finally:
            self._closed = True
        if first_error is not None:
            raise first_error


@dataclass(slots=True)
class DesktopMultipartUpload:
    submission: DesktopTurnSubmissionV1
    source: DesktopMultipartAttachmentSource

    async def aclose(self) -> None:
        await self.source.aclose()


class _DesktopMultipartLease:
    def __init__(self, capacity: "DesktopMultipartCapacity") -> None:
        self._capacity = capacity
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._capacity._release()


class DesktopMultipartCapacity:
    """Pessimistic process-local cap for concurrent multipart spools."""

    def __init__(self, *, max_active: int = 2) -> None:
        if (
            not isinstance(max_active, int)
            or isinstance(max_active, bool)
            or max_active <= 0
        ):
            raise ValueError("max_active must be a positive integer")
        self.max_active = max_active
        self._active = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> _DesktopMultipartLease | None:
        with self._lock:
            if self._active >= self.max_active:
                return None
            self._active += 1
        return _DesktopMultipartLease(self)

    def _release(self) -> None:
        with self._lock:
            if self._active <= 0:
                raise RuntimeError("Desktop multipart capacity underflow")
            self._active -= 1


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _reject_excessive_json_nesting(
    document: str,
    *,
    maximum: int = DEFAULT_MAX_JSON_NESTING,
) -> None:
    """Bound JSON structure depth independently of Python's recursion setting."""

    depth = 0
    in_string = False
    escaped = False
    for character in document:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > maximum:
                raise DesktopMultipartError("invalid_request_json")
        elif character in "]}":
            depth -= 1


async def _bounded_request_stream(request: Request, maximum: int):
    observed = 0
    try:
        async for chunk in request.stream():
            observed += len(chunk)
            if observed > maximum:
                raise _TransportTooLarge("Desktop multipart transport exceeded limit")
            yield chunk
    except MultiPartException:
        raise
    except Exception as exc:
        raise _TransportReadFailed("Desktop multipart transport read failed") from exc


def _close_parser_provisional_files(parser: MultiPartParser) -> None:
    """Close every spool the parser ever created, including unreturned parts."""

    for spool in tuple(getattr(parser, "_files_to_close_on_error", ())):
        try:
            spool.close()
        except Exception:
            pass


async def _close_form_uploads(form: FormData) -> None:
    """Attempt every visible UploadFile close without short-circuiting cleanup."""

    observed: set[int] = set()
    for _name, value in form.multi_items():
        if not isinstance(value, UploadFile) or id(value) in observed:
            continue
        observed.add(id(value))
        try:
            await value.close()
        except BaseException:
            # The original parse/validation failure retains authority.  The
            # underlying provisional spool gets one final synchronous close below.
            pass


async def decode_desktop_multipart_submission(
    request: Request,
    *,
    max_attachments: int,
    max_batch_bytes: int,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
    multipart_overhead_bytes: int = DEFAULT_MULTIPART_OVERHEAD_BYTES,
) -> DesktopMultipartUpload:
    """Decode one conditionally gated, strictly bounded multipart request."""

    for name, value in (
        ("max_attachments", max_attachments),
        ("max_batch_bytes", max_batch_bytes),
        ("max_request_bytes", max_request_bytes),
        ("multipart_overhead_bytes", multipart_overhead_bytes),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if max_attachments > DESKTOP_MAX_ATTACHMENTS:
        raise ValueError("Desktop multipart max_attachments exceeds wire contract")

    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise DesktopMultipartError("multipart_content_type_required", status_code=415)

    transport_maximum = max_batch_bytes + max_request_bytes + multipart_overhead_bytes
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1:
        raise DesktopMultipartError("invalid_content_length", status_code=400)
    if lengths:
        try:
            declared_length = int(lengths[0])
        except ValueError as exc:
            raise DesktopMultipartError(
                "invalid_content_length", status_code=400
            ) from exc
        if declared_length < 0:
            raise DesktopMultipartError("invalid_content_length", status_code=400)
        if declared_length > transport_maximum:
            raise DesktopMultipartError("multipart_too_large", status_code=413)

    parser = _BoundedMultiPartParser(
        request.headers,
        _bounded_request_stream(request, transport_maximum),
        max_files=max_attachments,
        max_fields=1,
        max_request_part_size=max_request_bytes,
    )
    try:
        form = await parser.parse()
    except _TransportTooLarge as exc:
        _close_parser_provisional_files(parser)
        raise DesktopMultipartError("multipart_too_large", status_code=413) from exc
    except _RequestPartTooLarge as exc:
        _close_parser_provisional_files(parser)
        raise DesktopMultipartError(
            "request_document_too_large", status_code=413
        ) from exc
    except _InvalidRequestEncoding as exc:
        _close_parser_provisional_files(parser)
        raise DesktopMultipartError(
            "invalid_request_encoding", status_code=400
        ) from exc
    except _TransportReadFailed as exc:
        _close_parser_provisional_files(parser)
        raise DesktopMultipartError("multipart_read_failed", status_code=400) from exc
    except MultipartParseError as exc:
        _close_parser_provisional_files(parser)
        raise DesktopMultipartError("invalid_multipart", status_code=400) from exc
    except MultiPartException as exc:
        _close_parser_provisional_files(parser)
        raise DesktopMultipartError("invalid_multipart") from exc
    except BaseException:
        # Starlette closes these handles for MultiPartException only. Disk I/O,
        # cancellation, or an unexpected parser failure must not strand spools.
        _close_parser_provisional_files(parser)
        raise

    try:
        if not parser.multipart_completed:
            raise DesktopMultipartError("invalid_multipart", status_code=400)
        provisional_spools = {
            id(spool)
            for spool in tuple(getattr(parser, "_files_to_close_on_error", ()))
        }
        visible_spools = {
            id(value.file)
            for _name, value in form.multi_items()
            if isinstance(value, UploadFile)
        }
        if provisional_spools != visible_spools:
            raise DesktopMultipartError("invalid_multipart", status_code=400)
        request_parts: list[str] = []
        uploads: dict[str, UploadFile] = {}
        for name, value in form.multi_items():
            if isinstance(value, UploadFile):
                if name in uploads:
                    raise DesktopMultipartError("duplicate_attachment_part")
                uploads[name] = value
            else:
                if name != "request":
                    raise DesktopMultipartError("unexpected_multipart_field")
                request_parts.append(value)
        if len(request_parts) != 1:
            raise DesktopMultipartError("one_request_part_required")
        if len(request_parts[0].encode("utf-8")) > max_request_bytes:
            raise DesktopMultipartError(
                "request_document_too_large",
                status_code=413,
            )
        _reject_excessive_json_nesting(request_parts[0])
        try:
            document = json.loads(
                request_parts[0],
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
            raise DesktopMultipartError("invalid_request_json") from exc
        if not isinstance(document, dict):
            raise DesktopMultipartError("invalid_request_json")
        try:
            submission = DesktopTurnSubmissionV1.model_validate(document)
        except (ValidationError, RecursionError) as exc:
            raise DesktopMultipartError("invalid_inbound") from exc
        if len(submission.attachment_descriptors) > max_attachments:
            raise DesktopMultipartError("attachment_count_exceeded")
        expected_ids = tuple(
            descriptor.source_attachment_id
            for descriptor in submission.attachment_descriptors
        )
        if set(uploads) != set(expected_ids) or len(uploads) != len(expected_ids):
            raise DesktopMultipartError("attachment_parts_mismatch")
        source = DesktopMultipartAttachmentSource(form, uploads)
        return DesktopMultipartUpload(submission=submission, source=source)
    except BaseException:
        await _close_form_uploads(form)
        # A syntactically incomplete trailing file may be absent from FormData
        # even though python-multipart already allocated its spool.
        _close_parser_provisional_files(parser)
        raise


__all__ = [
    "DEFAULT_MAX_REQUEST_BYTES",
    "DEFAULT_MAX_JSON_NESTING",
    "DEFAULT_MULTIPART_OVERHEAD_BYTES",
    "DEFAULT_MULTIPART_READ_TIMEOUT_SECONDS",
    "DESKTOP_TURN_SUBMISSION_SCHEMA_VERSION",
    "DesktopMultipartCapacity",
    "DesktopMultipartError",
    "DesktopMultipartUpload",
    "DesktopSourceAttachmentDescriptorV1",
    "DesktopAttachmentReferenceV1",
    "DesktopTurnAcceptedV1",
    "DesktopTurnSubmissionV1",
    "decode_desktop_multipart_submission",
]
