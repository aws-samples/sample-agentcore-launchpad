"""The preset catalogue: what a system-managed agent *is*.

Pure data + pure functions — no ledger, no AWS. ``build_spec`` derives the server-owned
``AgentSpec`` from the preset and the administrator's install choices (model, optional
knowledge bases); everything else about the agent is fixed here so a client cannot
smuggle changes through a request body.
"""

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from app.schemas.agent import DEFAULT_MODEL_ID, AgentSpec, KnowledgeBaseRef, ToolRef
from app.services.skill_ingest import (
    SKILL_BUNDLE_MAX_BYTES,
    bundle_from_dir,
    parse_frontmatter,
    validate_bundle,
)

SKILLS_ROOT = Path(__file__).resolve().parent / "skills"

# S3 prefix family for versioned preset skill bundles. Deliberately disjoint from the
# member-writable ``skills/`` (registry) and ``agent-skills/`` (wizard staging) prefixes:
# the execution role of a preset reads only its own version directory, and a member
# import can never overwrite what the preset loads.
SYSTEM_SKILLS_PREFIX = "system-skills"

# Public AWS Knowledge MCP server (unauthenticated, streamable HTTP). The same URL the
# Studio sample flow uses; a remote_mcp harness tool needs no IAM grant of its own —
# ``auth: "none"`` in the ToolRef config is what tells agent_iam to skip the
# workload-identity / token-vault statements it grants authenticated MCP refs.
AWS_KNOWLEDGE_MCP_URL = "https://knowledge-mcp.global.api.aws"
AWS_KNOWLEDGE_TOOL_NAME = "aws_knowledge"


@dataclass(frozen=True)
class SystemPreset:
    key: str  # ledger ``Agent.system_key`` and the API path segment
    name: str  # reserved ``Agent.name`` (AgentSpec name pattern)
    label: str
    description: str
    skill_version: str
    system_prompt: str
    # Harness ``allowedTools`` patterns. The harness exposes ``shell`` and
    # ``file_operations`` to every session unless restricted; an advisory agent gets the
    # file tools its skills need, its MCP server, and no shell.
    allowed_tools: tuple[str, ...]
    max_iterations: int = 30
    timeout_seconds: int = 900
    skill_dir: str = ""  # directory name under SKILLS_ROOT

    def skill_path(self) -> Path:
        return SKILLS_ROOT / (self.skill_dir or self.name)

    def skill_prefix(self, digest: str | None = None) -> str:
        """Release S3 key prefix (no bucket), trailing slash.

        Content-addressed: ``<version>-<digest12>`` names one exact validated snapshot,
        so two valid snapshots of the same version (say, one with an extra reference
        file) can never share a directory — the Harness loads the whole directory, so
        a losing writer must not be able to add bytes to the winner's deployed tree.
        ``digest=None`` yields the plain version directory (pre-content-addressing
        specs, and the parent for listing).
        """
        release = self.skill_version if not digest else f"{self.skill_version}-{digest[:12]}"
        return f"{SYSTEM_SKILLS_PREFIX}/{self.name}/{release}/"

    def skill_uri(self, bucket: str, digest: str | None = None) -> str:
        return f"s3://{bucket}/{self.skill_prefix(digest)}"


ARCHITECT_SYSTEM_PROMPT = """\
# AWS agent solution architect

You are a senior AWS AI-agent solution architect. You turn a customer's business
requirement into an implementable, evaluable and operable production-grade AWS design,
and you write the formal design document when the customer asks for one.

Rules you always follow:
1. Reply in the language of the customer's most recent message unless they ask
   otherwise. AWS service names, APIs, code and industry terms may stay in English.
2. Run the `aws-agent-solution-architect` skill workflow; do not recite the workflow to
   the customer.
3. Evidence order: current official AWS documentation (read through the AWS Knowledge
   tool in this conversation) > the production-agent methodology guide > customer input
   > explicitly labelled assumptions. Service availability, model versions, Regional
   support, quotas, prices and preview status are verified through the AWS Knowledge
   tool and dated; never written from memory.
4. Every new conversation is an independent requirement-intake session. Only facts the
   customer states or re-confirms in this conversation count; earlier conversations,
   persistent memory, old documents and old design contracts never pre-answer a
   question or justify skipping one, even for the same customer and project name.
5. Ask only the missing questions that change architecture, risk or cost; confirm what
   the customer already said instead of re-asking it.
6. Conclusions separate verified facts, methodology, customer input and assumptions
   pending validation. Never invent citations, prices, resource ids, ARNs, performance
   numbers or AWS capabilities.
7. Explain why option A was chosen over option B, and cover quality, reliability,
   security, compliance, cost, observability and continuous evaluation together.
8. A formal design includes an implementable evaluator registry and maps every golden
   test to concrete evaluators, levels, thresholds and blocking conditions. High-risk
   facts, authorization, tool parameters, write operations and idempotency use
   code-based evaluators, never only LLM-as-a-judge.
9. You advise; you never create, change or delete AWS resources, never run
   deployments, and never present an unexecuted plan as done.
10. Deliverables are structured Markdown in the reply (Mermaid for diagrams). Do not
    promise office documents, image exports or download links you did not create.
11. Keep secrets, credentials and account identifiers out of your output; refer to
    them by role or placeholder.
"""

