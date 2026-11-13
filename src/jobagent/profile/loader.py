"""Save and load MasterProfile as YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from jobagent.profile.schema import MasterProfile


def save_profile(profile: MasterProfile, path: Path | str | None = None) -> Path:
    """Serialise *profile* to YAML and write to *path*.

    Defaults to data/profile/master_profile.yaml.
    """
    if path is None:
        from jobagent.config import settings  # noqa: PLC0415
        path = settings.profiles_dir / "master_profile.yaml"

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # model_dump with mode="json" gives JSON-safe types (no datetime objects)
    data = profile.model_dump(mode="json", exclude_none=False)

    with dest.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)

    return dest


def load_profile(path: Path | str | None = None) -> MasterProfile:
    """Load a previously saved master_profile.yaml."""
    if path is None:
        from jobagent.config import settings  # noqa: PLC0415
        path = settings.profiles_dir / "master_profile.yaml"

    dest = Path(path)
    if not dest.exists():
        raise FileNotFoundError(
            f"master_profile.yaml not found at {dest}. "
            "Run `jobagent profile import` first."
        )

    with dest.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    return MasterProfile.model_validate(data)
