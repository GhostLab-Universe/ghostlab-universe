"""Versioned registry of reviewed skills (root ``skills/``) and prompts (``prompts/``).

Files are data. Loading validates each skill against the M00 ``SkillSpec``
contract and records a SHA-256 digest of its LF-normalized bytes, so a changed
procedure or prompt is visible as a new digest. A skill never grants authority:
it can only be used within a caller scope that already allows its tools.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ghostlab.contracts import BY_NAME, SkillRef, SkillSpec, TrustedScope

ROOT = Path(__file__).resolve().parents[3]
MAX_FILE_BYTES = 64_000


class SkillError(Exception):
    pass


class SkillPermissionError(SkillError):
    """The skill asks for tools the caller's trusted scope does not allow."""


def _read_normalized(path: Path) -> bytes:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise SkillError(f"{path.name} exceeds {MAX_FILE_BYTES} bytes")
    return path.read_bytes().replace(b"\r\n", b"\n")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class PromptVersion:
    prompt_version: str
    text: str
    sha256: str


@dataclass(frozen=True)
class RegisteredSkill:
    spec: SkillSpec
    sha256: str
    source: str  # repository-relative path

    @property
    def ref(self) -> SkillRef:
        return SkillRef(skill_id=self.spec.skill_id, version=self.spec.version)

    @property
    def output_contract_registered(self) -> bool:
        """False until M00 publishes the output schema this skill requests."""
        contract = BY_NAME.get(self.spec.output_schema.name)
        return contract is not None and contract.version == self.spec.output_schema.version


@dataclass(frozen=True)
class SkillInvocation:
    """What every context compilation records about the procedure it used."""

    skill_id: str
    skill_version: int
    skill_sha256: str
    prompt_version: str
    prompt_sha256: str


class PromptRegistry:
    def __init__(self, prompts: dict[str, PromptVersion]):
        self._prompts = dict(prompts)

    @classmethod
    def load(cls, directory: Path = ROOT / "prompts") -> "PromptRegistry":
        prompts = {}
        for path in sorted(directory.glob("*.md")):
            data = _read_normalized(path)
            text = data.decode("utf-8").strip()
            if not text:
                raise SkillError(f"{path.name} is empty")
            prompts[path.stem] = PromptVersion(path.stem, text, _sha256(data))
        return cls(prompts)

    def get(self, prompt_version: str) -> PromptVersion:
        try:
            return self._prompts[prompt_version]
        except KeyError:
            raise SkillError(f"Unknown prompt version {prompt_version!r}") from None

    def versions(self) -> list[str]:
        return sorted(self._prompts)


class SkillRegistry:
    def __init__(self, skills: list[RegisteredSkill], prompts: PromptRegistry):
        self._skills: dict[tuple[str, int], RegisteredSkill] = {}
        self.prompts = prompts
        for skill in skills:
            key = (skill.spec.skill_id, skill.spec.version)
            if key in self._skills:
                raise SkillError(f"Duplicate skill {key[0]} v{key[1]}")
            prompts.get(skill.spec.prompt_version)  # every skill names a real prompt
            self._skills[key] = skill

    @classmethod
    def load(cls, skills_dir: Path = ROOT / "skills",
             prompts_dir: Path = ROOT / "prompts") -> "SkillRegistry":
        prompts = PromptRegistry.load(prompts_dir)
        skills = []
        for path in sorted(skills_dir.glob("*.json")):
            data = _read_normalized(path)
            try:
                spec = SkillSpec.model_validate_json(data)
            except ValueError as error:
                raise SkillError(f"{path.name} is not a valid SkillSpec: {error}") from None
            expected = f"{spec.skill_id}.v{spec.version}.json"
            if path.name != expected:
                raise SkillError(f"{path.name} must be named {expected}")
            try:
                source = path.resolve().relative_to(ROOT).as_posix()
            except ValueError:
                source = path.name
            skills.append(RegisteredSkill(spec, _sha256(data), source))
        return cls(skills, prompts)

    def get(self, skill_id: str, version: int | None = None) -> RegisteredSkill:
        if version is None:
            versions = [v for (sid, v) in self._skills if sid == skill_id]
            if not versions:
                raise SkillError(f"Unknown skill {skill_id!r}")
            version = max(versions)
        try:
            return self._skills[(skill_id, version)]
        except KeyError:
            raise SkillError(f"Unknown skill {skill_id!r} v{version}") from None

    def all(self) -> list[RegisteredSkill]:
        return [self._skills[key] for key in sorted(self._skills)]

    def authorize(self, skill: RegisteredSkill, scope: TrustedScope) -> SkillInvocation:
        """Bind a skill to a controller-issued scope. Never widens the scope."""
        missing = sorted(set(skill.spec.allowed_tools) - set(scope.allowed_tools))
        if missing:
            raise SkillPermissionError(
                f"Skill {skill.spec.skill_id} v{skill.spec.version} needs tools outside "
                f"the {scope.role} scope: {missing}")
        prompt = self.prompts.get(skill.spec.prompt_version)
        return SkillInvocation(skill.spec.skill_id, skill.spec.version, skill.sha256,
                               prompt.prompt_version, prompt.sha256)
