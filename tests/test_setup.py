"""app/setup.py: secrets.env editing, key extraction, the qBittorrent password hash, and that
`wire` only adds what is missing (mocked HTTP, no live stack)."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app import config, setup

# ------------------------------------------------------------------ secrets.env editing

OWNER_FILE = (
    b"\xef\xbb\xbf# my secrets\r\n"
    b"SONARR_API_KEY=old\r\n"
    b"\r\n"
    b"# a comment with = sign\r\n"
    b"PLEX_TOKEN = keepme\r\n"
    b"export RADARR_API_KEY=old2\r\n"
    b"WEBHOOK_URL=\"https://example.invalid/x?a=b\"  # trailing\r\n"
    b"SONARR_API_KEY=dupe\r\n"
    b"NOTIFIER=none"  # no final newline
)


def test_env_with_updates_in_place_and_keeps_every_other_byte():
    out = setup.env_with(OWNER_FILE, {"SONARR_API_KEY": "new", "RADARR_API_KEY": "r2", "MCP_TOKEN": "tok"})
    assert out == (
        b"\xef\xbb\xbf# my secrets\r\n"
        b"SONARR_API_KEY=new\r\n"
        b"\r\n"
        b"# a comment with = sign\r\n"
        b"PLEX_TOKEN = keepme\r\n"
        b"export RADARR_API_KEY=r2\r\n"
        b"WEBHOOK_URL=\"https://example.invalid/x?a=b\"  # trailing\r\n"
        b"SONARR_API_KEY=new\r\n"
        b"NOTIFIER=none\r\n"
        b"MCP_TOKEN=tok\r\n"
    )


def test_env_with_no_updates_is_identity_and_lf_files_stay_lf():
    assert setup.env_with(OWNER_FILE, {}) == OWNER_FILE
    assert setup.env_with(b"A=1\n# c\n", {"B": "2"}) == b"A=1\n# c\nB=2\n"
    assert setup.env_with(b"", {"B": "2"}) == b"B=2\n"
    assert setup.env_with(b"A=\n", {"A": "x"}) == b"A=x\n"


def test_env_with_refuses_values_that_need_quoting():
    with pytest.raises(ValueError) as e:
        setup.env_with(b"", {"K": "has space"})
    assert "has space" not in str(e.value)


def test_env_values_reads_like_dotenv():
    v = setup.env_values(OWNER_FILE)
    assert v["SONARR_API_KEY"] == "dupe"  # last wins, as in compose and dotenv
    assert v["PLEX_TOKEN"] == "keepme"
    assert v["RADARR_API_KEY"] == "old2"
    assert v["WEBHOOK_URL"] == "https://example.invalid/x?a=b"


def test_write_atomic(tmp_path):
    f = tmp_path / "secrets.env"
    f.write_bytes(b"A=1\n")
    setup.write_atomic(f, b"A=2\n")
    assert f.read_bytes() == b"A=2\n"
    assert [p.name for p in tmp_path.iterdir()] == ["secrets.env"]


# ------------------------------------------------------------------ key extraction

ARR_XML = """<Config>
  <BindAddress>*</BindAddress>
  <Port>8989</Port>
  <ApiKey>0123456789abcdef0123456789abcdef</ApiKey>
  <AuthenticationMethod>External</AuthenticationMethod>
</Config>"""

BAZARR_YAML = """analytics:
  enabled: false
auth:
  apikey: fedcba9876543210fedcba9876543210
  password: ''
  type: null
backup:
  day: 6
