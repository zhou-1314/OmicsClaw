from __future__ import annotations

from dataclasses import fields, FrozenInstanceError

import pytest

from omicsclaw.attachments import AttachmentReferenceV1, SourceAttachmentDescriptorV1


@pytest.mark.parametrize(
    "source_attachment_id",
    ("..", "../secret", "/tmp/upload.png", "https://example.test/file"),
)
def test_source_descriptor_rejects_path_and_url_handles(source_attachment_id):
    with pytest.raises(ValueError, match="unsafe"):
        SourceAttachmentDescriptorV1(
            schema_version=1,
            ordinal=0,
            source_attachment_id=source_attachment_id,
            display_name="safe.png",
            declared_media_type=None,
            declared_size=None,
            declared_sha256=None,
        )


def test_source_descriptor_contract_is_frozen_and_content_free():
    descriptor = SourceAttachmentDescriptorV1(
        schema_version=1,
        ordinal=0,
        source_attachment_id="telegram:file-123",
        display_name="safe.png",
        declared_media_type="image/png",
        declared_size=10,
        declared_sha256="a" * 64,
    )

    assert tuple(field.name for field in fields(descriptor)) == (
        "schema_version",
        "ordinal",
        "source_attachment_id",
        "display_name",
        "declared_media_type",
        "declared_size",
        "declared_sha256",
    )
    assert not {
        "path",
        "url",
        "credential",
        "bytes",
        "payload",
    }.intersection(descriptor.to_json_dict())
    with pytest.raises(FrozenInstanceError):
        descriptor.ordinal = 1


@pytest.mark.parametrize("display_name", (".", "..", "../bad.png"))
def test_attachment_reference_rejects_unsafe_display_names(display_name):
    with pytest.raises(ValueError, match="safe"):
        AttachmentReferenceV1(
            schema_version=1,
            attachment_id="1" * 32,
            ordinal=0,
            content_sha256="2" * 64,
            byte_size=10,
            display_name=display_name,
            media_type="image/png",
        )