ARCHITECT = SystemPreset(
    key="aws-agent-solution-architect",
    name="aws-agent-solution-architect",
    label="AWS Agent Solution Architect",
    description=(
        "Platform-managed advisory Harness that turns an AI-agent business requirement "
        "into an evaluation-first, production-grade AWS design. Verifies AWS facts through "
        "the public AWS Knowledge MCP server; loads its methodology from a versioned S3 "
        "skill bundle."
    ),
    skill_version="1.0.0",
    system_prompt=ARCHITECT_SYSTEM_PROMPT,
    allowed_tools=("file_*", f"@{AWS_KNOWLEDGE_TOOL_NAME}"),
)

PRESETS: dict[str, SystemPreset] = {ARCHITECT.key: ARCHITECT}
RESERVED_AGENT_NAMES: frozenset[str] = frozenset(p.name for p in PRESETS.values())


def get_preset(key: str) -> SystemPreset | None:
    return PRESETS.get(key)


def is_reserved_name(name: str) -> bool:
    return name in RESERVED_AGENT_NAMES


@dataclass(frozen=True)
class InstallOptions:
    """The administrator's degrees of freedom. Everything not here is fixed."""

    model_id: str = DEFAULT_MODEL_ID
    model_source: str = "bedrock"
    knowledge_bases: tuple[KnowledgeBaseRef, ...] = field(default_factory=tuple)


def build_spec(
    preset: SystemPreset, bucket: str, options: InstallOptions, *, digest: str | None = None
) -> AgentSpec:
    """The server-owned spec for one preset in one workspace.

    Memory: explicitly **disabled** (``{"disabled": {}}`` on the harness request). The
    boolean flags cannot express "short-term only" against the real API — the shared
    workspace memory carries long-term strategies, and the harness-managed default
    creates one with SEMANTIC + SUMMARIZATION — so the preset opts out of persistent
    memory altogether: a new session's requirement baseline is independent of every
    earlier one by construction, and the execution role gets no memory grant. The
    conversation inside one runtime session lives in the harness session itself
    (memory persists context *across* sessions per the service model); that
    within-session continuity is part of the pending live smoke. Skills: the
    versioned S3 prefix, so the role's ``SkillBundle*`` statements scope to exactly
    this version.
    """
    return AgentSpec(
        name=preset.name,
        method="harness",
        model_id=options.model_id,
        model_source=options.model_source,  # type: ignore[arg-type]
        system_prompt=preset.system_prompt,
        tools=[
            ToolRef(
                type="mcp",
                name=AWS_KNOWLEDGE_TOOL_NAME,
                config={"url": AWS_KNOWLEDGE_MCP_URL, "auth": "none"},
            )
        ],
        skills=[preset.skill_uri(bucket, digest)],
        allowed_tools=list(preset.allowed_tools),
        memory={"short_term": False, "long_term": False, "memory_id": None},
        knowledge_bases=list(options.knowledge_bases),
        max_iterations=preset.max_iterations,
        timeout_seconds=preset.timeout_seconds,
    )


def options_from_spec(spec: dict) -> InstallOptions:
    """Recover the administrator's choices from a stored preset spec (repair path)."""
    return InstallOptions(
        model_id=str(spec.get("model_id") or DEFAULT_MODEL_ID),
        model_source=str(spec.get("model_source") or "bedrock"),
        knowledge_bases=tuple(
            KnowledgeBaseRef(**kb) for kb in (spec.get("knowledge_bases") or [])
        ),
    )


def skill_release_from_spec(spec: dict) -> tuple[str, str | None] | None:
    """``(version, digest12 | None)`` of the release a stored preset spec points at
    (``…/<version>/`` or the content-addressed ``…/<version>-<digest12>/``)."""
    for path in spec.get("skills") or []:
        marker = f"/{SYSTEM_SKILLS_PREFIX}/"
        if marker in path:
            leaf = path.rstrip("/").rsplit("/", 1)[-1]
            version, sep, digest = leaf.rpartition("-")
            if sep and len(digest) == 12 and all(c in "0123456789abcdef" for c in digest):
                return version, digest
            return leaf, None
    return None