"""

PLEX_CLAIMED = ('<?xml version="1.0" encoding="utf-8"?>\n<Preferences OldestPreviousVersion="1.43.4" '
                'MachineIdentifier="m" ProcessedMachineIdentifier="p" PlexOnlineToken="tokABC-123_x" '
                'PlexOnlineUsername="someone"/>')
PLEX_UNCLAIMED = '<?xml version="1.0" encoding="utf-8"?>\n<Preferences MachineIdentifier="m"/>'


def test_key_extraction():
    assert setup.arr_api_key(ARR_XML) == "0123456789abcdef0123456789abcdef"
    assert setup.arr_api_key("<Config><Port>1</Port></Config>") is None
    assert setup.arr_api_key("not xml") is None
    assert setup.bazarr_api_key(BAZARR_YAML) == "fedcba9876543210fedcba9876543210"
    assert setup.bazarr_api_key("auth:\n  apikey: null\n") is None
    assert setup.plex_token(PLEX_CLAIMED) == "tokABC-123_x"
    assert setup.plex_token(PLEX_UNCLAIMED) is None


# ------------------------------------------------------------------ qBittorrent password

def test_pbkdf2_matches_reference_vector():
    # Reference: .NET Rfc2898DeriveBytes.Pbkdf2(UTF8("correct horse battery staple"),
    # salt = bytes 0..15, 100000, SHA512, 64) -> base64(salt) + ":" + base64(key), computed
    # independently in PowerShell 7 on 2026-11-01. Same layout as qBittorrent's
    # Utils::Password::PBKDF2::generate (src/base/utils/password.cpp).
    want = ("AAECAwQFBgcICQoLDA0ODw==:hzaYXq3InP7jFNdKFTiXBaKMc6Hki6FR8fwp8lRCNSzgwBQu/67CPfP4HL9Zak"
            "yYey/St+3YqPl5YVpad7RbFQ==")
    assert setup.qbit_pbkdf2("correct horse battery staple", bytes(range(16))) == want


def test_pbkdf2_primitive_matches_published_vector():
    # The widely published PBKDF2-HMAC-SHA512 vector (P="password", S="salt", c=1, dkLen=64).
    assert hashlib.pbkdf2_hmac("sha512", b"password", b"salt", 1, 64).hex() == (
        "867f70cf1ade02cff3752599a3a53dc4af34c7a669815ae5d513554e1c8cf252"
        "c02d470a285a0501bad999bfe943c08f050235d7d68b1da55e63f73b60a57fce")


def test_pbkdf2_random_salt_shape():
    salt_b64, key_b64 = setup.qbit_pbkdf2("pw").split(":")
    salt, key = base64.b64decode(salt_b64), base64.b64decode(key_b64)
    assert len(salt) == 16 and len(key) == 64
    assert hashlib.pbkdf2_hmac("sha512", b"pw", salt, 100_000, 64) == key


LSIO_CONF = ("[BitTorrent]\r\nSession\\DefaultSavePath=/downloads/\r\n\r\n"
             "[Preferences]\r\nConnection\\PortRangeMin=6881\r\nWebUI\\Address=*\r\n"
             "WebUI\\Username=old\r\nWebUI\\Port=8090\r\n\r\n[Other]\r\nx=1\r\n")


def test_qbit_conf_sets_login_and_keeps_the_rest():
    out = setup.qbit_conf_with_login(LSIO_CONF, "admin", "S:H")
    assert out == ("[BitTorrent]\r\nSession\\DefaultSavePath=/downloads/\r\n\r\n"
                   "[Preferences]\r\nConnection\\PortRangeMin=6881\r\nWebUI\\Address=*\r\n"
                   "WebUI\\Username=admin\r\nWebUI\\Port=8090\r\n"
                   "WebUI\\Password_PBKDF2=\"@ByteArray(S:H)\"\r\n\r\n[Other]\r\nx=1\r\n")
    # Running it again replaces, never duplicates.
    again = setup.qbit_conf_with_login(out, "admin", "S2:H2")
    assert again.count("Password_PBKDF2") == 1 and "@ByteArray(S2:H2)" in again


def test_qbit_conf_without_preferences_section():
    out = setup.qbit_conf_with_login("[BitTorrent]\nA=1", "admin", "S:H")
    assert out == "[BitTorrent]\nA=1\n\n[Preferences]\nWebUI\\Username=admin\nWebUI\\Password_PBKDF2=\"@ByteArray(S:H)\"\n"


# ------------------------------------------------------------------ secrets phase

def _cfg(tmp_path: Path, *, plex=PLEX_UNCLAIMED, qbit=True) -> Path:
    cfg = tmp_path / "cfg"
    for app in ("sonarr", "radarr", "prowlarr"):
        (cfg / app).mkdir(parents=True)
        (cfg / app / "config.xml").write_text(ARR_XML.replace("0123", app[:4]), encoding="utf-8")
    (cfg / "bazarr/config").mkdir(parents=True)
    (cfg / "bazarr/config/config.yaml").write_text(BAZARR_YAML, encoding="utf-8")
    prefs = cfg / setup.PLEX_PREFS
    prefs.parent.mkdir(parents=True)
    prefs.write_text(plex, encoding="utf-8")
    if qbit:
        conf = cfg / setup.QBIT_CONF
        conf.parent.mkdir(parents=True)
        conf.write_text(LSIO_CONF, encoding="utf-8")
    return cfg


def _secrets_run(secrets_file, cfg, monkeypatch, qbit_up=False):
    monkeypatch.setattr(setup, "_port_open", lambda url: qbit_up)
    lines = []
    rc = setup.secrets_phase(secrets_file, cfg, wait=0, out=lines.append)
    return rc, lines


def test_secrets_fresh_install(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, plex=PLEX_CLAIMED)
    f = tmp_path / "secrets.env"
    f.write_bytes(b"# mine\r\nNOTIFIER=none\r\n")
    rc, lines = _secrets_run(f, cfg, monkeypatch)
    assert rc == 0
    v = setup.env_values(f.read_bytes())
    assert v["SONARR_API_KEY"].startswith("sona") and v["PROWLARR_API_KEY"].startswith("prow")
    assert v["BAZARR_API_KEY"] == "fedcba9876543210fedcba9876543210"
    assert v["PLEX_TOKEN"] == "tokABC-123_x"
    assert len(v["MCP_TOKEN"]) >= 40 and v["QBIT_USER"] == "admin" and len(v["QBIT_PASS"]) >= 24
    assert f.read_bytes().startswith(b"# mine\r\nNOTIFIER=none\r\n")
    # qBittorrent.conf holds a hash of exactly that password.
    conf = (cfg / setup.QBIT_CONF).read_text(encoding="utf-8")
    hashed = conf.split('WebUI\\Password_PBKDF2="@ByteArray(')[1].split(")")[0]
    salt, key = (base64.b64decode(x) for x in hashed.split(":"))
    assert hashlib.pbkdf2_hmac("sha512", v["QBIT_PASS"].encode(), salt, 100_000, 64) == key
    assert "WebUI\\Username=admin" in conf
    # Names only, never a value.
    out = "\n".join(lines)
    for name, value in v.items():
        if name != "QBIT_USER" and len(value) > 4:  # the user name "admin" is not a secret
            assert value not in out, name
    assert lines[-1] == ("updated: BAZARR_API_KEY, MCP_TOKEN, PLEX_TOKEN, PROWLARR_API_KEY, QBIT_PASS, "
                         "QBIT_USER, RADARR_API_KEY, SONARR_API_KEY")
    # Second run: nothing to do, file untouched.
    before = f.read_bytes()
    rc, lines = _secrets_run(f, cfg, monkeypatch)
    assert (rc, lines[-1], f.read_bytes()) == (0, "updated: nothing", before)


def test_secrets_app_key_is_the_truth_but_own_values_are_kept(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    f = tmp_path / "secrets.env"
    f.write_bytes(b"SONARR_API_KEY=stale\nPLEX_TOKEN=mine\nMCP_TOKEN=mine\nQBIT_USER=me\nQBIT_PASS=mine\n")
    conf_before = (cfg / setup.QBIT_CONF).read_bytes()
    rc, lines = _secrets_run(f, cfg, monkeypatch, qbit_up=True)  # qbit up is fine: QBIT_PASS is set
    assert rc == 0
    v = setup.env_values(f.read_bytes())
    assert v["SONARR_API_KEY"].startswith("sona")
    assert (v["PLEX_TOKEN"], v["MCP_TOKEN"], v["QBIT_USER"], v["QBIT_PASS"]) == ("mine",) * 2 + ("me", "mine")
    assert (cfg / setup.QBIT_CONF).read_bytes() == conf_before
    assert lines[-1] == "updated: BAZARR_API_KEY, PROWLARR_API_KEY, RADARR_API_KEY, SONARR_API_KEY"


def test_secrets_refuses_qbit_password_while_qbit_runs(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    f = tmp_path / "secrets.env"
    f.write_bytes(b"")
    rc, lines = _secrets_run(f, cfg, monkeypatch, qbit_up=True)
    assert rc == 1
    assert any(l.startswith("STOP qBittorrent is running") for l in lines)
    v = setup.env_values(f.read_bytes())
    assert "QBIT_PASS" not in v and v["SONARR_API_KEY"]  # the rest is still written
    assert "Password_PBKDF2" not in (cfg / setup.QBIT_CONF).read_text(encoding="utf-8")
    assert any(l.startswith("NOTE PLEX_TOKEN") for l in lines)


HASHED_CONF = LSIO_CONF.replace(
    "WebUI\\Username=old\r\n",
    "WebUI\\Username=owner\r\nWebUI\\Password_PBKDF2=\"@ByteArray(AAAA:BBBB)\"\r\n")
OWN_PASSWORD_NOTE = ("NOTE qBittorrent already has a password; put it in QBIT_USER/QBIT_PASS, "
                     "or keep localhost auth bypass")


@pytest.mark.parametrize("qbit_up", [False, True])
def test_secrets_existing_qbit_hash_is_left_alone_even_while_qbit_runs(tmp_path, monkeypatch, qbit_up):
    cfg = _cfg(tmp_path)
    (cfg / setup.QBIT_CONF).write_text(HASHED_CONF, encoding="utf-8")
    assert "Password_PBKDF2" in HASHED_CONF
    before = (cfg / setup.QBIT_CONF).read_bytes()
    f = tmp_path / "secrets.env"
    f.write_bytes(b"")
    rc, lines = _secrets_run(f, cfg, monkeypatch, qbit_up=qbit_up)
    assert rc == 0  # no stop-refusal: nothing will be written
    assert OWN_PASSWORD_NOTE in lines and not any(l.startswith("STOP") for l in lines)
    assert (cfg / setup.QBIT_CONF).read_bytes() == before
    v = setup.env_values(f.read_bytes())
    assert "QBIT_PASS" not in v and "QBIT_USER" not in v and v["SONARR_API_KEY"]


def test_secrets_fresh_qbit_conf_gets_a_hash_and_no_note(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    f = tmp_path / "secrets.env"
    f.write_bytes(b"")
    rc, lines = _secrets_run(f, cfg, monkeypatch)
    assert rc == 0 and OWN_PASSWORD_NOTE not in lines
    assert "Password_PBKDF2" in (cfg / setup.QBIT_CONF).read_text(encoding="utf-8")
    assert setup.env_values(f.read_bytes())["QBIT_USER"] == "admin"


def test_secrets_reports_apps_that_have_not_started(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    (cfg / "radarr/config.xml").unlink()
    f = tmp_path / "secrets.env"
    rc, lines = _secrets_run(f, cfg, monkeypatch)
    assert rc == 1
    assert any(l.startswith("MISSING RADARR_API_KEY") for l in lines)
    assert "RADARR_API_KEY" not in setup.env_values(f.read_bytes())


@pytest.mark.parametrize("make_cfg", [False, True])
def test_secrets_in_core_mode_makes_only_the_mcp_token(tmp_path, monkeypatch, make_cfg):
    """MEDIAHUB_STACK=core mounts no /cfg/<app> volumes: no harvesting, no waiting, one NOTE."""
    cfg = tmp_path / "cfg"
    if make_cfg:
        cfg.mkdir()                      # an empty /cfg is the same as none
    f = tmp_path / "secrets.env"
    f.write_bytes(b"SONARR_URL=http://nas:8989\nSONARR_API_KEY=mine\n")
    monkeypatch.setattr(setup, "_port_open", lambda url: pytest.fail("must not probe qBittorrent"))
    monkeypatch.setattr(setup.time, "sleep", lambda s: pytest.fail("must not wait for apps"))
    lines = []
    rc = setup.secrets_phase(f, cfg, wait=180, out=lines.append)
    assert rc == 0
    v = setup.env_values(f.read_bytes())
    assert len(v["MCP_TOKEN"]) >= 40 and v["SONARR_API_KEY"] == "mine"
    assert set(v) == {"SONARR_URL", "SONARR_API_KEY", "MCP_TOKEN"}
    assert lines[0].startswith("NOTE bundled apps:") and "core" in lines[0]
    assert not any(l.startswith(("MISSING", "STOP")) for l in lines)
    assert lines[-1] == "updated: MCP_TOKEN"
    # Second run: nothing to do.
    before = f.read_bytes()
    rc = setup.secrets_phase(f, cfg, wait=180, out=lines.append)
    assert (rc, lines[-1], f.read_bytes()) == (0, "updated: nothing", before)


def test_helper_commands_need_no_app_volumes(capsys, monkeypatch):
    """allowed-tools / version / bot-prompt work in core mode, where /cfg does not exist."""
    monkeypatch.setattr(setup, "CFG", Path("/nonexistent/cfg"))
    assert setup.main(["allowed-tools"]) == 0
    assert "mcp__mediahub__status" in capsys.readouterr().out
    assert setup.main(["version"]) == 0
    assert capsys.readouterr().out.strip()


# ------------------------------------------------------------------ wire (mocked stack)

PORTS = {8989: "sonarr", 7878: "radarr", 9696: "prowlarr", 8090: "qbit", 6767: "bazarr", 32400: "plex"}


class FakeStack:
    """Just enough of each app's API for setup.wire_steps. Records every write."""

    def __init__(self, fresh: bool, qbit_52: bool = False):
        self.qbit_52 = qbit_52
        self.qbit_locked = True  # unauthenticated requests are refused (no localhost bypass)
        self.version_probes = 0
        self.writes: list[tuple[str, str, str]] = []
        self.prefs_posts: list[dict] = []  # qBittorrent setPreferences payloads other than the save path
        bazarr_ok = {"sonarr": {"ip": "127.0.0.1"}, "radarr": {"ip": "127.0.0.1"},
                     "general": {"enabled_providers": setup.PROVIDERS}}
        if fresh:
            self.s = {
                "sonarr": {"rootfolder": [], "downloadclient": [], "notification": [], "mm": {"recycleBin": "", "recycleBinCleanupDays": 0}},
                "radarr": {"rootfolder": [], "downloadclient": [], "notification": [], "mm": {"recycleBin": "", "recycleBinCleanupDays": 0}},
                "prowlarr": {"applications": [], "indexerproxy": [], "tag": [], "indexer": []},
                "qbit": {"save_path": "/downloads/"},
                "bazarr": {"sonarr": {"ip": ""}, "radarr": {"ip": ""}, "general": {"enabled_providers": []}},
                "plex": [],
            }
        else:  # an owner's existing stack, set up by hand with other names and paths
            self.s = {
                "sonarr": {"rootfolder": [{"id": 1, "path": "/media/Shows"}],
                           "downloadclient": [{"id": 1, "name": "qBit", "implementation": "QBittorrent"}],
                           "notification": [], "mm": {"recycleBin": "/media/Bin", "recycleBinCleanupDays": 7}},
                "radarr": {"rootfolder": [{"id": 1, "path": "/media/Films"}],
                           "downloadclient": [{"id": 2, "name": "torrents", "implementation": "QBittorrent"}],
                           "notification": [], "mm": {"recycleBin": "/media/Bin", "recycleBinCleanupDays": 7}},
                "prowlarr": {"applications": [{"id": 1, "name": "TV", "implementation": "Sonarr"},
                                              {"id": 2, "name": "Films", "implementation": "Radarr"}],
                             "indexerproxy": [{"id": 1, "name": "FS", "implementation": "FlareSolverr"}],
                             "tag": [{"id": 3, "label": "flare"}],
                             "indexer": [{"id": 1, "name": "Shielded", "tags": [3], "enable": True},
                                         {"id": 2, "name": "Plain", "tags": [], "enable": True}]},
                # already compliant; its seeding limits differ from the config defaults on purpose
                "qbit": {"save_path": "/media/Downloads/", "torrent_content_layout": "Subfolder",
                         "dl_limit": 2000 * 1024, "up_limit": 250 * 1024,
                         "max_ratio_enabled": True, "max_ratio": 1.5,
                         "max_seeding_time_enabled": True, "max_seeding_time": 4000},
                "bazarr": bazarr_ok,
                "plex": [{"type": "movie", "title": "Films"}, {"type": "show", "title": "Telly"}],
            }

    def handler(self, req: httpx.Request) -> httpx.Response:
        app, path, m = PORTS[req.url.port], req.url.path, req.method
        if m != "GET" and not path.endswith("/auth/login"):
            self.writes.append((m, app, path))
        body = json.loads(req.content) if req.content and req.headers.get("content-type") == "application/json" else None
        s = self.s.get(app)
        if app in ("sonarr", "radarr"):
            res = path.rsplit("/v3/", 1)[1]
            if res == "config/mediamanagement":
                if m == "PUT":
                    s["mm"] = body
                return httpx.Response(200, json=s["mm"])
            if res == "downloadclient/schema":
                cat = "tvCategory" if app == "sonarr" else "movieCategory"
                return httpx.Response(200, json=[{"implementation": "Transmission", "fields": []},
                                                 {"implementation": "QBittorrent", "fields": [
                                                     {"name": n} for n in ("host", "port", "useSsl", "urlBase", "username",
                                                                            "password", cat, "contentLayout")]}])
            if m == "POST":
                s[res].append({"id": 9, **body})
                return httpx.Response(201, json=body)
            return httpx.Response(200, json=s[res])
        if app == "prowlarr":
            res = path.rsplit("/v1/", 1)[1]
            if res.endswith("/schema"):
                impl = {"applications/schema": ["Sonarr", "Radarr"], "indexerproxy/schema": ["FlareSolverr"]}[res]
                return httpx.Response(200, json=[{"implementation": i, "fields": [
                    {"name": "prowlarrUrl"}, {"name": "baseUrl"}, {"name": "apiKey"}, {"name": "host"},
                    {"name": "requestTimeout"}]} for i in impl])
            if m == "POST":
                item = {"id": 7, **body}
                s[res].append(item)
                return httpx.Response(201, json=item)
            return httpx.Response(200, json=s[res])
        if app == "qbit":
            if path.endswith("/auth/login"):
                ok = parse_qs(req.content.decode()) == {"username": ["admin"], "password": ["pw"]}
                if self.qbit_52:  # qBittorrent 5.2+: 204 on success, 401 on failure
                    return httpx.Response(204) if ok else httpx.Response(401, text="Unauthorized")
                return httpx.Response(200, text="Ok." if ok else "Fails.")
            if path.endswith("/app/version"):  # the unauthenticated probe (localhost auth bypass)
                self.version_probes += 1
                return httpx.Response(403, text="Forbidden") if self.qbit_locked else httpx.Response(200, text="v5.2.3")
            if path.endswith("/setPreferences"):
                sent = json.loads(parse_qs(req.content.decode())["json"][0])
                if "save_path" not in sent:
                    self.prefs_posts.append(sent)
                s.update(sent)
            return httpx.Response(200, json=s)
        if app == "bazarr":
            if m == "POST":
                s.update({"sonarr": {"ip": "127.0.0.1"}, "radarr": {"ip": "127.0.0.1"},
                          "general": {"enabled_providers": setup.PROVIDERS}})
                return httpx.Response(204)
            return httpx.Response(200, json=s)
        if app == "plex":
            if m == "POST":
                self.s["plex"].append({"type": req.url.params["type"], "title": req.url.params["name"]})
            return httpx.Response(200, json={"MediaContainer": {"Directory": self.s["plex"]}})
        raise AssertionError(req.url)


