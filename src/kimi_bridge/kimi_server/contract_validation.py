"""Compatibility rules for projecting live Kimi specification documents."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import contract as semantic_contract
from .contract import (
    KimiContractCheck,
    RestOperationContract,
    SchemaFieldContract,
    _field,
)


def evaluate_kimi_semantic_contract(
    openapi: Mapping[str, Any],
    asyncapi: Mapping[str, Any],
    *,
    expected_version: str | None = None,
) -> tuple[KimiContractCheck, ...]:
    """Project full upstream specs onto only the bridge's consumed contract."""

    checks: list[KimiContractCheck] = []
    checks.extend(_evaluate_openapi_contract(openapi))
    checks.extend(_evaluate_asyncapi_contract(asyncapi))
    if expected_version is not None:
        checks.append(
            _document_version_check(
                "openapi.version",
                "openapi",
                openapi,
                expected_version,
                "KimiServerClient.get_openapi_document",
            )
        )
        checks.append(
            _document_version_check(
                "asyncapi.version",
                "asyncapi",
                asyncapi,
                expected_version,
                "KimiServerClient.get_asyncapi_document",
            )
        )
    return tuple(sorted(checks, key=lambda item: item.id))


def _document_version_check(
    identifier: str,
    category: str,
    document: Mapping[str, Any],
    expected_version: str,
    source: str,
) -> KimiContractCheck:
    info = document.get("info")
    actual = info.get("version") if isinstance(info, dict) else None
    return _contract_check(
        identifier,
        category,
        actual == expected_version,
        f"document version matches live server version {expected_version}",
        source,
    )


def _evaluate_openapi_contract(
    document: Mapping[str, Any],
) -> list[KimiContractCheck]:
    checks: list[KimiContractCheck] = []
    info = document.get("info")
    metadata_ok = (
        isinstance(document.get("openapi"), str)
        and isinstance(info, dict)
        and info.get("title") == semantic_contract.KIMI_OPENAPI_TITLE
        and isinstance(info.get("version"), str)
    )
    checks.append(
        _contract_check(
            "openapi.metadata",
            "openapi",
            metadata_ok,
            "OpenAPI metadata identifies the Kimi Code server",
            "KimiServerClient.get_openapi_document",
        )
    )
    paths = document.get("paths")
    if not isinstance(paths, dict):
        checks.append(
            _contract_check(
                "openapi.paths",
                "openapi",
                False,
                "OpenAPI paths must be an object",
                "KimiServerClient._request_operation",
            )
        )
        return checks

    for contract in semantic_contract.KIMI_REST_OPERATIONS.values():
        operation = paths.get(contract.spec_path)
        method_spec = (
            operation.get(contract.method.lower())
            if isinstance(operation, dict)
            else None
        )
        operation_id = f"rest.{contract.name}.operation"
        if not isinstance(method_spec, dict):
            checks.append(
                _contract_check(
                    operation_id,
                    "rest",
                    False,
                    f"missing {contract.method} {contract.spec_path}",
                    contract.source,
                )
            )
            continue
        checks.append(
            _contract_check(
                operation_id,
                "rest",
                True,
                f"{contract.method} {contract.spec_path} is available",
                contract.source,
            )
        )
        checks.append(_check_request_contract(contract, method_spec))
        checks.append(_check_query_contract(contract, method_spec))

        response_schemas = _success_response_schemas(method_spec)
        envelope_ok = bool(response_schemas) and all(
            all(
                _field_matches(response_schema, requirement)
                for requirement in (
                    _field("code", "number", "integer"),
                    _field("data", "any"),
                )
            )
            for response_schema in response_schemas
        )
        checks.append(
            _contract_check(
                f"rest.{contract.name}.envelope",
                "rest",
                envelope_ok,
                "success response keeps the code/data envelope",
                contract.source,
            )
        )
        data_schemas = _success_data_schemas(method_spec)
        for requirement in contract.response_fields:
            checks.append(
                _contract_check(
                    "rest."
                    f"{contract.name}.response.{'.'.join(requirement.path)}",
                    "rest",
                    any(
                        _field_matches(data_schema, requirement)
                        for data_schema in data_schemas
                    ),
                    "response field "
                    f"{'.'.join(requirement.path)} keeps a compatible shape",
                    contract.source,
                )
            )
    return checks


