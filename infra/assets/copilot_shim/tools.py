import importlib.util
import inspect
import logging
import os
import sys
from typing import Callable, List

from copilot import define_tool


def discover_tools() -> List[Callable]:
    """
    Dynamically discover and load tools from the `tools` folder.
    """
    tools: List[Callable] = []
    project_src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tools_dir = os.path.join(project_src_dir, "tools")

    # Add tools dir to sys.path so tool modules can import shared helpers
    # (e.g. _patterns.py, _utils.py — files prefixed with _ that are skipped
    # during tool registration but may be imported by tool modules)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    print(f"[Tool Discovery] Looking for tools in: {tools_dir}")
    print(f"[Tool Discovery] Directory exists: {os.path.exists(tools_dir)}")

    if not os.path.exists(tools_dir):
        print(f"[Tool Discovery] WARNING: Tools directory not found: {tools_dir}")
        return tools

    files = [f for f in os.listdir(tools_dir) if f.endswith(".py") and not f.startswith("_")]
    print(f"[Tool Discovery] Python files found: {files}")

    registered_names: set[str] = set()

    for filename in files:
        filepath = os.path.join(tools_dir, filename)
        module_name = filename[:-3]
        print(f"[Tool Discovery] Loading module: {module_name} from {filepath}")
        try:
            spec = importlib.util.spec_from_file_location(module_name, filepath)
            if spec is None or spec.loader is None:
                print(f"[Tool Discovery] ERROR: Could not create spec for {filename}")
                continue

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            members = inspect.getmembers(module, inspect.isfunction)
            local_functions = [
                (name, obj)
                for name, obj in members
                if obj.__module__ == module_name and not name.startswith("_")
            ]
            print(f"[Tool Discovery] Local functions in {filename}: {[m[0] for m in local_functions]}")

            for name, obj in local_functions:
                description = (obj.__doc__ or f"Tool: {name}").strip()
                # Copilot tool names must be unique across the whole session.
                # Some environments may inject built-in tools (e.g. web_fetch), so
                # avoid known collisions while keeping canonical names when safe.
                tool_name = name
                # Avoid known collisions with platform/built-in tools.
                if tool_name in registered_names or tool_name in {"web_fetch", "fetch"}:
                    tool_name = f"local_{tool_name}"
                tools.append(define_tool(name=tool_name, description=description)(obj))
                print(f"[Tool Discovery] Loaded: {name}")
                print(f"[Tool Discovery]   Registered name: {tool_name}")
                print(f"[Tool Discovery]   Description: {description}")
                registered_names.add(tool_name)
                break
        except Exception as e:
            import traceback

            print(f"[Tool Discovery] ERROR loading {filename}: {e}")
            traceback.print_exc()
            logging.error(f"Failed to load tool from {filename}: {e}")

    return tools


_REGISTERED_TOOLS_CACHE = discover_tools()