@pytest.fixture
def stack_config(monkeypatch, tmp_path):
    for name, value in {"SONARR_API_KEY": "s", "RADARR_API_KEY": "r", "PROWLARR_API_KEY": "p",
                        "BAZARR_API_KEY": "b", "PLEX_TOKEN": "t", "QBIT_USER": "admin", "QBIT_PASS": "pw",
                        "SONARR_URL": "http://localhost:8989", "RADARR_URL": "http://localhost:7878",
                        "PROWLARR_URL": "http://localhost:9696", "QBIT_URL": "http://localhost:8090",
                        "BAZARR_URL": "http://localhost:6767", "PLEX_URL": "http://localhost:32400",
                        "PROWLARR_DROP_INDEXERS": [], "PROWLARR_FLARE_INDEXERS": [],
                        "QBIT_DL_LIMIT_KIB": 2000, "QBIT_UP_LIMIT_KIB": 250,
                        "QBIT_MAX_RATIO": 2.0, "QBIT_MAX_SEEDING_MINUTES": 10080}.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.delenv("MEDIAHUB_VPN", raising=False)
    monkeypatch.setattr(setup, "_media_mounted", lambda: False)
    made = []
    monkeypatch.setattr(setup, "_mkdir", made.append)
    return made


def _wire(stack: FakeStack, check: bool, capsys) -> tuple[int, list[str]]:
    with httpx.Client(transport=httpx.MockTransport(stack.handler)) as c:
        rc = setup.wire(check, client=c)
    return rc, capsys.readouterr().out.splitlines()


