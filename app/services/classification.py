"""Agent 1's core job: figure out which project a meeting belongs to, and what
kind of meeting it is.

Two-stage resolution, cheapest-first:

  1. DETERMINISTIC MATCH — no LLM call. Match the calendar invite's attendee
     emails and the meeting text against:
       * `agent.project_matchers` (aliases + known client emails WE control), and
       * the project's own name + its client's name/company (read from the CRM's
         `public.projects` / `public.clients`).
     If exactly one project matches, the project is resolved: confidence 1.0,
     zero cost, zero non-determinism.

     `project_matchers` starts empty and is grown by `apply_classification` /
     `_learn_email_matchers` below: every time a meeting is confidently
     classified (step 2), its attendee emails are learned as matchers for that
     project (unless already claimed by any project), so the NEXT meeting with
     the same attendees resolves in step 1 instead of paying for the LLM again.

  2. LLM FALLBACK — only if step 1 finds zero or more-than-one candidate, OR to
     decide the meeting *class* (which always needs the notes). GLM-4.7-Flash
     returns a structured classification.

A result is only auto-applied if confidence >= settings.classification_
confidence_threshold. Below that, the intake row is left status='pending_review'
for a human — the review queue the original whiteboard assumed but never spec'd.

We deliberately never auto-create a project. A misheard client name creating a
duplicate project is worse than one extra item in the review queue.

CRM note: reads `public.projects`/`public.clients` (owned by the Lovable CRM),
writes classification onto `agent.meeting_intake` (our schema). See
docs/ARCHITECTURE.md §3 "CRM integration".
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# bedrock/supabase/settings are imported lazily inside the functions that use
# them, so the pure `_deterministic_match` can be imported and tested without the
# LLM/DB stack. Low coupling, fast tests.

MeetingClass = Literal["onboarding", "follow_up", "update", "final_qa"]
SubType = Literal["wireframe", "plan", "gantt", "budget"]


class ClassificationResult(BaseModel):
    project_id: Optional[str] = Field(
        None, description="UUID of the matched project, or null if none matched / new project."
    )
    new_project_suggested_name: Optional[str] = Field(
        None, description="If no existing project matched, a suggested name for a new one."
    )
    meeting_class: MeetingClass
    sub_type: Optional[SubType] = Field(
        None, description="Required when meeting_class is 'follow_up', otherwise null."
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str = Field(..., description="One-sentence justification, for the review queue UI.")
    # Not model-generated: set by classify_meeting to record HOW the project was
    # resolved, for agent.meeting_intake.classification_method.
    method: str = Field("llm", exclude=True)

    @classmethod
    def stub(cls, messages: list | None = None) -> "ClassificationResult":
        """Canned output for MODEL_PROVIDER=stub.

        With no context (a plain unit test) it stays deliberately low-confidence,
        so a stubbed run lands in the review queue instead of silently
        auto-assigning a project it never actually reasoned about.

        Given the real messages (see app/services/stub_models.py), it derives the
        meeting CLASS from keywords instead. That's what lets the mock harness
        (app/mock/) exercise all four routes end to end without a live model —
        the project itself is still resolved deterministically upstream, never
        guessed here."""
        if not messages:
            return cls(
                project_id=None,
                new_project_suggested_name=None,
                meeting_class="onboarding",
                sub_type=None,
                confidence=0.0,
                reasoning="[stub] no real model — routed to review queue.",
            )

        # ONLY the human message. The system prompt names all four classes
        # ("...or 'final_qa' (acceptance stage)"), so scanning it would match
        # every keyword at once and classify everything identically.
        text = " ".join(_human_message_text(m) for m in messages).lower()

        meeting_class, sub_type = "onboarding", None
        if any(k in text for k in ("acceptance", "final qa", "sign off", "signs off", "aceptación")):
            meeting_class = "final_qa"
        elif any(k in text for k in ("progress", "sprint", "avance", "blocked", "so far")):
            meeting_class = "update"
        elif any(k in text for k in ("follow-up", "follow up", "seguimiento", "revisión", "revisar", "adjust")):
            meeting_class = "follow_up"
            for keyword, artifact in (
                ("presupuesto", "budget"), ("budget", "budget"),
                ("gantt", "gantt"), ("cronograma", "gantt"),
                ("wireframe", "wireframe"), ("plan", "plan"),
            ):
                if keyword in text:
                    sub_type = artifact
                    break
            sub_type = sub_type or "plan"

        return cls(
            project_id=None,  # resolved deterministically by classify_meeting, not here
            new_project_suggested_name=None,
            meeting_class=meeting_class,
            sub_type=sub_type,
            confidence=0.0,  # only the deterministic match may raise this
            reasoning=f"[stub] keyword-derived class={meeting_class} sub_type={sub_type}",
        )


def _internal_domains() -> set[str]:
    """Email domains belonging to us, not to a client. Configurable because it
    differs per deployment (see Settings.internal_email_domains)."""
    from app.config import get_settings

    raw = get_settings().internal_email_domains or ""
    return {d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()}


def _human_message_text(message) -> str:
    """Content of a human/user message, empty string for anything else.
    Messages arrive as ("human", content) tuples or as LangChain objects."""
    if isinstance(message, (tuple, list)) and len(message) == 2:
        role, content = message
        return str(content) if str(role).lower() in ("human", "user") else ""
    role = getattr(message, "type", getattr(message, "role", ""))
    return str(getattr(message, "content", "")) if str(role).lower() in ("human", "user") else ""


def _deterministic_match(
    attendee_emails: list[str],
    title_and_notes: str,
    active_projects: list[dict],
    matchers: list[dict],
) -> list[dict]:
    """Return the subset of `active_projects` that match, by:
      * an attendee email present in a project's `email` matchers, or
      * an `alias` matcher / the project name / the client name / the client
        company appearing as a substring in the meeting text.

    `active_projects` items: {id, name, client_name, client_company}.
    `matchers` items: {project_id, kind ('alias'|'email'), value}.
    Cheap, exact, and run before any LLM call.
    """
    text = title_and_notes.lower()
    emails = {e.lower() for e in attendee_emails}

    aliases_by_project: dict[str, list[str]] = {}
    emails_by_project: dict[str, set[str]] = {}
    for m in matchers:
        if m["kind"] == "alias":
            aliases_by_project.setdefault(m["project_id"], []).append(m["value"])
        elif m["kind"] == "email":
            emails_by_project.setdefault(m["project_id"], set()).add(m["value"].lower())

    matches = []
    for project in active_projects:
        pid = project["id"]
        email_hit = bool(emails & emails_by_project.get(pid, set()))
        name_terms = [project["name"], project.get("client_name"), project.get("client_company")]
        name_terms += aliases_by_project.get(pid, [])
        alias_hit = any(term and term.lower() in text for term in name_terms)
        if email_hit or alias_hit:
            matches.append(project)
    return matches


def _load_active_projects(supabase) -> list[dict]:
    """Read active CRM projects joined with their client name/company. Flattens
    the PostgREST nested `clients` object into client_name/client_company."""
    rows = (
        supabase.table("projects")
        .select("id,name,clients(name,company)")
        .eq("status", "active")
        .execute()
        .data
    )
    projects = []
    for r in rows:
        client = r.get("clients") or {}
        projects.append(
            {
                "id": r["id"],
                "name": r["name"],
                "client_name": client.get("name"),
                "client_company": client.get("company"),
            }
        )
    return projects


async def classify_meeting(*, attendee_emails: list[str], title_and_notes: str) -> ClassificationResult:
    from app.db.client import get_supabase

    supabase = get_supabase()
    active_projects = _load_active_projects(supabase)
    matchers = supabase.schema("agent").table("project_matchers").select("*").execute().data

    candidates = _deterministic_match(attendee_emails, title_and_notes, active_projects, matchers)

    # Whether or not the project is resolved deterministically, the meeting *class*
    # (onboarding / follow_up / update / final_qa) always requires reading the notes,
    # so we always run the classifier — passing the pre-resolved project when we have
    # exactly one, so the model only decides class/sub_type.
    forced = candidates[0] if len(candidates) == 1 else None
    result = await _llm_classify(title_and_notes, active_projects, forced_project=forced)

    if forced is None:
        return result

    # THE DETERMINISTIC MATCH WINS on *which project* (docs §4.1: "exactly one
    # project matches -> done, confidence 1.0"). Previously this was only a hint
    # in the prompt, which left the model free to contradict an exact
    # email/alias hit and attach the meeting to the wrong project — the one
    # outcome §4.1 is explicitly written to prevent. The model is still the sole
    # decider of meeting_class/sub_type, which is all it was ever needed for.
    return result.model_copy(
        update={
            "project_id": forced["id"],
            "new_project_suggested_name": None,
            "confidence": 1.0,
            "method": "deterministic",
            "reasoning": f"Project matched deterministically ({forced['name']}). {result.reasoning}",
        }
    )


async def _llm_classify(
    title_and_notes: str, active_projects: list[dict], forced_project: dict | None
) -> ClassificationResult:
    from app.services.bedrock import chat_model_for

    model = chat_model_for("meeting_notes").with_structured_output(ClassificationResult)

    project_list = "\n".join(
        f"- id={p['id']} name={p['name']!r} client={p.get('client_name')!r}" for p in active_projects
    )
    forced_hint = (
        f"\nThe project has ALREADY been determined: id={forced_project['id']}. "
        f"Set project_id to exactly that value and focus only on meeting_class/sub_type."
        if forced_project
        else ""
    )

    system = (
        "You classify a meeting transcript for Oblivion, a custom software company. "
        "Given the list of active projects below and the meeting content, decide:\n"
        "1) which project this meeting belongs to (or that it's a brand new project),\n"
        "2) the meeting class: 'onboarding' (new project, run the full build chain), "
        "'follow_up' (revising one existing artifact — set sub_type to wireframe/plan/gantt/budget), "
        "'update' (progress check on a live build), or 'final_qa' (acceptance stage).\n"
        "If nothing matches confidently, set project_id to null and fill "
        "new_project_suggested_name instead. Never invent a project_id.\n\n"
        f"Active projects:\n{project_list}{forced_hint}"
    )

    return await model.ainvoke([("system", system), ("human", title_and_notes)])


async def apply_classification(
    intake_id: str, result: ClassificationResult, attendee_emails: list[str] | None = None
) -> None:
    """Persist the classification onto the intake row, routing low-confidence
    results to the human review queue instead of auto-assigning them.

    When a project is confidently resolved, also *learns* new email matchers
    (agent.project_matchers) from this meeting's attendees — so the next
    meeting with the same attendees resolves deterministically (step 1, zero
    LLM cost) instead of falling through to the LLM every time. This is the
    only writer of that table; it starts empty and grows from confirmed
    classifications."""
    from app.config import get_settings
    from app.db.client import get_supabase

    settings = get_settings()
    supabase = get_supabase()

    status = (
        "classified"
        if result.confidence >= settings.classification_confidence_threshold
        else "pending_review"
    )

    supabase.schema("agent").table("meeting_intake").update(
        {
            "project_id": result.project_id,
            "class": result.meeting_class,
            "sub_type": result.sub_type,
            "classification_confidence": result.confidence,
            "classification_method": result.method,
            "status": status,
        }
    ).eq("id", intake_id).execute()

    if status == "classified" and result.project_id and attendee_emails:
        _learn_email_matchers(supabase, result.project_id, attendee_emails)


def _learn_email_matchers(supabase, project_id: str, attendee_emails: list[str]) -> None:
    """Add a matcher for each attendee email not already claimed by ANY project
    (including this one) — never reassign an email already known to belong to
    a different project.

    OUR OWN PEOPLE ARE EXCLUDED (internal_email_domains). Found by the mock
    harness: without this, the first meeting teaches "mvila@oblivion..." ->
    project A. Every later meeting for project B then matches BOTH A (via our
    own attendee) and B (via the client's), which is 2 candidates — ambiguous —
    so deterministic matching silently stops working entirely and every meeting
    falls through to the LLM. Our staff attend every client's meetings, so their
    addresses carry no signal about which project a meeting belongs to."""
    settings_domains = _internal_domains()
    emails = {
        e.lower()
        for e in attendee_emails
        if "@" in e and e.lower().rsplit("@", 1)[1] not in settings_domains
    }
    if not emails:
        return

    existing = (
        supabase.schema("agent")
        .table("project_matchers")
        .select("project_id,value")
        .eq("kind", "email")
        .in_("value", list(emails))
        .execute()
        .data
    )
    already_known = {row["value"] for row in existing}

    new_rows = [
        {"project_id": project_id, "kind": "email", "value": email}
        for email in emails
        if email not in already_known
    ]
    if new_rows:
        supabase.schema("agent").table("project_matchers").insert(new_rows).execute()
