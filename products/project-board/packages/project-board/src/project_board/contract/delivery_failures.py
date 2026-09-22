from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class DeliveryFailureTarget:
    field: str
    value: str
    field_source: str
    value_source: str

    def as_payload(self) -> dict[str, str]:
        return {
            "field": self.field,
            "value": self.value,
            "field_source": self.field_source,
            "value_source": self.value_source,
        }


def _value_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def _source_text(value: Any) -> str:
    text = str(value or "").strip()
    if len(text.encode("utf-8")) > 128:
        return ""
    if not all(character.isalnum() or character in "._-" for character in text):
        return ""
    return text


def _detail_target(code: str, details: Mapping[str, Any]) -> tuple[str, str, str]:
    field = str(details.get("field") or "").strip()
    if field:
        return field, "details.field", field

    fields = details.get("fields")
    if isinstance(fields, (list, tuple, set)):
        names = [str(item or "").strip() for item in fields]
        names = [item for item in names if item]
        if names:
            return ",".join(names), "details.fields", ""

    candidates = (
        ("recipient", "recipient"),
        ("path", "path"),
        ("work_ref", "work_ref"),
        (
            "object_ref",
            "work_ref" if "work_ref" in str(code or "") else "object_ref",
        ),
        ("resource_ref", "resource_ref"),
        ("resource", "resource"),
        ("operation", "operation"),
        ("required_role", "required_role"),
    )
    for detail_key, field_name in candidates:
        if detail_key in details and details.get(detail_key) not in (None, ""):
            return field_name, f"details.{detail_key}", detail_key
    return "", "unreported", ""


def resolve_delivery_failure_target(
    failure: Mapping[str, Any],
) -> DeliveryFailureTarget:
    """Recover the concrete rejected input without inventing missing evidence."""

    details = (
        dict(failure.get("details") or {})
        if isinstance(failure.get("details"), Mapping)
        else {}
    )
    code = str(failure.get("code") or "")
    reported_field = str(failure.get("field") or "").strip()
    reported_source = _source_text(failure.get("field_source"))
    reported_value_source = _source_text(failure.get("value_source"))
    reported_value = failure.get("value")

    detail_field, detail_source, detail_key = _detail_target(code, details)
    legacy_default = (
        not reported_source
        and reported_field in {"message", "payload"}
        and reported_value in (None, "")
    )
    if detail_field and (not reported_field or legacy_default):
        field = detail_field
        field_source = detail_source
        selected_detail_key = detail_key
    elif reported_field and not legacy_default:
        field = reported_field
        field_source = reported_source or "failure.field"
        selected_detail_key = ""
    else:
        field = ""
        field_source = "unreported"
        selected_detail_key = ""

    if reported_value_source:
        value = _value_text(reported_value)
        value_source = reported_value_source
    elif reported_value not in (None, ""):
        value = _value_text(reported_value)
        value_source = "failure.value"
    elif "value" in details:
        value = _value_text(details.get("value"))
        value_source = "details.value"
    elif selected_detail_key and selected_detail_key in details:
        value = _value_text(details.get(selected_detail_key))
        value_source = f"details.{selected_detail_key}"
    elif field and field in details:
        value = _value_text(details.get(field))
        value_source = "details.named_field"
    elif field == "work_ref" and "object_ref" in details:
        value = _value_text(details.get("object_ref"))
        value_source = "details.object_ref"
    elif "value_type" in details:
        value = _value_text(details.get("value_type"))
        value_source = "details.value_type"
    else:
        value = ""
        value_source = "unreported"

    return DeliveryFailureTarget(
        field=field,
        value=value,
        field_source=field_source,
        value_source=value_source,
    )


def delivery_failure_target_lines(target: DeliveryFailureTarget) -> tuple[str, str]:
    field_line = (
        f"Field: {target.field}"
        if target.field
        else "Field: not reported by the receiving host."
    )
    if target.value_source == "unreported":
        value_line = "Value: not reported by the receiving host."
    elif target.value:
        value_line = f"Value: {target.value}"
    else:
        value_line = "Value: empty string (reported by the receiving host)."
    return field_line, value_line


__all__ = [
    "DeliveryFailureTarget",
    "delivery_failure_target_lines",
    "resolve_delivery_failure_target",
]
