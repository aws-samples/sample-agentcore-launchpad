"""routers/knowledge.py runs the ledger guards BEFORE the backend lookup (N1 contract note)."""
from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.services import knowledge_backend


def _mount(kb_id, name="reader", system_key=None):
    db = SessionLocal()
    db.add(Agent(workspace_id="default", name=name, method="zip_runtime", status="active",
                 spec={"knowledge_bases": [{"kb_id": kb_id}]}, system_key=system_key))
    db.commit()
    db.close()


def test_delete_409s_from_the_ledger_before_any_backend(client):
    knowledge_backend.set_backend(None)
    _mount("KBAAAA")
    res = client.delete("/api/knowledge-bases/KBAAAA")
    assert res.status_code == 409
    assert res.json()["code"] == "kb.has_attached_agents"
    assert res.json()["detail"] == {"agents": ["reader"]}
    res = client.delete("/api/knowledge-bases/KBAAAA?force=true")
    # guard passed, slot empty
    assert res.status_code == 503 and res.json()["code"] == "feature.unavailable"


def test_delete_refuses_a_system_preset_mount_even_with_force(client):
    knowledge_backend.set_backend(None)
    _mount("KBSYS", name="frontdesk", system_key="frontdesk")
    res = client.delete("/api/knowledge-bases/KBSYS?force=true")
    assert res.status_code == 409 and res.json()["code"] == "kb.attached_to_system_agent"


def test_create_app_registers_the_selfhosted_knowledge_backend(client):
    from app.services.knowledge_selfhosted import SelfHostedKnowledgeBackend

    assert isinstance(knowledge_backend.backend_or_none(), SelfHostedKnowledgeBackend)


def test_list_without_substrate_is_empty_not_503(client):
    res = client.get("/api/knowledge-bases")
    assert res.status_code == 200 and res.json() == {"items": []}
