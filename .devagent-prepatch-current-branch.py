from pathlib import Path


def replace_once(path_name: str, old: str, new: str) -> None:
    path = Path(path_name)
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise RuntimeError(f"{path_name} prepatch expected one match, found {text.count(old)}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "README.md",
    "- one commit is created and pushed to the selected remote,",
    "- one commit is created and pushed to the selected remote branch,",
)
replace_once(
    "CONTRIBUTING.md",
    "Branch-publishing tests should use local bare Git repositories where possible and must prove that protected/non-VERIFIED publication attempts are rejected. CLI tests should also prove that the engineering report is emitted before any commit/push action.",
    "Branch-publishing tests should use local bare Git repositories where possible and must prove that protected/non-`VERIFIED` publication attempts are rejected. CLI tests should also prove that the engineering report is emitted before any commit/push action.",
)
print("Continuation patch inputs normalized.")
