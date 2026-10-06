from .governance import router as governance_router
from .governance_project_merge import router as governance_project_merge_router
from .governance_schedule import router as governance_schedule_router
from .admin import router as admin_router
from .admin_autodedup import router as admin_autodedup_router
from .admin_write_queue import router as admin_write_queue_router
from .agent_tokens import router as agent_tokens_router
from .apps import router as apps_router
from .auth import router as auth_router
from .backup import router as backup_router
from .compat_v3 import router as compat_v3_router
from .config import router as config_router
from .discovery import router as discovery_router
from .groups import router as groups_router
from .user_analytics import router as user_analytics_router
from .health import router as health_router
from .memories import router as memories_router
from .metrics import router as metrics_router
from .mcp_oauth_compat import router as mcp_oauth_compat_router
from .ops_metrics import router as ops_metrics_router
from .provision import router as provision_router
from .specs import router as specs_router
from .specs_rich import router as specs_rich_router
from .stats import router as stats_router
from .store import router as store_router

__all__ = [
    "admin_router",
    "admin_autodedup_router",
    "admin_write_queue_router",
    "agent_tokens_router",
    "auth_router",
    "memories_router",
    "apps_router",
    "stats_router",
    "config_router",
    "backup_router",
    "discovery_router",
    "mcp_oauth_compat_router",
    "compat_v3_router",
    "provision_router",
    "health_router",
    "metrics_router",
    "ops_metrics_router",
    "governance_router",
    "governance_project_merge_router",
    "governance_schedule_router",
    "groups_router",
    "user_analytics_router",
    "specs_router",
    "specs_rich_router",
    "store_router",
]
