"""services/a2a_actor — the hub-signed A2A memory actor (N16-obs90)."""

from app.services import a2a_actor

FAKE_SECRET = "fake-session-secret-for-tests"  # C4: never a real secret


def test_actor_key_is_per_agent_deterministic_and_not_the_secret():
    k1 = a2a_actor.actor_key("agent-1", secret=FAKE_SECRET)
    assert k1 == a2a_actor.actor_key("agent-1", secret=FAKE_SECRET)
    assert k1 != a2a_actor.actor_key("agent-2", secret=FAKE_SECRET)
    assert k1 != a2a_actor.actor_key("agent-1", secret="another-fake-secret")  # rotation
    assert len(k1) == 64 and FAKE_SECRET not in k1


def test_actor_key_defaults_to_the_console_session_secret(monkeypatch):
    import app.routers.auth as auth

    monkeypatch.setattr(auth, "session_secret", lambda settings=None: FAKE_SECRET)
    assert a2a_actor.actor_key("agent-1") == a2a_actor.actor_key("agent-1", secret=FAKE_SECRET)


def test_signature_binds_context_and_actor():
    key = a2a_actor.actor_key("agent-1", secret=FAKE_SECRET)
    sig = a2a_actor.sign_actor(key, "ctx-1", "agent-1__river")
    assert a2a_actor.verify_actor(key, "ctx-1", "agent-1__river", sig)
    assert not a2a_actor.verify_actor(key, "ctx-2", "agent-1__river", sig)  # another context
    assert not a2a_actor.verify_actor(key, "ctx-1", "agent-1__alice", sig)  # another human
    assert not a2a_actor.verify_actor("other-key", "ctx-1", "agent-1__river", sig)
    assert not a2a_actor.verify_actor(key, "ctx-1", "agent-1__river", "")
