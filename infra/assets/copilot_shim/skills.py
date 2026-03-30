import os
from typing import Optional


def resolve_session_directory_for_skills() -> Optional[str]:
    """
    Resolve a session directory that contains common skills locations.
    """
    cwd = os.getcwd()
    function_app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_session_dir = os.environ.get("COPILOT_SESSION_DIRECTORY")
    if env_session_dir:
        resolved = os.path.expanduser(env_session_dir)
        if os.path.isdir(resolved):
            return resolved

    candidate_roots = [
        cwd,
        function_app_dir,
        os.path.join(cwd, ".codex"),
        os.path.join(cwd, ".claudeCode"),
        os.path.join(cwd, ".github"),
        os.path.join(cwd, ".vscode"),
    ]
    skill_dir_names = ("skills", "Skills")

    for root in candidate_roots:
        if not os.path.isdir(root):
            continue
        for name in skill_dir_names:
            if os.path.isdir(os.path.join(root, name)):
                return root

    return None
