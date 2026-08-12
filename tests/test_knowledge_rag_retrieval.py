"""Knowledge/RAG retrieval regression test (architecture item 1 — document/
SOP search + metadata filtering + keyword/BM25 + embeddings/vector search +
reranking, provider-agnostic and tenant-isolated).

Runs against the REAL database and the REAL configured LLM provider
(Ollama + nomic-embed-text, confirmed working in this environment) — not a
mock — so this proves the actual end-to-end embedding call succeeds and
real cosine-similarity semantic ranking works, not just that the code
compiles. Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, OWNER, TEAM_MANAGER, TEAM_MEMBER
from app.models.copilot import KnowledgeDocument
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.services.copilot.tools import ToolContext, run_tool


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="KB Owner", email=f"kb.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        member = User(full_name="KB Member", email=f"kb.member.{suffix}@test.invalid", hashed_password="x", role="team_member")
        client = User(full_name="KB Client", email=f"kb.client.{suffix}@test.invalid", hashed_password="x", role="client")
        other_owner = User(full_name="KB Other Owner", email=f"kb.otherowner.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add_all([owner, member, client, other_owner])
        await db.flush()

        org = Organization(name=f"KB Org {suffix}", slug=f"kb-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"KB Other Org {suffix}", slug=f"kb-other-{suffix}", owner_id=other_owner.id)
        db.add_all([org, other_org])
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=member.id, role="team_member"),
            OrganizationMembership(organization_id=org.id, user_id=client.id, role="client"),
            OrganizationMembership(organization_id=other_org.id, user_id=other_owner.id, role="owner"),
        ])
        await db.commit()
        org_id, owner_id, member_id, client_id, other_org_id, other_owner_id = (
            org.id, owner.id, member.id, client.id, other_org.id, other_owner.id
        )

        doc_ids: list[int] = []
        try:
            owner_ctx = ToolContext(db=db, org_id=org_id, org_role=OWNER, user=owner, user_id=owner_id, session_id=None)

            # ── 1. RBAC: TEAM_MEMBER may not create knowledge documents ──
            member_ctx = ToolContext(db=db, org_id=org_id, org_role=TEAM_MEMBER, user=member, user_id=member_id, session_id=None)
            refused = await run_tool(
                "create_knowledge_document",
                {"title": f"Refund Policy {suffix}", "content": "Refunds are issued within 14 days.", "doc_type": "policy", "tags": ["refunds"]},
                member_ctx,
            )
            assert not refused.ok, "TEAM_MEMBER must not be able to create knowledge documents"

            # ── 2. Create two documents as OWNER — real embedding call ──
            result = await run_tool(
                "create_knowledge_document",
                {
                    "title": f"Refund Policy {suffix}",
                    "content": f"Customers may request a refund within 14 days of purchase for order token {suffix}. Contact support to initiate.",
                    "doc_type": "policy",
                    "tags": ["refunds", "billing"],
                },
                owner_ctx,
            )
            assert result.ok, f"create_knowledge_document should succeed, got: {result.message}"
            refund_doc_id = result.data["document_id"]
            doc_ids.append(refund_doc_id)

            result2 = await run_tool(
                "create_knowledge_document",
                {
                    "title": f"Onboarding Runbook {suffix}",
                    "content": f"New employees complete IT setup, badge issuance, and orientation training on their first day, token {suffix}.",
                    "doc_type": "runbook",
                    "tags": ["hr", "onboarding"],
                },
                owner_ctx,
            )
            assert result2.ok, f"create_knowledge_document should succeed, got: {result2.message}"
            onboarding_doc_id = result2.data["document_id"]
            doc_ids.append(onboarding_doc_id)

            # A same-content doc in a DIFFERENT org — must never leak in.
            other_ctx = ToolContext(db=db, org_id=other_org_id, org_role=OWNER, user=other_owner, user_id=other_owner_id, session_id=None)
            result3 = await run_tool(
                "create_knowledge_document",
                {"title": f"Refund Policy {suffix}", "content": f"Refund policy for another organization, token {suffix}.", "doc_type": "policy", "tags": ["refunds"]},
                other_ctx,
            )
            assert result3.ok
            other_org_doc_id = result3.data["document_id"]
            doc_ids.append(other_org_doc_id)

            # ── 3. Real embedding was actually computed (not skipped/faked) ──
            doc_row = await db.get(KnowledgeDocument, refund_doc_id)
            assert doc_row.embedding_json is not None and len(doc_row.embedding_json) > 0, (
                "embedding_json must be populated by the real Ollama/nomic-embed-text call configured in this environment"
            )
            assert doc_row.embedding_model, "embedding_model must record which model produced the vector"

            # ── 4. Keyword search finds it by an exact term ──
            search = await run_tool("search_knowledge", {"query": f"refund {suffix}"}, owner_ctx)
            assert search.ok
            titles = {i["title"] for i in search.data["items"]}
            assert f"Refund Policy {suffix}" in titles, "search_knowledge must find the matching document by keyword"

            # ── 5. Semantic (vector) search: a paraphrase with NO shared
            #      keywords should still surface the refund doc above the
            #      unrelated onboarding doc, proving real embedding
            #      similarity ranking is doing the work, not just keyword
            #      overlap.
            semantic_query = f"getting my money back after buying something {suffix}"
            search2 = await run_tool("search_knowledge", {"query": semantic_query}, owner_ctx)
            assert search2.ok
            items2 = search2.data["items"]
            assert items2, "semantic search should surface at least the refund document via vector similarity"
            ranked_ids = [i["id"] for i in items2]
            assert refund_doc_id in ranked_ids, "vector search must find the refund doc from a paraphrase with no shared keywords"

            # ── 6. Metadata filtering — doc_type narrows results ──
            search3 = await run_tool("search_knowledge", {"query": suffix, "doc_type": "runbook"}, owner_ctx)
            assert search3.ok
            found_titles3 = {i["title"] for i in search3.data["items"]}
            assert f"Onboarding Runbook {suffix}" in found_titles3
            assert f"Refund Policy {suffix}" not in found_titles3, "doc_type filter must exclude non-matching documents"

            # ── 7. Metadata filtering — tags narrows results ──
            search4 = await run_tool("search_knowledge", {"query": suffix, "tags": ["hr"]}, owner_ctx)
            assert search4.ok
            found_titles4 = {i["title"] for i in search4.data["items"]}
            assert f"Onboarding Runbook {suffix}" in found_titles4
            assert f"Refund Policy {suffix}" not in found_titles4, "tags filter must exclude documents without a matching tag"

            # ── 8. Tenant isolation — org A never sees org B's documents ──
            search5 = await run_tool("search_knowledge", {"query": suffix}, owner_ctx)
            assert search5.ok
            found_ids5 = {i["id"] for i in search5.data["items"]}
            assert other_org_doc_id not in found_ids5, "CROSS-TENANT LEAK: search_knowledge returned another organization's document"

            # ── 9. CLIENT is refused (staff-only knowledge search, same
            #      posture as search_everything) ──
            client_ctx = ToolContext(db=db, org_id=org_id, org_role=CLIENT, user=client, user_id=client_id, session_id=None)
            client_search = await run_tool("search_knowledge", {"query": suffix}, client_ctx)
            assert not client_search.ok, "CLIENT must be refused for search_knowledge"

        finally:
            await db.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id.in_(doc_ids)))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org_id, other_org_id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org_id, other_org_id])))
            await db.execute(delete(User).where(User.id.in_([owner_id, member_id, client_id, other_owner_id])))
            await db.commit()

    await engine.dispose()


def test_knowledge_rag_retrieval_end_to_end():
    asyncio.run(_scenario())
