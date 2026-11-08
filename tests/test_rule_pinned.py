"""Everything CI and the image run is pinned (AGENTS.md, Docker image; #579).

Each action a workflow or a composite action of this repo uses is pinned to a full commit SHA with its version in
a comment (a `docker://` one to a digest), each container image a workflow runs (`image:`, `container:`) to a
digest, and each image the Dockerfile builds on (`FROM`, `COPY --from=`) to a tag and a digest, so what runs
changes only through a reviewed PR (Dependabot proposes the bumps). No exception: a new action or image is pinned
the same way, with the SHA its release tag points to.
"""

import re

from helpers.workflows import LOCAL_ACTIONS, ROOT, WORKFLOWS

_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
_PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
_VERSION_COMMENT = re.compile(r"^\s+#\s*v?\d")
# `image: x`, or the short form `container: x` (the long form's `image:` is matched on its own line).
_IMAGE = re.compile(r"^\s*(?:-\s+)?(?:image|container):\s*(?P<ref>[^\s#]+)")
_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def unpinned_actions(text: str) -> list[tuple[int, str]]:
    """The (line, ref) of each `uses:` that is not a local action nor a SHA with a version comment."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        match = _USES.match(line)
        if match is None or match["ref"].startswith("./"):
            continue
        if match["ref"].startswith("docker://"):
            if not _DIGEST.search(match["ref"]):
                found.append((number, match["ref"]))
            continue
        if not (_PINNED_ACTION.match(match["ref"]) and _VERSION_COMMENT.match(match["rest"])):
            found.append((number, match["ref"]))
    return found


def unpinned_images(text: str) -> list[tuple[int, str]]:
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        match = _IMAGE.match(line)
        if match is not None and not _DIGEST.search(match["ref"]):
            found.append((number, match["ref"]))
    return found


def _pinned_by_tag_and_digest(image: str) -> bool:
    name = image.split("@", 1)[0]
    return bool(_DIGEST.search(image)) and ":" in name.rsplit("/", 1)[-1]


def unpinned_bases(dockerfile: str) -> list[tuple[int, str]]:
    """The (line, image) of each FROM or COPY --from= that is neither `name:tag@sha256:<digest>` nor a stage."""
    stages: set[str] = set()
    found = []
    for number, line in enumerate(dockerfile.splitlines(), 1):
        words = line.split()
        if not words:
            continue
        if words[0].upper() == "FROM":
            args = [word for word in words[1:] if not word.startswith("--")]
            image = args[0]
            if len(args) >= 3 and args[1].upper() == "AS":
                stages.add(args[2])
        elif words[0].upper() == "COPY":
            sources = [word.split("=", 1)[1] for word in words[1:] if word.startswith("--from=")]
            # A stage, by name or index, or an image.
            if not sources or sources[0] in stages or sources[0].isdigit():
                continue
            image = sources[0]
        else:
            continue
        if image not in stages and not _pinned_by_tag_and_digest(image):
            found.append((number, image))
    return found


def test_every_action_is_pinned_to_a_commit_with_its_version() -> None:
    files = [*WORKFLOWS, *LOCAL_ACTIONS]
    assert files, "no workflow found under .github/"
    found = [
        f"{path.relative_to(ROOT)}:{line}: {ref}"
        for path in files
        for line, ref in unpinned_actions(path.read_text(encoding="utf-8"))
    ]
    assert found == [], (
        "pin each action to the full commit SHA its release tag points to, with the version in a comment "
        "(`uses: owner/action@<40-hex sha> # v1.2.3`):\n" + "\n".join(found)
    )


def test_every_container_image_of_a_workflow_is_pinned_to_a_digest() -> None:
    found = [
        f"{path.relative_to(ROOT)}:{line}: {ref}"
        for path in WORKFLOWS
        for line, ref in unpinned_images(path.read_text(encoding="utf-8"))
    ]
    assert found == [], "pin each image to its digest (`image: name:tag@sha256:<digest>`):\n" + "\n".join(found)


def test_every_base_image_of_the_dockerfile_is_pinned_to_a_tag_and_a_digest() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^FROM\s", dockerfile, re.MULTILINE), "the Dockerfile has no FROM line"
    found = [f"Dockerfile:{line}: {image}" for line, image in unpinned_bases(dockerfile)]
    assert found == [], "pin each base image by tag and digest (`FROM name:tag@sha256:<digest>`):\n" + "\n".join(found)


def test_the_checks_find_what_is_not_pinned() -> None:
    sha = "3d3c42e5aac5ba805825da76410c181273ba90b1"
    workflow = (
        f"      - uses: actions/checkout@{sha} # v7.0.1\n"
        "      - uses: actions/checkout@v7\n"
        f"        uses: actions/setup-python@{sha}\n"
        "      - uses: ./.github/actions/scan-image\n"
        "        image: registry:3\n"
        "    container: node:20\n"
        "      - uses: docker://alpine:3\n"
        f"      - uses: docker://alpine:3@sha256:{'0' * 64}\n"
    )
    assert unpinned_actions(workflow) == [
        (2, "actions/checkout@v7"),
        (3, f"actions/setup-python@{sha}"),
        (7, "docker://alpine:3"),
    ]
    assert unpinned_images(workflow) == [(5, "registry:3"), (6, "node:20")]
    digest = "@sha256:" + "0" * 64
    dockerfile = (
        f"FROM python:3.13{digest} AS builder\nFROM builder\nFROM python{digest}\nFROM python:3.13\n"
        "COPY --from=builder /a /b\nCOPY --from=0 /a /b\nCOPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/\n"
    )
    assert unpinned_bases(dockerfile) == [
        (3, f"python{digest}"),
        (4, "python:3.13"),
        (7, "ghcr.io/astral-sh/uv:latest"),
    ]
