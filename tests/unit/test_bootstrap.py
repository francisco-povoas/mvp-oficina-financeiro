from app.infrastructure import bootstrap


def test_bootstrap_finishes_when_table_is_available(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "ensure_table", lambda settings: object())
    monkeypatch.setattr(bootstrap, "get_settings", lambda: object())

    bootstrap.main()


def test_bootstrap_retries_transient_client_error(monkeypatch) -> None:
    calls = iter([bootstrap.ClientError({"Error": {"Code": "Internal"}}, "DescribeTable"), None])

    def ensure(_settings):
        error = next(calls)
        if error:
            raise error
        return object()

    monkeypatch.setattr(bootstrap, "ensure_table", ensure)
    monkeypatch.setattr(bootstrap, "get_settings", lambda: object())
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _seconds: None)

    bootstrap.main()