def _check_request_contract(
    contract: RestOperationContract, operation: Mapping[str, Any]
) -> KimiContractCheck:
    request_body = operation.get("requestBody")
    body_schema = _json_content_schema(request_body)
    if contract.request_examples:
        ok = body_schema is not None and all(
            _schema_accepts_instance(body_schema, example)
            for example in contract.request_examples
        )
    else:
        ok = not (
            isinstance(request_body, dict)
            and request_body.get("required") is True
        )
    return _contract_check(
        f"rest.{contract.name}.request",
        "rest",
        ok,
        "request shapes sent by the client remain accepted",
        contract.source,
    )


def _check_query_contract(
    contract: RestOperationContract, operation: Mapping[str, Any]
) -> KimiContractCheck:
    parameters = operation.get("parameters")
    parameter_items = parameters if isinstance(parameters, list) else []
    query_parameters = {
        item.get("name"): item
        for item in parameter_items
        if isinstance(item, dict)
        and item.get("in") == "query"
        and isinstance(item.get("name"), str)
    }
    examples = contract.query_examples or ({},)
    ok = True
    for example in examples:
        required_names = {
            name
            for name, item in query_parameters.items()
            if item.get("required") is True
        }
        if not required_names.issubset(example):
            ok = False
            break
        for name, value in example.items():
            item = query_parameters.get(name)
            schema = item.get("schema") if isinstance(item, dict) else None
            if not isinstance(schema, dict) or not _schema_accepts_instance(
                schema, value
            ):
                ok = False
                break
        if not ok:
            break
    return _contract_check(
        f"rest.{contract.name}.query",
        "rest",
        ok,
        "query parameters sent by the client remain accepted",
        contract.source,
    )


def _evaluate_asyncapi_contract(
    document: Mapping[str, Any],
) -> list[KimiContractCheck]:
    checks: list[KimiContractCheck] = []
    info = document.get("info")
    metadata_ok = (
        isinstance(document.get("asyncapi"), str)
        and isinstance(info, dict)
        and info.get("title") == semantic_contract.KIMI_ASYNCAPI_TITLE
        and isinstance(info.get("version"), str)
    )
    checks.append(
        _contract_check(
            "asyncapi.metadata",
            "asyncapi",
            metadata_ok,
            "AsyncAPI metadata identifies the Kimi Code WebSocket server",
            "KimiServerClient.get_asyncapi_document",
        )
    )

    channels = document.get("channels")
    channel = (
        channels.get("kimiCodeWebSocket")
        if isinstance(channels, dict)
        else None
    )
    channel_ok = (
        isinstance(channel, dict)
        and channel.get("address") == semantic_contract.KIMI_WEBSOCKET_PATH
    )
    checks.append(
        _contract_check(
            "asyncapi.websocket.path",
            "asyncapi",
            channel_ok,
            "WebSocket address remains "
            f"{semantic_contract.KIMI_WEBSOCKET_PATH}",
            "KimiServerClient.subscribe_events",
        )
    )

    components = document.get("components")
    messages = (
        components.get("messages")
        if isinstance(components, dict)
        else None
    )
    message_map = messages if isinstance(messages, dict) else {}
    for contract in semantic_contract.KIMI_WEBSOCKET_MESSAGES:
        message = message_map.get(contract.name)
        payload = message.get("payload") if isinstance(message, dict) else None
        checks.append(
            _contract_check(
                f"websocket.message.{contract.name}",
                "websocket",
                isinstance(payload, dict),
                f"message {contract.name} remains available",
                contract.source,
            )
        )
        if not isinstance(payload, dict):
            continue
        for requirement in contract.fields:
            checks.append(
                _contract_check(
                    "websocket.message."
                    f"{contract.name}.{'.'.join(requirement.path)}",
                    "websocket",
                    _field_matches(payload, requirement),
                    "message field "
                    f"{'.'.join(requirement.path)} keeps a compatible shape",
                    contract.source,
                )
            )
        if contract.examples:
            checks.append(
                _contract_check(
                    f"websocket.message.{contract.name}.outbound",
                    "websocket",
                    all(
                        _schema_accepts_instance(payload, example)
                        for example in contract.examples
                    ),
                    "outbound message shapes remain accepted",
                    contract.source,
                )
            )

    session_message = message_map.get("session_event")
    session_schema = (
        session_message.get("payload")
        if isinstance(session_message, dict)
        else None
    )
    event_payload = None
    if isinstance(session_schema, dict):
        properties = session_schema.get("properties")
        if isinstance(properties, dict):
            event_payload = properties.get("payload")
    for event_contract in semantic_contract.KIMI_SESSION_EVENTS:
        variants = _find_discriminator_variants(
            event_payload, event_contract.event_type
        )
        checks.append(
            _contract_check(
                f"websocket.event.{event_contract.event_type}",
                "websocket",
                bool(variants),
                f"event {event_contract.event_type} remains available",
                event_contract.source,
            )
        )
        for requirement in event_contract.fields:
            checks.append(
                _contract_check(
                    "websocket.event."
                    f"{event_contract.event_type}.{'.'.join(requirement.path)}",
                    "websocket",
                    any(
                        _field_matches(variant, requirement)
                        for variant in variants
                    ),
                    "event field "
                    f"{'.'.join(requirement.path)} keeps a compatible shape",
                    event_contract.source,
                )
            )
    return checks


