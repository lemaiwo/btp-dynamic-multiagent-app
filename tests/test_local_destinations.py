"""Destinations from a local file (``agents/local_destinations.py``): the
resolver, the user types (SAML bearer assertion, forwarded token) and the
rules that keep a user from being replaced by somebody else."""

from __future__ import annotations

import base64
import json
import xml.etree.ElementTree as ET

import pytest

from agents import auth, local_destinations
from agents.destination import (
    DestinationError,
    DestinationResolver,
    config_from_environment,
)
from agents.destination_auth import DestinationAuth, NotUserPropagating
from tests import local_stack_helpers as helpers

NS = {"saml2": "urn:oasis:names:tc:SAML:2.0:assertion"}


@pytest.fixture(scope="module")
def key():
    return helpers.rsa_key()


@pytest.fixture
def stack(tmp_path, monkeypatch, key):
    """A destinations file with a signing key, and OIDC validation bound."""
    saml_key = helpers.rsa_key()
    (tmp_path / "idp.key").write_bytes(helpers.key_pem(saml_key))
    (tmp_path / "idp.crt").write_bytes(helpers.cert_pem(saml_key))
    document = {
        "saml": {"issuer": "agent-local-saml-idp", "key_file": "idp.key",
                 "cert_file": "idp.crt"},
        "destinations": [
            {"Name": "S4_USER", "URL": "https://sap.example.test:44300/",
             "Authentication": "SAMLAssertion", "audience": "S4H_100",
             "URL.queries.sap-client": "100", "Description": "as the user"},
            {"Name": "S4_TECH", "URL": "https://sap.example.test:44300",
             "Authentication": "BasicAuthentication", "User": "TECH",
             "Password": "${S4_TECH_PASSWORD}"},
            {"Name": "ARC1", "URL": "https://arc1.example.test",
             "Authentication": "ForwardUserToken"},
            {"Name": "PUBLIC", "URL": "https://api.example.test"},
        ],
    }
    path = tmp_path / "destinations.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("LOCAL_DESTINATIONS_FILE", str(path))
    monkeypatch.setenv("S4_TECH_PASSWORD", "s3cret-pw")
    monkeypatch.setattr(auth, "_validator", helpers.validator(key))
    monkeypatch.setattr(auth, "_validator_checked", True)
    local_destinations._loaded.clear()
    return path, document


def resolver(name: str, **kwargs) -> DestinationResolver:
    import os

    config = config_from_environment(os.environ)
    assert local_destinations.is_local(config)
    return DestinationResolver(name, config, **kwargs)


def rewrite(path, document) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")
    local_destinations._loaded.clear()


async def test_technical_user_destination(stack):
    destination = await resolver("S4_TECH").resolve()
    expected = "Basic " + base64.b64encode(b"TECH:s3cret-pw").decode()
    assert destination.headers["Authorization"] == expected
    assert destination.url == "https://sap.example.test:44300"
    assert destination.local and not destination.per_user
    assert destination.auth_type == "BasicAuthentication"


async def test_saml_assertion_for_the_signed_in_user(stack, key):
    token = helpers.token(key, email="wouter@example.test")
    destination = await resolver("S4_USER").resolve(user_token=token, principal="user-1")
    scheme, _, value = destination.headers["Authorization"].partition(" ")
    assert scheme == "SAML2.0"
    root = ET.fromstring(base64.b64decode(value))
    assert root.findtext("saml2:Subject/saml2:NameID", namespaces=NS) == "wouter@example.test"
    assert root.findtext(".//saml2:Audience", namespaces=NS) == "S4H_100"
    assert root.findtext("saml2:Issuer", namespaces=NS) == "agent-local-saml-idp"
    data = root.find(".//saml2:SubjectConfirmationData", NS)
    assert data.get("Recipient") == "https://sap.example.test:44300/"
    assert destination.headers["x-sap-security-session"] == "create"
    assert destination.queries == {"sap-client": "100"}
    assert destination.per_user and destination.local
    assert destination.auth_type == "SAMLAssertion"
    # The minted header is not a stored one: a user run may send it.
    assert "authorization" not in destination.static_headers


async def test_saml_destination_passes_the_user_propagation_rule(stack, key):
    token = helpers.token(key)
    made = resolver("S4_USER")
    auth_flow = DestinationAuth(made, user_context=True, server_key="builtin:odata")
    destination = await made.resolve(user_token=token, principal="user-1")
    assert auth_flow._as_user(destination).headers["Authorization"].startswith("SAML2.0 ")
    # The technical destination does not sign in as the user, local or not.
    technical = await resolver("S4_TECH").resolve(user_token=token, principal="user-1")
    with pytest.raises(NotUserPropagating):
        auth_flow._as_user(technical)


async def test_user_types_refuse_a_call_without_a_user(stack):
    for name in ("S4_USER", "ARC1"):
        with pytest.raises(DestinationError, match="signed-in user"):
            await resolver(name).resolve()
        with pytest.raises(DestinationError):
            await resolver(name, require_credential=False).resolve()


