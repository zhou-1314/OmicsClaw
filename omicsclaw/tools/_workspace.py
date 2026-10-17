"""Resolve file-tool paths against one workspace and reject credential paths.

Workspace.resolve joins relative paths to the workspace, follows symlinks,
checks sensitive components, then checks containment by path component.
Missing leaves are allowed for writers; callers decide whether a file must
exist. A credential path remains forbidden inside the workspace. Refusals
are typed so callers can distinguish sensitivity from containment failures.

Credential matching is independent of HOME. The resolver does not search
alternate roots or redirect an escaped destination to a default directory.
Its checks concern file-tool paths; shell isolation belongs to the deployment.

Historical migration notes and reference comparisons are preserved in
docs/engineering/runtime-tool-design-notes.md, under this module's path.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

WORKSPACE_KEY = "workspace"
"""The :class:`~omicsclaw.tools.context.ToolContext` value naming the root.

Spelled as a constant because a typo read through
:func:`~omicsclaw.tools.context.context_value` yields ``None``, which is
indistinguishable from a surface that bound nothing — the tool then
reports "no workspace is bound", which reads like a surface bug.

One session has one workspace, so two tools reading two different keys
would be two workspaces wearing one name. Plan 0028's ``save_gene_panel``
spelled the string a second time and the duplication was pinned by test
until a later step could collapse it; that step arrived by deletion, and
the foundation tools import this one.
``test_the_workspace_key_is_spelled_once_and_the_tools_take_it_from_here``
now pins both the literal and the identity.
"""

CREDENTIAL_PATHS: tuple[tuple[str, ...], ...] = (
    (".ssh",),
    (".aws",),
    (".kube",),
    (".gnupg",),
    (".netrc",),
    (".config", "gcloud"),
    (".config", "gh"),
)
"""Component runs that make a path a credential path, wherever it sits.

Each entry is matched as **consecutive path components**, so ``.ssh``
catches ``/home/bob/.ssh/id_rsa`` and ``/srv/backup/.ssh`` alike, while
``(".config", "gcloud")`` catches ``~/.config/gcloud`` without also
refusing every directory in the world named ``gcloud``. Components, never
substrings: a project file called ``sshconfig.md`` is not a credential,
and a rule that thought it was would be a rule people route around.

The first six are harness9's list (``safe_path.go:31-38``), kept because
the machines this runs on are developer workstations. ``.config/gh`` is
added: a GitHub token is push access to this repository.
"""

SECRET_FILENAMES: tuple[str, ...] = (".env", ".env.*", ".envrc")
"""Final components that hold this project's own secrets.

Matched against the file name only — a *directory* named ``.env`` is the
virtualenv convention and refusing everything under one would cost real
reads for no security gain, since the secrets live in a file.

Plan 0029 §4 Q8 proposes the glob ``.env*``. These three patterns are
narrower on purpose: ``.env*`` also swallows ``.envelope-design.md``,
which is a refusal a user cannot understand and has nothing to do with
credentials. The three cover the conventions that actually carry secrets
— ``.env``, per-environment ``.env.production``, and direnv's ``.envrc``,
which is a shell script and routinely holds exported keys.
"""


class PathRefused(ValueError):
    """This path will not be used, and the message says why.

    A :exc:`ValueError` because the value is the problem. Subclassed
    twice rather than raised bare so that "the model wandered outside its
    workspace" and "something tried to open a private key" are separable
    downstream: they are the same answer to the model and completely
    different events for whoever is watching the machine.
    """


class PathEscapesWorkspace(PathRefused):
    """The path resolved to somewhere outside the session's workspace.

    Ordinarily a model mistake and correctable by sending a different
    path, which is why the message names the workspace root.
    """


class PathIsSensitive(PathRefused):
    """The path names credentials, and no workspace setting permits it.

    The message deliberately does **not** echo where the path resolved
    to. A model that reached a key through a symlink learns only that it
    was refused; confirming the key's location would be a courtesy to the
    one caller who should not receive it.
    """


def _tail_matches(parts: tuple[str, ...], marker: tuple[str, ...]) -> bool:
    """Do ``marker``'s components appear consecutively inside ``parts``?"""
    width = len(marker)
    return any(
        parts[index : index + width] == marker
        for index in range(len(parts) - width + 1)
    )