def test_wire_on_an_existing_stack_writes_nothing(stack_config, capsys, monkeypatch):
    monkeypatch.setattr(config, "PROWLARR_FLARE_INDEXERS", ["Shielded"])
    stack = FakeStack(fresh=False)
    for check in (True, False):
        rc, lines = _wire(stack, check, capsys)
        assert rc == 0
        assert all(l.startswith("OK   ") for l in lines), lines
        assert len(lines) == 16
    assert stack.writes == [] and stack_config == []


def test_wire_on_a_fresh_stack_creates_then_is_idempotent(stack_config, capsys):
    stack = FakeStack(fresh=True)
    rc, lines = _wire(stack, True, capsys)  # check first: drift, no writes
    assert rc == 1 and stack.writes == []
    assert sum(l.startswith("DRIFT ") for l in lines) == 22  # 14 steps with one line + 5 qbit policy lines + 2 for the recycle bin folders

    rc, lines = _wire(stack, False, capsys)
    assert rc == 0, lines
    assert not any(l.startswith("FAIL") for l in lines), lines
    s = stack.s
    assert [r["path"] for r in s["sonarr"]["rootfolder"]] == ["/media/TV"]
    assert [r["path"] for r in s["radarr"]["rootfolder"]] == ["/media/Movies"]
    dc = {f["name"]: f.get("value") for f in s["sonarr"]["downloadclient"][0]["fields"]}
    assert dc == {"host": "localhost", "port": 8090, "useSsl": False, "urlBase": "", "username": "admin",
                  "password": "pw", "tvCategory": "tv-sonarr", "contentLayout": 2}
    rdc = {f["name"]: f.get("value") for f in s["radarr"]["downloadclient"][0]["fields"]}
    assert rdc["movieCategory"] == "radarr"
    apps = {a["implementation"]: {f["name"]: f.get("value") for f in a["fields"]} for a in s["prowlarr"]["applications"]}
    assert apps["Sonarr"]["baseUrl"] == "http://localhost:8989" and apps["Sonarr"]["apiKey"] == "s"
    assert apps["Radarr"]["baseUrl"] == "http://localhost:7878" and apps["Radarr"]["prowlarrUrl"] == "http://localhost:9696"
    assert all(a["syncLevel"] == "fullSync" for a in s["prowlarr"]["applications"])
    proxy = s["prowlarr"]["indexerproxy"][0]
    assert proxy["tags"] == [7] and {f["name"]: f.get("value") for f in proxy["fields"]}["host"] == "http://localhost:8191/"
    assert s["prowlarr"]["indexer"] == []  # never adds an indexer
    assert s["qbit"]["save_path"] == "/media/Downloads"
    assert sorted(d["type"] for d in s["plex"]) == ["movie", "show"]
    assert sorted(stack_config) == ["/media/Downloads", "/media/Movies", "/media/Movies", "/media/TV", "/media/TV"]
    for app in ("sonarr", "radarr"):  # the recycle bin folder is set, so deletes are no longer permanent
        assert s[app]["mm"] == {"recycleBin": "/media/.recyclebin", "recycleBinCleanupDays": 7}

    stack.writes.clear()
    rc, lines = _wire(stack, False, capsys)
    assert rc == 0 and stack.writes == [] and all(l.startswith("OK   ") for l in lines), lines
    rc, _ = _wire(stack, True, capsys)
    assert rc == 0


