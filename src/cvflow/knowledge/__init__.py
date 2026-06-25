"""Knowledge-base loader — reads the user's profile into LLM context.

The profile is the single source of truth about the user. Loading reads every
``profile/**/*.md`` (skipping ``*.example.md`` templates and ``README.md``) plus
``form_fields.json``. Form fields left empty surface as a pending list rather than being
guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cvflow.storage import FormFields


@dataclass(frozen=True)
class KnowledgeBase:
    documents: dict[str, str]
    form_fields: FormFields

    @classmethod
    def load(
        cls, profile_dir: str | Path, form_fields_path: str | Path | None = None
    ) -> KnowledgeBase:
        profile_dir = Path(profile_dir)
        documents: dict[str, str] = {}
        for md in sorted(profile_dir.rglob("*.md")):
            if md.name.endswith(".example.md") or md.name == "README.md":
                continue
            key = md.relative_to(profile_dir).with_suffix("").as_posix()
            documents[key] = md.read_text()

        ff_path = Path(form_fields_path) if form_fields_path else profile_dir / "form_fields.json"
        return cls(documents=documents, form_fields=FormFields.load(ff_path))

    def _doc(self, key: str) -> str:
        if key not in self.documents:
            raise KeyError(f"knowledge base has no document: {key}")
        return self.documents[key]

    @property
    def skills(self) -> str:
        return self._doc("skills")

    @property
    def experience(self) -> str:
        return self._doc("experience")

    @property
    def education(self) -> str:
        return self._doc("education")

    @property
    def personality(self) -> str:
        return self._doc("personality")

    def project_docs(self) -> dict[str, str]:
        return {k: v for k, v in self.documents.items() if k.startswith("projects/")}

    def doc_keys(self) -> list[str]:
        """Return the profile document keys (relative stems)."""
        return list(self.documents)

    def missing_form_fields(self) -> list[str]:
        return self.form_fields.missing()

    def full_context(self) -> str:
        return "\n\n".join(f"# {key}\n\n{text}" for key, text in self.documents.items())
