# Shared tagging conventions and tiny helpers

from typing import Optional

DEFAULT_TAGS = {
    "CreatedBy": "project-cli",
}

def build_tag_list(owner: str, project: Optional[str] = None, env: Optional[str] = None):
    """Return AWS TagSpecifications list-of-dicts format."""
    tags = [
        {"Key": "CreatedBy", "Value": DEFAULT_TAGS["CreatedBy"]},
        {"Key": "Owner", "Value": owner},
    ]
    if project:
        tags.append({"Key": "Project", "Value": project})
    if env:
        tags.append({"Key": "Environment", "Value": env})
    return tags