"""Agent 2 — Wireframe.

Builds an editable wireframe (structured JSON, rendered by the CRM itself) from
the meeting notes. Uses Kimi K2.5 for its native vision — it can read a
whiteboard photo from the meeting — plus reliable structured output.

Inputs: meeting notes (+ project), a library of past wireframes as few-shot
examples (agent.artifact_examples — docs §9.4; expected EMPTY for a while, see
that section: this artifact's JSON schema was invented here, so no historical
wireframe exists in it yet), and an optional whiteboard image (not yet carried
by the ingestion path — see note below).

Modes: create (fresh) | follow-up (load latest, change only what the notes ask).

Schema (finalized from the architecture interrogation, docs §9.1): a flat list
of screens, each with its purpose, components, which roles see it, and where it
navigates — enough to cover the confirmed requirements (20+ screens, conditional
flows by role, low-fidelity/no visual styling) without over-specifying a layout
grid the CRM doesn't render anyway.

Persistence: agent.wireframe_drafts (our schema — the CRM has no table for this
artifact type; see app.services.wireframe_persist).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.graph.state import BuildState


class WireframeScreen(BaseModel):
    name: str
    purpose: str = Field(..., description="One line: what this screen is for.")
    components: list[str] = Field(..., description="The elements/sections on this screen.")
    visible_to_roles: list[str] = Field(
        default_factory=list, description="User roles that see this screen; empty means all roles."
    )
    navigates_to: list[str] = Field(
        default_factory=list, description="Names of screens reachable from here."
    )


class WireframeDraft(BaseModel):
    screens: list[WireframeScreen]

    @classmethod
    def stub(cls, messages: list | None = None) -> "WireframeDraft":
        """Canned output for MODEL_PROVIDER=stub.

        With no context, a fixed 2-screen skeleton — enough to exercise the
        Judge/persist plumbing after it in production dev, nothing more.

        Given the real messages (see app/services/stub_models.py — same
        opt-in mechanism as ClassificationResult.stub), derives a richer set
        of screens from keywords in the notes instead, up to 5. This is what
        lets the mock harness (app/mock/) show something resembling an actual
        wireframe instead of the same 2 placeholder screens for every meeting."""
        if not messages:
            return cls(
                screens=[
                    WireframeScreen(
                        name="[stub] Dashboard",
                        purpose="Landing screen after login.",
                        components=["Summary cards", "Recent activity list"],
                        visible_to_roles=[],
                        navigates_to=["[stub] Detail"],
                    ),
                    WireframeScreen(
                        name="[stub] Detail",
                        purpose="Drill into one item.",
                        components=["Item header", "Edit form"],
                        visible_to_roles=[],
                        navigates_to=[],
                    ),
                ]
            )

        text = " ".join(
            str(part)
            for message in messages
            for part in (message if isinstance(message, (tuple, list)) else [getattr(message, "content", message)])
        ).lower()

        roles = []
        for keyword, role in (
            ("almacenista", "warehouse staff"), ("supervisor", "supervisor"),
            ("administrador", "admin"), ("admin", "admin"), ("manager", "manager"),
        ):
            if keyword in text and role not in roles:
                roles.append(role)

        candidates = [
            ("Login", "Role-based sign-in.", ["Email/username field", "Password field", "Role redirect"], []),
            ("Dashboard", "Landing screen after login; at-a-glance summary.",
             ["Summary cards", "Recent activity list"], roles),
            ("Inventory / Stock", "Register entries and exits; barcode scan support.",
             ["Barcode scan input", "Item list", "Quantity adjust"], roles[:1]),
            ("Approvals", "Review and approve pending stock/budget adjustments.",
             ["Pending list", "Approve/reject buttons", "Audit trail"], roles[1:2] or roles[:1]),
            ("Reports", "Monthly exportable report.", ["Date range filter", "Export to Excel button", "Summary chart"], roles),
            ("User Management", "Manage users, roles and catalogs.",
             ["User list", "Role assignment", "Catalog editor"], roles[-1:] or roles),
            ("Alerts", "Low-stock and threshold notifications.", ["Alert list", "Threshold config"], roles[:1]),
        ]

        keywords_by_screen = {
            "Login": ["login", "rol", "role", "acceso"],
            "Inventory / Stock": ["inventario", "inventory", "stock", "almac", "código de barras", "barcode"],
            "Approvals": ["aprob", "approv", "ajuste"],
            "Reports": ["reporte", "report", "excel", "export"],
            "User Management": ["usuario", "user", "catálogo", "catalog", "gestion"],
            "Alerts": ["alerta", "alert", "mínimo", "threshold"],
        }

        selected = [c for c in candidates if c[0] == "Dashboard"]  # Dashboard always present
        for cand in candidates:
            name = cand[0]
            if name == "Dashboard":
                continue
            triggers = keywords_by_screen.get(name)
            if triggers is None or any(k in text for k in triggers):
                selected.append(cand)
        selected = selected[:5]

        names = [f"[stub] {c[0]}" for c in selected]
        return cls(
            screens=[
                WireframeScreen(
                    name=f"[stub] {name}",
                    purpose=purpose,
                    components=components,
                    visible_to_roles=screen_roles,
                    navigates_to=[n for n in names if n != f"[stub] {name}"][:2],
                )
                for name, purpose, components, screen_roles in selected
            ]
        )


async def build_wireframe(state: BuildState) -> BuildState:
    from app.config import model_id_for
    from app.services.bedrock import chat_model_for
    from app.services.example_library import format_examples_block, load_examples
    from app.services.wireframe_persist import load_latest_wireframe

    prior = ""
    if state.get("mode") == "follow_up":
        latest = load_latest_wireframe(state["project_id"])
        if latest:
            prior = f"\n\nCurrent wireframe to revise (change ONLY what the notes ask):\n{latest['payload']}"

    model = chat_model_for("wireframe").with_structured_output(WireframeDraft)
    system = (
        "You are Oblivion's wireframe agent. From the meeting notes, produce an editable, "
        "low-fidelity wireframe: the screens implied by the scope, their components, which "
        "user roles see each screen, and navigation between them. If the notes lack enough "
        "detail to infer a coherent screen, do NOT invent it — omit it rather than guess."
    )
    examples = format_examples_block(
        load_examples("wireframe"),
        heading="Past wireframes to match in structure and level of detail (do NOT copy their screens):",
    )
    human = f"Meeting notes:\n{state['notes']}{prior}{examples}"

    # If a whiteboard photo is available it would be attached to the human message here
    # (Kimi K2.5 has native vision) — left out until the ingestion path carries images
    # (Plaud integration, still pending — docs §9).
    draft: WireframeDraft = await model.ainvoke([("system", system), ("human", human)])

    return {
        **state,
        "current_artifact_type": "wireframe",
        "draft": {"payload": draft.model_dump(), "model_id": model_id_for("wireframe")},
    }
