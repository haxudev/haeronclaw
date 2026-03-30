import logging
import os
import platform
import shutil


def get_copilot_cli_path() -> str:
    """
    Get the path to the Copilot CLI executable.

    Priority:
    1. COPILOT_CLI_PATH environment variable
    2. Bundled npm package (current platform-specific binary only)
    3. System PATH (via `which copilot`) - fallback for local development
    4. Default 'copilot' (assumes it's in PATH)
    """
    env_path = os.environ.get("COPILOT_CLI_PATH")
    if env_path:
        logging.info(f"Using COPILOT_CLI_PATH: {env_path}")
        return env_path

    function_app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    system = platform.system().lower()
    machine = platform.machine().lower()

    platform_map = {
        ("darwin", "arm64"): "copilot-darwin-arm64",
        ("darwin", "x86_64"): "copilot-darwin-x64",
        ("linux", "x86_64"): "copilot-linux-x64",
        ("linux", "aarch64"): "copilot-linux-arm64",
        ("windows", "amd64"): "copilot-win32-x64",
        ("windows", "x86_64"): "copilot-win32-x64",
    }
    platform_pkg = platform_map.get((system, machine))

    if platform_pkg:
        ext = ".exe" if system == "windows" else ""
        binary_path = os.path.join(
            function_app_dir, "node_modules", "@github", platform_pkg, f"copilot{ext}"
        )
        if os.path.exists(binary_path):
            logging.info(f"Using bundled Copilot CLI for {system}/{machine}: {binary_path}")
            return binary_path
        logging.debug(f"Platform binary not found: {binary_path}")

    bundled_cli = os.path.join(function_app_dir, "node_modules", "@github", "copilot", "index.js")
    if os.path.exists(bundled_cli):
        logging.info(f"Using bundled Copilot CLI (Node.js): {bundled_cli}")
        return bundled_cli

    system_copilot = shutil.which("copilot")
    if system_copilot:
        logging.info(f"Using copilot from system PATH: {system_copilot}")
        return system_copilot

    logging.warning("No copilot CLI found, falling back to 'copilot' command")
    return "copilot"