def is_sensitive(path: Path) -> bool:
    """Is this a credential path, regardless of any workspace?

    Call it on a **resolved** path (plan 0029 trap 8). Called on the name
    the model sent, ``<workspace>/link-to-ssh`` is an ordinary-looking
    file name and the rule never fires — which is the whole of the bypass
    this ordering exists to close. :meth:`Workspace.resolve` is the
    caller that gets this right; it is exposed separately so a future
    ``bash`` pre-flight check can reuse the same list rather than growing
    a second one.
    """
    if any(fnmatch.fnmatchcase(path.name, name) for name in SECRET_FILENAMES):
        return True
    return any(_tail_matches(path.parts, marker) for marker in CREDENTIAL_PATHS)


def _is_within(candidate: Path, root: Path) -> bool:
    """Is ``candidate`` the root itself or something under it?

    :meth:`Path.is_relative_to` compares components, which is what makes
    ``/project-evil`` not a child of ``/project``. Written as its own
    function so the rule has one line to point a mutation at: replace it
    with ``str(candidate).startswith(str(root))`` and the named test goes
    red, which is the only evidence that the component comparison is
    doing anything.
    """
    return candidate.is_relative_to(root)


@dataclass(frozen=True, slots=True)
class Workspace:
    """One session's filesystem boundary, and the only way through it.

    Constructed by whatever binds :data:`WORKSPACE_KEY` — a surface, a
    test — and handed to each tool. Frozen because a boundary that a tool
    can widen at runtime is not a boundary; the root is normalised once,
    in :meth:`__post_init__`, so every later comparison is against an
    already-resolved path.

    The root is expanded and resolved while model-supplied paths are only
    resolved, and the asymmetry is the point: ``~`` in an operator's
    configuration means the operator's home, while ``~`` in a payload the
    model wrote is just a character it chose. The second is treated as an
    ordinary directory name — which lands it inside the workspace, where
    it is harmless — rather than as a way to name ``$HOME``.
    """

    root: Path

    def __post_init__(self) -> None:
        """Normalise the root, and refuse to sit on top of credentials.

        Resolving the root matters more than it looks: temporary
        directories are routinely reached through symlinks (``/var`` →
        ``/private/var`` on macOS, ``/tmp`` wherever an operator has
        linked it), and comparing a resolved candidate against an
        unresolved root would reject every path in such a workspace.

        A root that is itself sensitive would make every call fail with a
        confusing per-path refusal, so it fails here instead, once, where
        the mistake was actually made.
        """
        resolved = Path(self.root).expanduser().resolve()
        if is_sensitive(resolved):
            raise PathIsSensitive(
                f"{self.root} cannot be used as a workspace: it is inside a "
                "location this agent never reads"
            )
        object.__setattr__(self, "root", resolved)

    def resolve(self, path: str | Path) -> Path:
        """The real file ``path`` names, or a refusal saying why not.

        ``path`` is **untrusted input** — whatever the model put in its
        arguments. Relative paths are joined to :attr:`root`; absolute
        ones are judged as given and are accepted only when they land
        inside it, which is how a model that has been told its workspace
        path may use it. The empty string and ``"."`` both name the root,
        matching ``safePath("/project", "")``.

        Returns an absolute, symlink-free :class:`~pathlib.Path`. It is
        not guaranteed to exist — ``write`` needs exactly that — so a
        caller that requires existence checks for it and says so in its
        own words.
        """
        try:
            resolved = (self.root / Path(path)).resolve()
        except (OSError, ValueError) as exc:
            # A NUL byte, a name longer than the filesystem allows, a
            # symlink chain the kernel gives up on. None of these can be
            # proved safe, so none of them are used. Refusing beats
            # letting an OSError surface from whichever tool happened to
            # open the file, where it reads as an I/O failure rather than
            # as a rejected path.
            raise PathRefused(f"{str(path)!r} is not a usable path: {exc}") from exc

        if is_sensitive(resolved):
            raise PathIsSensitive(
                f"{str(path)!r} is refused: it names credentials or "
                "environment secrets, which this agent does not read or "
                "write under any workspace setting"
            )

        if not _is_within(resolved, self.root):
            raise PathEscapesWorkspace(
                f"{str(path)!r} resolves to {resolved}, which is outside "
                f"this session's workspace {self.root}; send a path inside "
                "the workspace"
            )

        return resolved


__all__ = [
    "CREDENTIAL_PATHS",
    "PathEscapesWorkspace",
    "PathIsSensitive",
    "PathRefused",
    "SECRET_FILENAMES",
    "WORKSPACE_KEY",
    "Workspace",
    "is_sensitive",
]