def _recycle(stack, app="sonarr"):
    return stack.s[app]["mm"]


def _run_recycle(stack, check, app="sonarr"):
    with httpx.Client(transport=httpx.MockTransport(stack.handler)) as c:
        return setup.arr_recycle(c, app, check)


def test_recycle_bin_empty_is_drift_in_check_and_untouched(stack_config):
    stack = FakeStack(fresh=True)
    drift = _run_recycle(stack, True)
    assert len(drift) == 2 and "no recycle bin folder" in drift[0] and "/media/.recyclebin" in drift[0]
    assert stack.writes == [] and _recycle(stack)["recycleBin"] == ""


def test_recycle_bin_empty_is_set_with_days_and_the_folder_is_made(stack_config, monkeypatch):
    monkeypatch.setattr(setup, "_media_mounted", lambda: True)
    stack = FakeStack(fresh=True)
    _recycle(stack)["otherKey"] = "kept"
    assert len(_run_recycle(stack, False)) == 2
    assert _recycle(stack) == {"recycleBin": "/media/.recyclebin", "recycleBinCleanupDays": 7, "otherKey": "kept"}
    assert stack_config == ["/media/.recyclebin"]
    assert _run_recycle(stack, True) == []       # now compliant


def test_recycle_bin_not_made_outside_the_container(stack_config, monkeypatch):
    monkeypatch.setattr(setup, "_media_mounted", lambda: False)   # a run outside the container
    stack = FakeStack(fresh=True)
    _run_recycle(stack, False)
    assert stack_config == [] and _recycle(stack)["recycleBin"] == "/media/.recyclebin"


