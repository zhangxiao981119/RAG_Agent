"""权限服务：主体解析 / 标签生成 / 可见性判定。"""
from app.services.acl.errors import AclError, AclPathError, AclTagError
from app.services.acl.subjects import (
    DeptNode,
    ExpansionWarning,
    KnowledgeBaseRecord,
    Principal,
    UserRecord,
    build_principal,
    check_expansion_scale,
    compute_doc_acl_tags,
    dept_ancestors,
    normalize_dept_path,
    resolve_authorized_kb_ids,
    resolve_user_subjects,
    validate_acl_tags,
)
from app.services.acl.visibility import (
    PUSHDOWN_WHERE_SQL,
    Chunk,
    filter_visible,
    is_visible,
    pushdown_params,
)

__all__ = [
    "PUSHDOWN_WHERE_SQL",
    "AclError",
    "AclPathError",
    "AclTagError",
    "Chunk",
    "DeptNode",
    "ExpansionWarning",
    "KnowledgeBaseRecord",
    "Principal",
    "UserRecord",
    "build_principal",
    "check_expansion_scale",
    "compute_doc_acl_tags",
    "dept_ancestors",
    "filter_visible",
    "is_visible",
    "normalize_dept_path",
    "pushdown_params",
    "resolve_authorized_kb_ids",
    "resolve_user_subjects",
    "validate_acl_tags",
]
