import time
from datetime import timedelta

import numpy
import pytest

from ..adapters.array import ArrayAdapter
from ..adapters.mapping import MapAdapter
from ..client import from_config
from ..client.context import CannotRefreshAuthentication
from ..client.utils import ClientError

arr = ArrayAdapter.from_array(numpy.ones((5, 5)))


tree = MapAdapter({"A1": arr, "A2": arr})


@pytest.fixture
def config(tmpdir):
    """
    Return config with

    - a unique temporary sqlite database location
    - a unique nested dict instance that the test can mutate
    """
    return {
        "authentication": {
            "secret_keys": ["SECRET"],
            "providers": [
                {
                    "provider": "toy",
                    "authenticator": "tiled.authenticators:DictionaryAuthenticator",
                    "args": {
                        "users_to_passwords": {"alice": "secret1", "bob": "secret2"}
                    },
                }
            ],
        },
        "database_uri": f"sqlite:///{tmpdir}/tiled.sqlite",
        "trees": [
            {
                "tree": f"{__name__}:tree",
                "path": "/",
            },
        ],
    }


def test_password_auth(enter_password, config):
    """
    A password that is wrong, empty, or belonging to a different user fails.
    """

    with enter_password("secret1"):
        from_config(config, username="alice", token_cache={})
    with enter_password("secret2"):
        from_config(config, username="bob", token_cache={})

    # Bob's password should not work for alice
    with pytest.raises(ClientError) as info:
        with enter_password("secret2"):
            from_config(config, username="alice", token_cache={})
    assert info.value.response.status_code == 401

    # Empty password should not work.
    with pytest.raises(ClientError) as info:
        with enter_password(""):
            from_config(config, username="alice", token_cache={})
    assert info.value.response.status_code == 422


def test_key_rotation(enter_password, config):
    """
    Rotate in a new secret used to sign keys.
    Confirm that clients experience a smooth transition.
    """

    # Obtain refresh token.
    token_cache = {}
    with enter_password("secret1"):
        from_config(config, username="alice", token_cache=token_cache)
    # Use refresh token (no prompt to reauthenticate).
    from_config(config, username="alice", token_cache=token_cache)

    # Rotate in a new key.
    config["authentication"]["secret_keys"].insert(0, "NEW_SECRET")
    assert config["authentication"]["secret_keys"] == ["NEW_SECRET", "SECRET"]
    # The refresh token from the old key is still valid.
    # We reauthenticate and receive a refresh token for the new key.
    from_config(config, username="alice", token_cache=token_cache)

    # Rotate out the old key.
    del config["authentication"]["secret_keys"][1]
    assert config["authentication"]["secret_keys"] == ["NEW_SECRET"]
    # New refresh token works with the new key.
    from_config(config, username="alice", token_cache=token_cache)


def test_refresh_flow(enter_password, config):
    """
    Run a server with an artificially short max access token age
    to force a refresh.
    """

    # Normal default configuration: a refresh is not immediately required.
    token_cache = {}
    with enter_password("secret1"):
        client = from_config(config, username="alice", token_cache=token_cache)
    token1 = client.context.tokens["access_token"]
    client["A1"]
    assert token1 is client.context.tokens["access_token"]

    # Forcing a refresh gives us a new token.
    client.context.reauthenticate()
    token2 = client.context.tokens["access_token"]
    assert token2 is not token1

    # Pathological configuration: a refresh is almost immediately required
    config["authentication"]["access_token_max_age"] = timedelta(seconds=1)
    token_cache = {}
    with enter_password("secret1"):
        client = from_config(config, username="alice", token_cache=token_cache)
    token3 = client.context.tokens["access_token"]
    time.sleep(2)
    # A refresh should happen automatically now.
    client["A1"]
    token4 = client.context.tokens["access_token"]
    assert token3 is not token4

    # Pathological configuration: sessions do not last
    config["authentication"]["session_max_age"] = timedelta(seconds=1)
    token_cache = {}
    with enter_password("secret1"):
        client = from_config(config, username="alice", token_cache=token_cache)
    time.sleep(2)
    # Refresh should fail because the session is too old.
    with pytest.raises(CannotRefreshAuthentication):
        # Set prompt=False so that this raises instead of interactively prompting.
        client.context.reauthenticate(prompt=False)


def test_revoke_session(enter_password, config):
    with enter_password("secret1"):
        client = from_config(config, username="alice", token_cache={})
    # Get the current session ID.
    info = client.context.whoami()
    (session,) = info["sessions"]
    assert not session["revoked"]
    # Revoke it.
    client.context.revoke_session(session["uuid"])
    # Update info and confirm it is listed as revoked.
    updated_info = client.context.whoami()
    (updated_session,) = updated_info["sessions"]
    assert updated_session["revoked"]
    # Confirm it cannot be refreshed.
    with pytest.raises(CannotRefreshAuthentication):
        # Set prompt=False so that this raises instead of interactively prompting.
        client.context.reauthenticate(prompt=False)
