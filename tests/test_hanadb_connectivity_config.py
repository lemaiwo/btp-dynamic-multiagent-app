"""The connectivity binding's optional SOCKS5 port (builtin:hana)."""

import json

from agents.destination import (
    ConnectivityConfig,
    connectivity_config_from_environment,
)

SECRET = "s3cr3t-value"


def _vcap(**extra: str) -> dict[str, str]:
    creds = {
        "clientid": "cid",
        "clientsecret": SECRET,
        "token_service_url": "https://uaa.example.invalid",
        "onpremise_proxy_host": "proxy.internal",
        "onpremise_proxy_http_port": "20003",
        **extra,
    }
    return {"VCAP_SERVICES": json.dumps({"connectivity": [{"credentials": creds}]})}


def test_vcap_socks5_port_is_read():
    cfg = connectivity_config_from_environment(
        _vcap(onpremise_socks5_proxy_port="20004")
    )
    assert cfg is not None and cfg.socks5_port == 20004
    assert cfg.proxy_port == 20003


def test_vcap_without_socks5_port_is_none():
    cfg = connectivity_config_from_environment(_vcap())
    assert cfg is not None and cfg.socks5_port is None
    assert cfg.proxy_host == "proxy.internal" and cfg.proxy_port == 20003


def test_vcap_out_of_range_socks5_port_is_none():
    for bad in ("0", "70000", "abc"):
        cfg = connectivity_config_from_environment(
            _vcap(onpremise_socks5_proxy_port=bad)
        )
        assert cfg is not None and cfg.socks5_port is None


def _env(**extra: str) -> dict[str, str]:
    return {
        "CONNECTIVITY_CLIENT_ID": "cid",
        "CONNECTIVITY_CLIENT_SECRET": SECRET,
        "CONNECTIVITY_TOKEN_URL": "https://uaa.example.invalid/oauth/token",
        "CONNECTIVITY_PROXY_HOST": "proxy.internal",
        "CONNECTIVITY_PROXY_PORT": "20003",
        **extra,
    }


def test_env_fallback_socks5_port():
    cfg = connectivity_config_from_environment(_env(CONNECTIVITY_SOCKS5_PORT="20004"))
    assert cfg is not None and cfg.socks5_port == 20004
    assert connectivity_config_from_environment(_env()).socks5_port is None
    bad = connectivity_config_from_environment(_env(CONNECTIVITY_SOCKS5_PORT="99999"))
    assert bad is not None and bad.socks5_port is None


def test_repr_holds_no_secret_and_default_is_optional():
    cfg = ConnectivityConfig("cid", SECRET, "https://t", "h", 1)
    assert cfg.socks5_port is None
    assert SECRET not in repr(cfg)
