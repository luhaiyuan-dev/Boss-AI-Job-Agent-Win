"""通过最小 CDP/WebSocket 接管 Edge 的 Boss直聘 Web 浏览器控制层。"""

from .driver import (
    BrowserError,
    EdgeBrowser,
    EdgeDebugTarget,
    ElementNotFoundError,
    LoginRequiredError,
    boss_edge_user_data_dir,
    default_edge_user_data_dir,
    discover_edge_debug_targets,
)

__all__ = [
    "BrowserError",
    "EdgeBrowser",
    "EdgeDebugTarget",
    "ElementNotFoundError",
    "LoginRequiredError",
    "boss_edge_user_data_dir",
    "default_edge_user_data_dir",
    "discover_edge_debug_targets",
]