async def test_invalid_or_foreign_token_mints_nothing(stack, key):
    for forged in (
        helpers.token(helpers.rsa_key()),
        helpers.token(key, aud="another-app"),
        "not-a-token",
    ):
        with pytest.raises(DestinationError, match="not valid"):
            await resolver("S4_USER").resolve(user_token=forged, principal="user-1")


async def test_without_token_validation_nothing_is_minted(stack, key, monkeypatch):
    monkeypatch.setattr(auth, "_validator", None)
    with pytest.raises(DestinationError, match="token validation"):
        await resolver("S4_USER").resolve(user_token=helpers.token(key), principal="user-1")


async def test_missing_or_unverified_user_name_is_refused(stack, key):
    with pytest.raises(DestinationError, match="'email' claim"):
        await resolver("S4_USER").resolve(
            user_token=helpers.token(key, email=None), principal="user-1")
    with pytest.raises(DestinationError, match="not verified"):
        await resolver("S4_USER").resolve(
            user_token=helpers.token(key, email_verified=False), principal="user-1")


async def test_user_id_source_and_format_follow_the_destination(stack, key):
    path, document = stack
    document["destinations"][0].update(
        {"userIdSource": "preferred_username", "nameIdFormat": "unspecified",
         "URL.headers.x-sap-security-session": "none"})
    rewrite(path, document)
    token = helpers.token(key, preferred_username="DEVELOPER")
    destination = await resolver("S4_USER").resolve(user_token=token, principal="user-1")
    root = ET.fromstring(base64.b64decode(destination.headers["Authorization"].split(" ")[1]))
    name_id = root.find("saml2:Subject/saml2:NameID", NS)
    assert name_id.text == "DEVELOPER"
    assert name_id.get("Format").endswith("unspecified")
    assert not any(k.lower() == "x-sap-security-session" for k in destination.headers)


async def test_forwarded_user_token(stack, key):
    token = helpers.token(key)
    destination = await resolver("ARC1").resolve(user_token=token, principal="user-1")
    assert destination.headers["Authorization"] == f"Bearer {token}"


async def test_no_authentication_and_properties(stack):
    destination = await resolver("PUBLIC", require_credential=False).resolve()
    assert destination.headers == {}
    properties = await resolver("S4_TECH").resolve_properties()
    assert properties["User"] == "TECH" and properties["Password"] == "s3cret-pw"
    assert "s3cret-pw" not in repr(properties)


async def test_refusals_name_the_rule_not_a_value(stack, monkeypatch):
    path, document = stack
    with pytest.raises(DestinationError, match="does not exist"):
        await resolver("NOPE").resolve()
    monkeypatch.delenv("S4_TECH_PASSWORD")
    with pytest.raises(DestinationError, match="S4_TECH_PASSWORD"):
        await resolver("S4_TECH").resolve()
    document["destinations"][1].update({"ProxyType": "OnPremise", "Password": "pw-in-file"})
    rewrite(path, document)
    with pytest.raises(DestinationError, match="ProxyType") as refused:
        await resolver("S4_TECH").resolve()
    assert "pw-in-file" not in str(refused.value)
    document["destinations"][1].update({"ProxyType": "Internet", "Authentication": "OAuth2Magic"})
    rewrite(path, document)
    with pytest.raises(DestinationError, match="Authentication must be one of"):
        await resolver("S4_TECH").resolve()


async def test_saml_needs_audience_and_key_material(stack, key):
    path, document = stack
    token = helpers.token(key)
    del document["destinations"][0]["audience"]
    rewrite(path, document)
    with pytest.raises(DestinationError, match="audience"):
        await resolver("S4_USER").resolve(user_token=token, principal="user-1")
    document["destinations"][0]["audience"] = "S4H_100"
    document["saml"]["key_file"] = "missing.key"
    rewrite(path, document)
    with pytest.raises(DestinationError, match="cannot be read"):
        await resolver("S4_USER").resolve(user_token=token, principal="user-1")


async def test_destination_picker_lists_the_file(stack):
    from agents.odata import destinations as picker

    answer = await picker.list_destinations()
    by_name = {item["name"]: item for item in answer["items"]}
    assert set(by_name) == {"S4_USER", "S4_TECH", "ARC1", "PUBLIC"}
    assert by_name["S4_USER"]["user_propagating"] is True
    assert by_name["S4_USER"]["description"] == "as the user"
    assert by_name["S4_TECH"]["user_propagating"] is False
    assert by_name["ARC1"]["user_propagating"] is True
    # Only the five listed properties: no URL, user or password.
    assert "s3cret-pw" not in json.dumps(answer) and "sap.example.test" not in json.dumps(answer)


def test_a_bound_destination_service_wins(stack, monkeypatch):
    vcap = {"destination": [{"credentials": {
        "clientid": "c", "clientsecret": "s", "url": "https://uaa.example.test",
        "uri": "https://destination.example.test"}}]}
    config = config_from_environment(
        {"VCAP_SERVICES": json.dumps(vcap), "LOCAL_DESTINATIONS_FILE": str(stack[0])})
    assert not local_destinations.is_local(config)
    assert config_from_environment({}) is None
