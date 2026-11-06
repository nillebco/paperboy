import asyncio

import pytest

from later_ink.matins import routes, speech

from test_matins import _csrf, _signup_without_readwise, _tts_dir, client  # noqa: F401


class FakeRemote:
    def __init__(self, held=None):
        self.held = dict(held or {})
        self.puts = []

    def get(self, key):
        return self.held.get(key)

    def put(self, key, data):
        self.puts.append(key)
        self.held[key] = data


def _speech(tmp_path, made, remote=None, max_bytes=10_000_000):
    def synth(voice, text):
        made.append((voice, text))
        return f"{voice}:{text}".encode()

    return speech.Speech(tmp_path / "speech", max_bytes, synth, remote)


async def _audio(service, key):
    path = await service.audio(key, timeout=5)
    return path.read_bytes() if path else None


def test_speech_makes_each_paragraph_once_and_keeps_it(tmp_path):
    made = []
    service = _speech(tmp_path, made)
    keys = service.submit("af_heart", ["One.", "Two."])
    assert len(set(keys)) == 2
    assert asyncio.run(_audio(service, keys[1])) == b"af_heart:Two."
    assert asyncio.run(_audio(service, keys[0])) == b"af_heart:One."
    # Same text, same voice: the same key, nothing made again.
    assert service.submit("af_heart", ["One."]) == keys[:1]
    assert asyncio.run(_audio(service, keys[0])) == b"af_heart:One."
    assert sorted(made) == [("af_heart", "One."), ("af_heart", "Two.")]
    assert service.submit("ff_siwis", ["One."]) != keys[:1]


def test_speech_unknown_key_is_none(tmp_path):
    service = _speech(tmp_path, [])
    assert asyncio.run(service.audio("0" * 32, timeout=1)) is None


def test_speech_prefers_object_storage_and_fills_it(tmp_path):
    made = []
    held_key = speech.key_for("af_heart", "Held.")
    remote = FakeRemote({held_key: b"from the bucket"})
    service = _speech(tmp_path, made, remote)
    held, fresh = service.submit("af_heart", ["Held.", "Fresh."])
    assert asyncio.run(_audio(service, held)) == b"from the bucket"
    assert asyncio.run(_audio(service, fresh)) == b"af_heart:Fresh."
    assert made == [("af_heart", "Fresh.")]
    assert remote.puts == [fresh]


def test_speech_evicts_the_oldest_files(tmp_path):
    service = _speech(tmp_path, [], max_bytes=40)
    keys = service.submit("af_heart", [f"Paragraph {i} " + "x" * 10 for i in range(5)])
    for key in keys:
        asyncio.run(_audio(service, key))
    files = list((tmp_path / "speech").glob("*.mp3"))
    assert sum(f.stat().st_size for f in files) <= 40
    assert service.path(keys[-1]).is_file()


@pytest.fixture
def server_speech(tmp_path, monkeypatch):
    made = []
    service = _speech(tmp_path, made)
    monkeypatch.setattr(speech, "service", lambda: service)
    return made


def test_reader_posts_paragraphs_and_plays_their_mp3(client, tmp_path, monkeypatch, server_speech):
    _tts_dir(tmp_path, monkeypatch)
    secret = _signup_without_readwise(client)
    keys = client.post(
        f"/{secret}/read/speech", json={"voice": "af_heart", "blocks": ["A title", "A  paragraph."]}
    ).json()["keys"]
    audio = client.get(f"/{secret}/read/speech/{keys[1]}.mp3")
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/mpeg"
    assert audio.content == b"af_heart:A paragraph."
    assert "immutable" in audio.headers["cache-control"]
    assert client.get(f"/{secret}/read/speech/{'0' * 32}.mp3").status_code == 404
    assert client.get(f"/{secret}/read/speech/nope.mp3").status_code == 404
    bad = client.post(f"/{secret}/read/speech", json={"voice": "zz_nobody", "blocks": ["x"]})
    assert bad.status_code == 400
    assert client.post(f"/{secret}/read/speech", json={"voice": "af_heart", "blocks": []}).status_code == 400


def test_speech_needs_the_catalogue_secret(client, server_speech):
    response = client.post("/nope/read/speech", json={"voice": "af_heart", "blocks": ["x"]})
    assert response.status_code == 404


def test_server_speech_is_off_without_the_model(client, monkeypatch):
    monkeypatch.setattr(speech, "service", lambda: None)
    secret = _signup_without_readwise(client)
    response = client.post(f"/{secret}/read/speech", json={"voice": "af_heart", "blocks": ["x"]})
    assert response.status_code == 404


def test_settings_choose_the_voice_engine(client, tmp_path, monkeypatch, server_speech):
    _tts_dir(tmp_path, monkeypatch)
    secret = _signup_without_readwise(client)
    page = client.get(f"/{secret}/settings").text
    assert '<option value="kokoro" selected>Kokoro, in your browser</option>' in page
    assert 'value="server"' in page
    form = {"csrf": _csrf(client, secret), "schedule": "07:00", "timezone": "Europe/Paris", "enabled": "1"}
    client.post(f"/{secret}/settings", data={**form, "listen_engine": "server"})
    assert client.app.state.matins.get_delivery(secret).listen_engine == "server"
    assert '<option value="server" selected>' in client.get(f"/{secret}/settings").text
    client.post(f"/{secret}/settings", data={**form, "listen_engine": "kokoro"})
    assert client.app.state.matins.get_delivery(secret).listen_engine is None


def test_server_engine_falls_back_to_kokoro_when_the_server_cannot_speak(monkeypatch):
    monkeypatch.setattr(speech, "service", lambda: None)
    delivery = type("D", (), {"listen_engine": "server"})()
    assert routes._listen_engine(delivery) == "kokoro"
    assert routes._listen_engine(type("D", (), {"listen_engine": "device"})()) == "device"
    assert routes._listen_engine(None) == "kokoro"
