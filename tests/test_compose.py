"""The three compose files (core, bundled stack, VPN) stay consistent. Needs pyyaml (dev only)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

COMPOSE = Path(__file__).resolve().parent.parent / "compose"
BUNDLED = {"flaresolverr", "prowlarr", "sonarr", "radarr", "qbittorrent", "plex", "bazarr"}


class _Loader(yaml.SafeLoader):
    """Compose's `!override` / `!reset` tags: keep the plain value."""


def _plain(loader, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


for _tag in ("!override", "!reset"):
    _Loader.add_constructor(_tag, _plain)


def load(name: str) -> dict:
    return yaml.load((COMPOSE / name).read_text(encoding="utf-8"), Loader=_Loader)


def host_ports(service: dict) -> list[str]:
    """Host-side `ip:port` of every published port, with variable defaults expanded."""
    out = []
    for p in service.get("ports", []):
        p = re.sub(r"\$\{[A-Z_]+:-([^}]*)\}", r"\1", p)
        out.append(p.rsplit(":", 1)[0])
    return out


def test_core_publishes_only_the_dashboard_port():
    core = load("docker-compose.yml")
    assert set(core["services"]) == {"net", "mediahub", "setup"}
    assert host_ports(core["services"]["net"]) == ["127.0.0.1:8000"]
    assert set(core["volumes"]) == {"mediahub-data", "mediahub-taste"}


def test_stack_has_every_bundled_service():
    stack = load("stack.yml")
    assert BUNDLED <= set(stack["services"])
    for name in BUNDLED:
        assert stack["services"][name]["image"], name
    # The dashboard waits for the apps it talks to.
    assert {"sonarr", "radarr", "prowlarr", "plex", "qbittorrent"} <= set(stack["services"]["mediahub"]["depends_on"])
    ports = host_ports(stack["services"]["net"])
    assert "127.0.0.1:8000" not in ports  # that one belongs to the core file
    assert len(ports) == len(set(ports)) == 7
    assert {v for v in stack["volumes"]} == {f"{n}-config" for n in BUNDLED - {"flaresolverr"}}


def test_vpn_moves_qbittorrent_behind_gluetun():
    vpn = load("vpn.yml")
    svc = vpn["services"]
    assert svc["qbittorrent"]["network_mode"] == "service:gluetun"
    assert svc["qbittorrent"]["depends_on"]["gluetun"]["condition"] == "service_healthy"
    assert "NET_ADMIN" in svc["gluetun"]["cap_add"]
    assert "/dev/net/tun:/dev/net/tun" in svc["gluetun"]["devices"]
    assert re.fullmatch(r"qmcgaw/gluetun:v3\.\d+\.\d+", svc["gluetun"]["image"])  # pinned, not :latest
    assert svc["mediahub"]["environment"]["QBIT_URL"] == "http://gluetun:8090"
    # 8090 is published by gluetun only: net's re-declared list is core + stack minus 8090.
    expected = host_ports(load("docker-compose.yml")["services"]["net"]) + host_ports(load("stack.yml")["services"]["net"])
    expected.remove("127.0.0.1:8090")
    assert sorted(host_ports(svc["net"])) == sorted(expected)
    assert host_ports(svc["gluetun"]) == ["127.0.0.1:8090"]


def test_vpn_override_tags_are_present():
    # Without these, compose would append to net's ports and merge network_mode wrongly.
    text = (COMPOSE / "vpn.yml").read_text(encoding="utf-8")
    assert re.search(r"ports: !override", text)
    assert re.search(r'network_mode: !override "service:gluetun"', text)


def test_anchors_shared_between_core_and_stack_match():
    def anchors(name):
        d = load(name)
        return {k: v for k, v in d.items() if k in ("x-media", "x-tz", "x-common")}
    assert anchors("docker-compose.yml") == anchors("stack.yml")


@pytest.mark.parametrize("name", ["docker-compose.yml", "stack.yml", "vpn.yml", "plex-claim.yml", "stack.env.example", "secrets.env.example", "vpn.env.example"])
def test_no_personal_paths_or_names(name):
    text = (COMPOSE / name).read_text(encoding="utf-8")
    assert not re.search(r"[A-Za-z]:[\\/](Users|Media|claude[O]S)|/home/\w+|\bbart\b", text, re.I), name
    # Container names are prefixable so a second copy can run beside the first.
    if name.endswith(".yml"):
        for m in re.finditer(r"container_name: (\S+)", text):
            assert m.group(1).startswith("${MEDIAHUB_PREFIX:-}"), m.group(1)


def test_setup_service_is_in_core_and_stack_only_adds_the_app_volumes():
    svc = load("docker-compose.yml")["services"]["setup"]   # core mode has it too
    assert svc["profiles"] == ["setup"]  # never started by `up`
    assert svc["image"] == "mediahub-dashboard:local" and svc["pull_policy"] == "never"
    assert "build" not in svc and "ports" not in svc
    assert svc["network_mode"] == "service:net"
    assert svc["entrypoint"] == ["python", "-m", "app.setup"]
    assert svc["user"] == "${PUID:-1000}:${PGID:-1000}"
    assert svc["environment"]["MEDIAHUB_SECRETS"] == "/mh-home/secrets.env"
    assert svc["container_name"].startswith("${MEDIAHUB_PREFIX:-}")
    assert "${MEDIAHUB_HOME}:/mh-home" in svc["volumes"]
    assert not any("/cfg/" in v for v in svc["volumes"])    # nothing of the bundled apps in core
    # Secrets come from the file (re-read each run), not from compose's environment.
    assert all("secrets.env" not in f for f in svc["env_file"])
    # stack.yml only appends the bundled apps' config volumes (compose merges `volumes` lists).
    extra = load("stack.yml")["services"]["setup"]
    assert set(extra) == {"volumes"}
    for app in ("sonarr", "radarr", "prowlarr", "bazarr", "plex"):
        assert f"{app}-config:/cfg/{app}:ro" in extra["volumes"]
    assert "qbittorrent-config:/cfg/qbittorrent" in extra["volumes"]  # setup secrets writes qBittorrent.conf
    # With the VPN, setup reaches qBittorrent through gluetun like the dashboard does.
    assert load("vpn.yml")["services"]["setup"]["environment"]["QBIT_URL"] == "http://gluetun:8090"


def test_mediahub_and_setup_run_as_puid_pgid_and_the_image_dirs_are_writable_for_any_uid():
    core = load("docker-compose.yml")["services"]
    assert core["mediahub"]["user"] == core["setup"]["user"] == "${PUID:-1000}:${PGID:-1000}"
    docker = (COMPOSE / "dashboard" / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"chmod 0777 (.+)", docker)
    assert m and set(m.group(1).split()) == {"/opt/mediahub/data", "/opt/mediahub/logs", "/taste"}
    assert re.search(r"mkdir -p data logs /taste", docker)   # the same three folders exist


def test_plex_claim_is_a_separate_file_so_plex_is_unchanged_without_it():
    plex = load("stack.yml")["services"]["plex"]
    assert "PLEX_CLAIM" not in plex["environment"]
    claim = load("plex-claim.yml")
    assert list(claim["services"]) == ["plex"]
    assert claim["services"]["plex"] == {"environment": {"PLEX_CLAIM": "${PLEX_CLAIM:-}"}}
    assert re.search(r'PLEX_CLAIM\)" \]; then files\+=\(-f "\$dir/plex-claim\.yml"\)',
                     (COMPOSE / "mh.sh").read_text(encoding="utf-8"))
    assert "Setting 'PLEX_CLAIM' '')) { $files += 'plex-claim.yml' }" in (COMPOSE / "mh.ps1").read_text(encoding="utf-8")
