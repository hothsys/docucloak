"""_read_app_config, _ensure_dependencies and main()."""

import builtins
import subprocess
import sys

import pytest

from lib import config, dependencies, main, web


# ─── _read_app_config ──────────────────────────────────────────────────────────

def test_read_app_config_parses_keys_and_strips_quotes(isolated_app):
    (isolated_app / 'app.conf').write_text(
        '# comment line\n'
        '\n'
        'APP_NAME=DocuCloak\n'
        'APP_TITLE="DocuCloak Redactor"\n'
        "SINGLE='quoted'\n"
        '  PORT = 6000  \n'
        'URL=http://x/?a=b\n'
        'no equals sign here\n'
    )
    cfg = config._read_app_config()
    assert cfg == {
        'APP_NAME': 'DocuCloak',
        'APP_TITLE': 'DocuCloak Redactor',
        'SINGLE': 'quoted',
        'PORT': '6000',
        'URL': 'http://x/?a=b',  # only the first '=' splits
    }


def test_read_app_config_missing_file_returns_empty(isolated_app):
    assert config._read_app_config() == {}


def test_real_app_conf_is_loaded_at_import():
    assert config.APP_NAME == 'DocuCloak'
    assert config._APP_CONFIG.get('PORT') == '5903'


# ─── _ensure_dependencies ──────────────────────────────────────────────────────

def _fake_import(missing):
    real_import = builtins.__import__

    def fake(name, *args, **kwargs):
        if name in missing:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)
    return fake


def test_ensure_dependencies_noop_when_all_present(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, 'check_call', lambda *a, **k: calls.append(a))
    dependencies._ensure_dependencies()
    assert calls == []


def test_ensure_dependencies_installs_only_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, 'check_call', lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(builtins, '__import__', _fake_import({'pytesseract', 'PIL'}))
    dependencies._ensure_dependencies()
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:4] == [sys.executable, '-m', 'pip', 'install']
    assert sorted(cmd[5:]) == ['Pillow>=10.0.0', 'pytesseract>=0.3.10']


def test_ensure_dependencies_propagates_pip_failure(monkeypatch):
    def boom(cmd, **k):
        raise subprocess.CalledProcessError(1, cmd)
    monkeypatch.setattr(subprocess, 'check_call', boom)
    monkeypatch.setattr(builtins, '__import__', _fake_import({'pymupdf'}))
    with pytest.raises(subprocess.CalledProcessError):
        dependencies._ensure_dependencies()


# ─── main ──────────────────────────────────────────────────────────────────────

class FakeServer:
    instances = []

    def __init__(self, addr, handler):
        self.addr, self.handler, self.shut = addr, handler, False
        FakeServer.instances.append(self)

    def serve_forever(self):
        raise KeyboardInterrupt

    def shutdown(self):
        self.shut = True


@pytest.fixture
def fake_server(monkeypatch):
    FakeServer.instances = []
    monkeypatch.setattr(main, 'ThreadingHTTPServer', FakeServer)
    return FakeServer


def test_main_uses_app_conf_port_and_shuts_down_on_ctrl_c(fake_server, monkeypatch, caplog):
    monkeypatch.setattr(config, '_APP_CONFIG', {'PORT': '6123'})
    monkeypatch.delenv('PORT', raising=False)
    main.main()
    srv = fake_server.instances[0]
    assert srv.addr == ('127.0.0.1', 6123), 'must only accept connections from this Mac'
    assert srv.handler is web.Handler
    assert srv.shut is True
    assert 'running at http://localhost:6123' in caplog.text
    assert 'Shutting down' in caplog.text


def test_main_app_conf_port_overrides_env(fake_server, monkeypatch):
    monkeypatch.setattr(config, '_APP_CONFIG', {'PORT': '6123'})
    monkeypatch.setenv('PORT', '7000')
    main.main()
    assert fake_server.instances[0].addr[1] == 6123


def test_main_falls_back_to_env_then_default(fake_server, monkeypatch):
    monkeypatch.setattr(config, '_APP_CONFIG', {'PORT': 'not-a-number'})
    monkeypatch.setenv('PORT', '7000')
    main.main()
    monkeypatch.delenv('PORT')
    monkeypatch.setattr(config, '_APP_CONFIG', {})
    main.main()
    assert [s.addr[1] for s in fake_server.instances] == [7000, 5903]