def test_recycle_bin_owner_path_is_left_alone(stack_config, monkeypatch):
    monkeypatch.setattr(setup, "_media_mounted", lambda: True)
    stack = FakeStack(fresh=False)
    _recycle(stack)["recycleBin"] = "/media/MyBin"
    _recycle(stack)["recycleBinCleanupDays"] = 30
    drift = _run_recycle(stack, False)
    assert drift == ["sonarr recycle bin keeps 30 day(s), want 7"]
    assert _recycle(stack)["recycleBin"] == "/media/MyBin" and _recycle(stack)["recycleBinCleanupDays"] == 7
    assert stack_config == []                      # no folder made for a path we did not set


def test_qbit_policy_already_compliant_is_ok_and_writes_nothing(stack_config, capsys):
    stack = FakeStack(fresh=False)
    for check in (True, False):
        with httpx.Client(transport=httpx.MockTransport(stack.handler)) as c:
            assert setup.qbit_policy(c, check) == []
    assert stack.writes == []


def test_qbit_policy_on_fresh_defaults_sets_every_field(stack_config, capsys):
    stack = FakeStack(fresh=True)
    with httpx.Client(transport=httpx.MockTransport(stack.handler)) as c:
        drift = setup.qbit_policy(c, True)  # check: reports, writes nothing
        assert len(drift) == 5 and stack.prefs_posts == []
        assert setup.qbit_policy(c, False) == drift
        assert stack.prefs_posts == [{
            "torrent_content_layout": "Subfolder", "dl_limit": 2048000, "up_limit": 256000,
            "max_ratio_enabled": True, "max_ratio": 2.0,
            "max_seeding_time_enabled": True, "max_seeding_time": 10080}]
        assert setup.qbit_policy(c, False) == []  # idempotent
    assert len(stack.prefs_posts) == 1


