"""权限模块的异常类型。"""
from __future__ import annotations


class AclError(Exception):
    """权限相关错误的基类。"""


class AclTagError(AclError):
    """`acl_tags` 书写违规（H2 / H3）。入库时抛出 → MUST 拒绝入库。"""


class AclPathError(AclError):
    """部门路径格式非法（MUST 前后都带 `/`）。"""
