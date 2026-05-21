from babeltower_agent.crypto import canonical_request_string, generate_keypair, request_signature


def test_canonical_request_string_uses_protocol_shape() -> None:
    canonical = canonical_request_string(
        "post",
        "/v1/search",
        "2026-05-21T14:32:11Z",
        b"",
    )

    assert canonical == (
        b"POST\n"
        b"/v1/search\n"
        b"2026-05-21T14:32:11Z\n"
        b"e3b0c44298fc1c149afbf4c8996fb924"
        b"27ae41e4649b934ca495991b7852b855"
    )


def test_request_signature_is_base64() -> None:
    private_key, _pubkey = generate_keypair()

    signature = request_signature(
        private_key,
        "GET",
        "/v1/inbox",
        "2026-05-21T14:32:11Z",
        b"",
    )

    assert isinstance(signature, str)
    assert len(signature) > 40