def _contract_check(
    identifier: str,
    category: str,
    passed: bool,
    detail: str,
    source: str,
) -> KimiContractCheck:
    return KimiContractCheck(
        identifier,
        category,
        "pass" if passed else "fail",
        detail,
        source,
    )


def _json_content_schema(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    content = value.get("content")
    if not isinstance(content, dict):
        return None
    application_json = content.get("application/json")
    if not isinstance(application_json, dict):
        return None
    schema = application_json.get("schema")
    return schema if isinstance(schema, dict) else None


def _success_response_schemas(
    operation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    responses = operation.get("responses")
    response = responses.get("200") if isinstance(responses, dict) else None
    schema = _json_content_schema(response)
    if schema is None:
        return []
    matches: list[dict[str, Any]] = []
    for candidate in _schema_alternatives(schema):
        properties = candidate.get("properties")
        code_schema = properties.get("code") if isinstance(properties, dict) else None
        if isinstance(code_schema, dict) and _schema_accepts_instance(
            code_schema, 0
        ):
            matches.append(candidate)
    return matches


def _success_data_schemas(
    operation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    schemas: list[dict[str, Any]] = []
    for response in _success_response_schemas(operation):
        properties = response.get("properties")
        data = properties.get("data") if isinstance(properties, dict) else None
        if isinstance(data, dict):
            schemas.append(data)
    return schemas


def _schema_alternatives(schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    for keyword in ("oneOf", "anyOf"):
        choices = schema.get(keyword)
        if isinstance(choices, list):
            alternatives: list[dict[str, Any]] = []
            for choice in choices:
                if isinstance(choice, dict):
                    alternatives.extend(_schema_alternatives(choice))
            return alternatives
    return [dict(schema)]


def _field_matches(
    schema: Mapping[str, Any], requirement: SchemaFieldContract
) -> bool:
    states = _field_states(schema, requirement.path)
    if not states:
        return False
    if requirement.required and not any(required for _schema, required in states):
        return False
    if "any" in requirement.types and not requirement.values:
        return True
    return any(
        (
            "any" in requirement.types
            or not _schema_types(candidate)
            or bool(
                set(_schema_types(candidate)).intersection(requirement.types)
            )
        )
        and all(
            _schema_accepts_instance(candidate, value)
            for value in requirement.values
        )
        for candidate, _required in states
    )


def _field_states(
    schema: Mapping[str, Any],
    path: tuple[str, ...],
    *,
    ancestors_required: bool = True,
) -> list[tuple[dict[str, Any], bool]]:
    if not path:
        return [(dict(schema), ancestors_required)]

    alternatives = _schema_alternatives(schema)
    if len(alternatives) > 1 or alternatives[0] != dict(schema):
        states: list[tuple[dict[str, Any], bool]] = []
        for alternative in alternatives:
            states.extend(
                _field_states(
                    alternative,
                    path,
                    ancestors_required=ancestors_required,
                )
            )
        return states

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        states = []
        for item in all_of:
            if isinstance(item, dict):
                states.extend(
                    _field_states(
                        item,
                        path,
                        ancestors_required=ancestors_required,
                    )
                )
        return states

    token, *remaining = path
    rest = tuple(remaining)
    if token == "[]":
        items = schema.get("items")
        if not isinstance(items, dict):
            return []
        return _field_states(
            items, rest, ancestors_required=ancestors_required
        )
    if token == "{}":
        values = schema.get("additionalProperties")
        if not isinstance(values, dict):
            return []
        return _field_states(
            values, rest, ancestors_required=ancestors_required
        )

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    child = properties.get(token)
    if not isinstance(child, dict):
        return []
    required_names = schema.get("required")
    is_required = (
        token in required_names if isinstance(required_names, list) else False
    )
    return _field_states(
        child,
        rest,
        ancestors_required=ancestors_required and is_required,
    )


def _schema_types(schema: Mapping[str, Any]) -> tuple[str, ...]:
    value = schema.get("type")
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _schema_accepts_instance(schema: Mapping[str, Any], value: Any) -> bool:
    all_of = schema.get("allOf")
    if isinstance(all_of, list) and not all(
        isinstance(item, dict) and _schema_accepts_instance(item, value)
        for item in all_of
    ):
        return False
    for keyword in ("oneOf", "anyOf"):
        choices = schema.get(keyword)
        if isinstance(choices, list):
            return any(
                isinstance(item, dict) and _schema_accepts_instance(item, value)
                for item in choices
            )
    if value is None:
        return schema.get("nullable") is True or "null" in _schema_types(schema)
    if "const" in schema and value != schema["const"]:
        return False
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return False

    types = _schema_types(schema)
    if types and not any(_instance_has_json_type(value, item) for item in types):
        return False
    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        required = required if isinstance(required, list) else []
        if not all(name in value for name in required):
            return False
        additional = schema.get("additionalProperties", True)
        for name, item_value in value.items():
            item_schema = properties.get(name)
            if isinstance(item_schema, dict):
                if not _schema_accepts_instance(item_schema, item_value):
                    return False
            elif additional is False:
                return False
            elif isinstance(additional, dict) and not _schema_accepts_instance(
                additional, item_value
            ):
                return False
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict) and not all(
            _schema_accepts_instance(items, item) for item in value
        ):
            return False
    return True


def _instance_has_json_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _find_discriminator_variants(
    schema: Any, discriminator: str
) -> list[dict[str, Any]]:
    if not isinstance(schema, dict):
        return []
    matches: list[dict[str, Any]] = []
    properties = schema.get("properties")
    type_schema = properties.get("type") if isinstance(properties, dict) else None
    if isinstance(type_schema, dict):
        values: list[Any] = []
        if "const" in type_schema:
            values.append(type_schema["const"])
        enum = type_schema.get("enum")
        if isinstance(enum, list):
            values.extend(enum)
        if discriminator in values:
            matches.append(dict(schema))
    for value in schema.values():
        if isinstance(value, dict):
            matches.extend(_find_discriminator_variants(value, discriminator))
        elif isinstance(value, list):
            for item in value:
                matches.extend(_find_discriminator_variants(item, discriminator))
    return matches