def test_qbit_policy_unlimited_and_custom_values(stack_config, monkeypatch):
    monkeypatch.setattr(config, "QBIT_DL_LIMIT_KIB", 0)
    monkeypatch.setattr(config, "QBIT_UP_LIMIT_KIB", 500)
    stack = FakeStack(fresh=True)
    stack.s["qbit"].update(dl_limit=2048000, up_limit=0)
    with httpx.Client(transport=httpx.MockTransport(stack.handler)) as c:
        setup.qbit_policy(c, False)
    sent = stack.prefs_posts[0]
    assert sent["dl_limit"] == 0 and sent["up_limit"] == 500 * 1024


def test_qbit_policy_leaves_enabled_seeding_limits_alone(stack_config):
    stack = FakeStack(fresh=False)
    q = stack.s["qbit"]
    q.update(torrent_content_layout="Original", dl_limit=0,  # only these two are off-policy
             max_ratio_enabled=True, max_ratio=5.0, max_seeding_time_enabled=False, max_seeding_time=-1)
    with httpx.Client(transport=httpx.MockTransport(stack.handler)) as c:
        assert len(setup.qbit_policy(c, False)) == 3
    assert stack.prefs_posts == [{
        "torrent_content_layout": "Subfolder", "dl_limit": 2048000,
        "max_seeding_time_enabled": True, "max_seeding_time": 10080}]
    assert q["max_ratio"] == 5.0 and q["max_ratio_enabled"] is True  # untouched


def test_wire_vpn_points_download_clients_at_gluetun(stack_config, capsys, monkeypatch):
    monkeypatch.setenv("MEDIAHUB_VPN", "gluetun")
    stack = FakeStack(fresh=True)
    _wire(stack, False, capsys)
    dc = {f["name"]: f.get("value") for f in stack.s["radarr"]["downloadclient"][0]["fields"]}
    assert dc["host"] == "gluetun"


def test_wire_without_plex_token_is_a_note_not_a_failure(stack_config, capsys, monkeypatch):
    monkeypatch.setattr(config, "PLEX_TOKEN", "")
    stack = FakeStack(fresh=False)
    rc, lines = _wire(stack, True, capsys)
    assert rc == 0
    assert [l for l in lines if not l.startswith("OK")] == [
        "NOTE plex libraries: Plex not claimed: open Plex's web page on port 32400 of this server (from a "
        "browser on the same network), or set PLEX_CLAIM in stack.env, then run `setup secrets` and "
        "`setup wire` again"]


@pytest.mark.parametrize("qbit_52, status", [(False, 200), (True, 401)])
def test_wire_reports_a_wrong_qbit_password_once(stack_config, capsys, monkeypatch, qbit_52, status):
    monkeypatch.setattr(config, "QBIT_PASS", "wrong")
    stack = FakeStack(fresh=False, qbit_52=qbit_52)
    rc, lines = _wire(stack, True, capsys)
    assert rc == 1
    assert [l for l in lines if l.startswith("FAIL")] == [
        f"FAIL qbittorrent save path: qBittorrent login refused (HTTP {status}): QBIT_USER/QBIT_PASS do not match"]
    assert "wrong" not in "\n".join(lines)


def test_wire_without_qbit_pass_uses_localhost_auth_bypass(stack_config, capsys, monkeypatch):
    monkeypatch.setattr(config, "QBIT_PASS", "")
    stack = FakeStack(fresh=False)
    stack.qbit_locked = False  # LocalHostAuth=false: the API answers without a login
    rc, lines = _wire(stack, True, capsys)
    assert rc == 0 and all(l.startswith("OK   ") for l in lines), lines
    assert "OK   qbittorrent save path" in lines and "OK   qbit policy" in lines
    assert stack.version_probes == 1  # one probe shared by both qBittorrent steps
    # The steps really run: a fresh qBittorrent behind the bypass gets its policy applied.
    fresh = FakeStack(fresh=True)
    fresh.qbit_locked = False
    rc, lines = _wire(fresh, False, capsys)
    assert not any(l.startswith("FAIL") for l in lines), lines
    assert fresh.s["qbit"]["save_path"] == "/media/Downloads" and fresh.s["qbit"]["dl_limit"] == 2048000
    assert not any(w[2].endswith("/auth/login") for w in fresh.writes)


def test_wire_without_qbit_pass_and_no_bypass_is_one_fail(stack_config, capsys, monkeypatch):
    monkeypatch.setattr(config, "QBIT_PASS", "")
    stack = FakeStack(fresh=False)  # locked: the probe gets 403
    rc, lines = _wire(stack, True, capsys)
    assert rc == 1
    assert [l for l in lines if l.startswith("FAIL")] == [
        "FAIL qbittorrent save path: qBittorrent needs a login: QBIT_PASS is empty — run `setup secrets` "
        "(fresh install) or set QBIT_USER/QBIT_PASS"]
    assert "NOTE qbit policy: skipped: the qBittorrent login failed above" in lines
    assert stack.version_probes == 1 and stack.writes == []


def test_wire_accepts_qbittorrent_52_login(stack_config, capsys):
    stack = FakeStack(fresh=True, qbit_52=True)
    rc, lines = _wire(stack, False, capsys)
    assert rc == 0, lines
    assert stack.s["qbit"]["save_path"] == "/media/Downloads"


def test_main_parses_phases(monkeypatch):
    called = []
    monkeypatch.setattr(setup, "wire", lambda check: called.append(check) or 0)
    assert setup.main(["check"]) == 0 and setup.main(["wire"]) == 0
    assert called == [True, False]
    with pytest.raises(SystemExit):
        setup.main(["nope"])


