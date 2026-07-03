"""Tests for the stdlib Telegram notifier used from executor threads."""

from cvflow.app.notify import TelegramNotifier


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, data: dict[str, str]) -> None:
        self.calls.append((url, data))


def test_posts_text_to_the_bot_chat() -> None:
    poster = _Recorder()
    notify = TelegramNotifier("TOKEN123", 42, poster=poster)
    notify("hello there")
    assert len(poster.calls) == 1
    url, data = poster.calls[0]
    assert "botTOKEN123/sendMessage" in url
    assert data["chat_id"] == "42"
    assert data["text"] == "hello there"


def test_chunks_long_messages_under_the_limit() -> None:
    poster = _Recorder()
    notify = TelegramNotifier("T", 1, poster=poster)
    lines = "\n".join(f"line {i} " + "x" * 80 for i in range(120))  # ~10k chars
    notify(lines)
    assert len(poster.calls) >= 3
    assert all(len(data["text"]) <= 4000 for _, data in poster.calls)
    # nothing lost in the chunking
    assert "".join(data["text"] for _, data in poster.calls).replace("\n", "") == lines.replace(
        "\n", ""
    )


def test_prefers_newline_boundaries_when_chunking() -> None:
    poster = _Recorder()
    notify = TelegramNotifier("T", 1, poster=poster)
    notify(("a" * 3000) + "\n" + ("b" * 3000))
    assert len(poster.calls) == 2
    assert poster.calls[0][1]["text"] == "a" * 3000
    assert poster.calls[1][1]["text"] == "b" * 3000


def test_never_raises_when_the_post_fails() -> None:
    def _boom(url: str, data: dict[str, str]) -> None:
        raise OSError("network down")

    notify = TelegramNotifier("T", 1, poster=_boom)
    notify("must not raise")  # the whole point: a notice failure never kills a run


def test_empty_message_sends_nothing() -> None:
    poster = _Recorder()
    TelegramNotifier("T", 1, poster=poster)("")
    assert poster.calls == []