def skill_version_from_spec(spec: dict) -> str | None:
    """The bundle version a stored preset spec points at."""
    release = skill_release_from_spec(spec)
    return release[0] if release else None


def skill_prefix_from_spec(spec: dict) -> str | None:
    """The S3 key prefix (no bucket) the stored spec's Harness actually loads from."""
    for path in spec.get("skills") or []:
        marker = f"/{SYSTEM_SKILLS_PREFIX}/"
        if marker in path and path.startswith("s3://"):
            return path.split("/", 3)[3]
    return None


def bundle_digest(skill_dir: Path) -> tuple[str, list[str]]:
    """Deterministic sha256 over the bundle's sorted relative paths + bytes.

    Reported in the package stage detail so a reviewer can tie a deployed harness back
    to the exact repository revision of the bundle; not a security boundary.
    """
    files = sorted(
        p.relative_to(skill_dir).as_posix() for p in skill_dir.rglob("*") if p.is_file()
    )
    h = sha256()
    for rel in files:
        h.update(rel.encode())
        h.update(b"\0")
        h.update((skill_dir / rel).read_bytes())
        h.update(b"\0")
    return h.hexdigest(), files


@dataclass(frozen=True)
class BundleSnapshot:
    """One immutable in-memory copy of the bundle: the bytes that were validated are
    the bytes that are hashed are the bytes that are uploaded. A mutable checkout
    re-read between those steps can never split the identity."""

    version: str
    digest: str
    files: dict[str, bytes]  # POSIX-relative path → content, sorted keys

    def file_digests(self) -> dict[str, str]:
        return {rel: sha256(body).hexdigest() for rel, body in self.files.items()}

    def release(self) -> dict:
        """What an install pins onto its job and what the manifest records."""
        return {
            "version": self.version,
            "digest": self.digest,
            "files": self.file_digests(),
        }


def _digest_of(files: dict[str, bytes]) -> str:
    h = sha256()
    for rel in sorted(files):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(files[rel])
        h.update(b"\0")
    return h.hexdigest()


def snapshot_bundle(preset: SystemPreset) -> BundleSnapshot:
    """Read the repository bundle ONCE into memory, validate that copy, hash it.

    Validation runs against a temporary directory materialized from the snapshot
    (never against the live checkout), with the same ``validate_bundle`` every member
    skill passes, plus the preset's own invariants: SKILL.md ``version`` equals the
    catalogue's ``skill_version`` (a content change cannot ship under a stale S3
    version directory) and the SKILL.md name equals the reserved agent name. Every
    path is a plain relative POSIX path, so the upload can only land under the
    versioned prefix. Total size is bounded by the shared skill bundle cap.
    """
    root = preset.skill_path()
    files: dict[str, bytes] = {}
    total = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = PurePosixPath(path.relative_to(root).as_posix())
        if rel.is_absolute() or any(part in ("..", "") for part in rel.parts):
            raise ValueError(f"preset '{preset.key}': unsafe bundle path {rel!s}")
        body = path.read_bytes()
        total += len(body)
        if total > SKILL_BUNDLE_MAX_BYTES:
            raise ValueError(
                f"preset '{preset.key}': bundle exceeds {SKILL_BUNDLE_MAX_BYTES} bytes"
            )
        files[str(rel)] = body
    if "SKILL.md" not in files:
        raise ValueError(f"preset '{preset.key}': bundle has no SKILL.md")
    with TemporaryDirectory(prefix="preset-snapshot-") as tmp:
        base = Path(tmp)
        for rel, body in files.items():
            target = base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        bundle = bundle_from_dir(base)
        validate_bundle(bundle)
        front = parse_frontmatter(files["SKILL.md"].decode("utf-8"))
        if str(front.get("version", "")).strip() != preset.skill_version:
            raise ValueError(
                f"preset '{preset.key}': SKILL.md version {front.get('version')!r} != "
                f"preset skill_version {preset.skill_version!r} — bump both together"
            )
        if bundle.name != preset.name:
            raise ValueError(
                f"preset '{preset.key}': SKILL.md name {bundle.name!r} != {preset.name!r}"
            )
    return BundleSnapshot(
        version=preset.skill_version, digest=_digest_of(files), files=dict(sorted(files.items()))
    )


def bundle_release(preset: SystemPreset) -> dict:
    """The release an install pins onto its job (``{version, digest, files{rel: sha}}``)."""
    return snapshot_bundle(preset).release()


def load_bundle(preset: SystemPreset):
    """Open + validate the repository-side bundle (caller closes it). Kept for
    callers that want the ``SkillBundle`` view; publication uses ``snapshot_bundle``."""
    snapshot_bundle(preset)  # same invariants, raised the same way
    return bundle_from_dir(preset.skill_path())