# ------------------------------------------------------------------ qBittorrent 5.2 login in the status poll

@pytest.mark.parametrize("login, up", [
    (httpx.Response(204), True),                       # 5.2+: success has no body
    (httpx.Response(200, text="Ok."), True),           # before 5.2
    (httpx.Response(401, text="Unauthorized"), False),  # 5.2+: wrong password
    (httpx.Response(200, text="Fails."), False),       # before 5.2
])
def test_status_poll_accepts_both_qbittorrent_login_styles(monkeypatch, login, up):
    import asyncio
    from app import services
    monkeypatch.setattr(config, "QBIT_URL", "http://localhost:8090")
    monkeypatch.setattr(config, "QBIT_USER", "admin")
    monkeypatch.setattr(config, "QBIT_PASS", "pw")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/login"):
            return login
        if req.url.path.endswith("/app/version"):
            return httpx.Response(200, text="v5.2.3")
        return httpx.Response(200, json=[] if "torrents" in req.url.path else {})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await services.qbit_snapshot(c)
    assert asyncio.run(go())["service"]["up"] is up


# ------------------------------------------------------------------ the Plex NOTE is the same in every unclaimed case

def _plex_cfg(tmp_path: Path, how: str) -> Path:
    cfg = _cfg(tmp_path, plex=PLEX_UNCLAIMED)
    prefs = cfg / setup.PLEX_PREFS
    if how == "no-plex-dir":
        import shutil
        shutil.rmtree(cfg / "plex")
    elif how == "no-preferences-yet":      # Plex's volume exists but it has not written Preferences.xml
        prefs.unlink()
    elif how == "empty-file":
        prefs.write_text("", encoding="utf-8")
    elif how == "half-written":
        prefs.write_text('<?xml version="1.0" encoding="utf-8"?>\n<Preferences MachineIdent', encoding="utf-8")
    elif how == "empty-token":
        prefs.write_text('<?xml version="1.0"?>\n<Preferences PlexOnlineToken=""/>', encoding="utf-8")
    else:
        assert how == "no-token-attribute"
    return cfg


@pytest.mark.parametrize("how", ["no-plex-dir", "no-preferences-yet", "no-token-attribute", "empty-file",
                                 "half-written", "empty-token"])
def test_secrets_plex_note_appears_whenever_no_token_was_harvested(tmp_path, monkeypatch, how):
    cfg = _plex_cfg(tmp_path, how)
    f = tmp_path / "secrets.env"
    f.write_bytes(b"")
    rc, lines = _secrets_run(f, cfg, monkeypatch)
    assert rc == 0
    assert lines.count(setup.PLEX_UNCLAIMED_NOTE) == 1, lines
    assert "PLEX_TOKEN" not in setup.env_values(f.read_bytes()) and "PLEX_TOKEN" not in lines[-1]
    # Run again: still the same single line (it is information, not a failure).
    rc, lines = _secrets_run(f, cfg, monkeypatch)
    assert rc == 0 and lines.count(setup.PLEX_UNCLAIMED_NOTE) == 1


def test_secrets_plex_note_is_gone_once_claimed_or_when_a_token_is_set(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, plex=PLEX_CLAIMED)
    f = tmp_path / "secrets.env"
    f.write_bytes(b"")
    rc, lines = _secrets_run(f, cfg, monkeypatch)
    assert rc == 0 and setup.PLEX_UNCLAIMED_NOTE not in lines and "PLEX_TOKEN" in lines[-1]
    cfg = _plex_cfg(tmp_path / "b", "no-preferences-yet")
    f.write_bytes(b"PLEX_TOKEN=mine\n")
    rc, lines = _secrets_run(f, cfg, monkeypatch)
    assert setup.PLEX_UNCLAIMED_NOTE not in lines


def test_secrets_plex_note_is_not_printed_in_core_mode(tmp_path, monkeypatch):
    f = tmp_path / "secrets.env"
    f.write_bytes(b"")
    rc, lines = _secrets_run(f, tmp_path / "no-cfg-volumes", monkeypatch)
    assert rc == 0 and setup.PLEX_UNCLAIMED_NOTE not in lines
    assert any(l.startswith("NOTE bundled apps") for l in lines)


def test_install_doc_shows_exactly_the_line_setup_secrets_prints():
    doc = (Path(__file__).parent.parent / "INSTALL.md").read_text(encoding="utf-8")
    assert f"\n{setup.PLEX_UNCLAIMED_NOTE}\n" in doc.replace("\r\n", "\n")
    assert "<- only if unclaimed" not in doc


def test_install_doc_says_secrets_waits_and_wire_needs_no_wait():
    doc = (Path(__file__).parent.parent / "INSTALL.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    sec = doc.split("### 5.4 First run")[1].split("### 5.5")[0]
    assert "up to 180 seconds" in sec and "`--wait <seconds>`" in sec and "default 180" in sec
    assert "need no wait either" in sec
    # The claims are true: the flag exists with that default, and wire never talks to MediaHub.
    import inspect
    assert "default=180" in inspect.getsource(setup.main)
    wire_src = inspect.getsource(setup.wire_steps) + inspect.getsource(setup.config_steps)
    assert "8000" not in wire_src and "MEDIAHUB" not in wire_src.upper().replace("MEDIAHUB_HOME", "")
