"""主体解析与标签生成 —— 手册 §3.2.2 / §3.2.3 / §3.2.4 的可运行实现。

★ 本文件是 `acl_tags` 的**唯一**生成入口（手册 §4.2.8 硬约束 2）。

口径要点（改动前先读手册）：
  · 用户侧：`祖先 ∪ 自己 ∪ 子孙`（A1 + A2 双向展开）
  · 文档侧：**只取自己所属那一条路径**，MUST NOT 展开祖先（H1）
    —— 两侧刻意不对称；若都展开祖先，任意两个主体在根节点相交 → 全公司互通。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.config import decisions as D
from app.services.acl.errors import AclPathError, AclTagError

# ─────────────────────────────────────────────────────────────
# 数据模型（骨架版：内存结构。M3/M4 换成 SQLAlchemy 模型，
# 但下面的函数签名与判定顺序 MUST 保持不变）
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeptNode:
    """组织树节点。`path` 形如 `/总部/技术中心/后端组/`。"""

    path: str
    visible_to_parent: bool = True
    """§8 D-12：False ⇒ 祖先用户 MUST NOT 看到本部门及其子树。"""


@dataclass(frozen=True)
class UserRecord:
    """用户。`dept_paths` 支持兼岗（第一个视为主部门）。"""

    id: str
    tenant_id: str
    dept_paths: tuple[str, ...] = ()
    clearance: int = D.LEVEL_RANK_INTERNAL
    group_ids: tuple[str, ...] = ()
    role_names: tuple[str, ...] = ()
    region: str | None = None


@dataclass(frozen=True)
class KnowledgeBaseRecord:
    """知识库。`is_public=True` 时所有登录用户自动成为成员（§3.2.7）。"""

    id: str
    tenant_id: str
    is_public: bool = False
    members: tuple[str, ...] = ()


@dataclass(frozen=True)
class Principal:
    """解析后的用户上下文。放进缓存的就是它（§3.2.6）。"""

    user_id: str
    tenant_id: str
    subjects: frozenset[str]
    authorized_kb_ids: frozenset[str]
    clearance: int

    def has_write_intent(self) -> bool:  # pragma: no cover - 占位，M4 用
        return False


@dataclass
class ExpansionWarning:
    """展开规模告警（§3.2.7 / §7.2 核验 SQL 的运行时补充）。"""

    user_id: str
    subject_count: int
    limit: int
    warnings: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# 部门路径工具
# ─────────────────────────────────────────────────────────────


def normalize_dept_path(path: str) -> str:
    """规整为 `/a/b/` 形式。文档与用户两侧 MUST 用同一个函数，否则标签对不上。"""
    if not path or not path.strip("/"):
        raise AclPathError(f"部门路径为空: {path!r}")
    parts = [p.strip() for p in path.split("/") if p.strip()]
    return "/" + "/".join(parts) + "/"


def dept_ancestors(path: str, *, include_self: bool = True) -> list[str]:
    """祖先链，从根到自身：`/总部/技术中心/后端组/` → [`/总部/`, `/总部/技术中心/`, 自身]。"""
    normalized = normalize_dept_path(path)
    parts = [p for p in normalized.split("/") if p]
    out = ["/" + "/".join(parts[: i + 1]) + "/" for i in range(len(parts))]
    return out if include_self else out[:-1]


def _visible_to_ancestor(
    descendant: str, ancestor: str, vtp: Mapping[str, bool]
) -> bool:
    """`descendant` 能否被 `ancestor` 看到（§8 D-12）。

    从 `ancestor` 往下的**每一级**都必须 `visible_to_parent=True`；
    只要有一级为 False，整棵子树对祖先不可见。
    """
    for node in dept_ancestors(descendant):
        if len(node) <= len(ancestor):
            continue  # 只检查严格位于 ancestor 之下的部分
        if not vtp.get(node, True):
            return False
    return True


# ─────────────────────────────────────────────────────────────
# 用户侧：主体解析
# ─────────────────────────────────────────────────────────────


def resolve_user_subjects(
    user: UserRecord,
    departments: Mapping[str, DeptNode] | Iterable[str] = (),
) -> frozenset[str]:
    """解析用户主体。含部门双向展开（A1 + A2）。

    `public` 对所有登录用户成立（含新账号、含外部人员）。
    """
    vtp: dict[str, bool] = {}
    all_paths: list[str] = []
    nodes = (
        departments.values() if isinstance(departments, Mapping) else departments
    )
    for node in nodes:
        if isinstance(node, str):
            all_paths.append(normalize_dept_path(node))
        else:
            path = normalize_dept_path(node.path)
            all_paths.append(path)
            vtp[path] = node.visible_to_parent

    subjects: set[str] = {f"user:{user.id}", "public"}
    for gid in user.group_ids:
        subjects.add(f"group:{gid}")
    for role in user.role_names:
        subjects.add(f"role:{role}")
    if user.region:
        subjects.add(f"region:{user.region}")

    for raw_path in user.dept_paths:
        own = normalize_dept_path(raw_path)

        # A1 · 祖先 ∪ 自己
        for ancestor in dept_ancestors(own):
            subjects.add(f"dept:{ancestor}")

        # A2 · 子孙（受 §8 D-12 的 visible_to_parent 约束）
        if D.USER_SUBJECT_DESCENDANT_EXPANSION:
            for candidate in all_paths:
                if candidate == own or not candidate.startswith(own):
                    continue
                if _visible_to_ancestor(candidate, own, vtp):
                    subjects.add(f"dept:{candidate}")

    return frozenset(subjects)


def resolve_authorized_kb_ids(
    user: UserRecord,
    subjects: frozenset[str],
    knowledge_bases: Iterable[KnowledgeBaseRecord],
) -> frozenset[str]:
    """G2 的右半边：用户有权限访问的知识库集合。

    ★ 公开库（`is_public`）对所有用户自动成立；其余靠 `kb_members` 交集。
    ★ 注意：这里是"库级"授权，**不是**文档可见性 —— 库内文档仍须过 G1/G3/G4。
    """
    out: set[str] = set()
    for kb in knowledge_bases:
        if kb.tenant_id != user.tenant_id:
            continue
        if kb.is_public and D.PUBLIC_KB_ENABLED:
            out.add(kb.id)
            continue
        if set(kb.members) & subjects:
            out.add(kb.id)
    return frozenset(out)


def build_principal(
    user: UserRecord,
    departments: Mapping[str, DeptNode] | Iterable[str] = (),
    knowledge_bases: Iterable[KnowledgeBaseRecord] = (),
) -> Principal:
    """一次解析出可直接放进缓存的 Principal（§3.2.6）。"""
    subjects = resolve_user_subjects(user, departments)
    return Principal(
        user_id=user.id,
        tenant_id=user.tenant_id,
        subjects=subjects,
        authorized_kb_ids=resolve_authorized_kb_ids(user, subjects, knowledge_bases),
        clearance=user.clearance,
    )


def check_expansion_scale(principal: Principal) -> ExpansionWarning | None:
    """主体数超过软上限时告警（不抛出 —— 告警不是拒绝服务）。"""
    count = len(principal.subjects)
    if count <= D.SUBJECT_EXPANSION_SOFT_LIMIT:
        return None
    return ExpansionWarning(
        user_id=principal.user_id,
        subject_count=count,
        limit=D.SUBJECT_EXPANSION_SOFT_LIMIT,
        warnings=["组织树过深，考虑预计算部门闭包表（手册 §7.2 核验 SQL）"],
    )


# ─────────────────────────────────────────────────────────────
# 文档侧：标签生成（唯一入口）
# ─────────────────────────────────────────────────────────────


def validate_acl_tags(tags: Iterable[str]) -> None:
    """写库前置校验（H2 / H3）。失败即拒绝入库。"""
    tag_set = {t.strip() for t in tags if t and t.strip()}
    if not tag_set:
        raise AclTagError("acl_tags MUST NOT 为空数组（须先规范化为 {public}）")
    if D.ACL_PUBLIC_MUTEX and "public" in tag_set and len(tag_set) != 1:
        raise AclTagError(
            f"public MUST 单独存在（H2 · §3.2.4）。收到: {sorted(tag_set)}；"
            "混标会让 G4 对所有用户成立，只剩 G2 在守"
        )
    if not D.ACL_LEVEL_TAGS_ALLOWED:
        bad = sorted(t for t in tag_set if t.startswith("level:"))
        if bad:
            raise AclTagError(
                f"acl_tags MUST NOT 含 level: 标签（H3 · §3.2.4）。收到: {bad}；"
                "放进来会让 G4 绕过 G1，密级形同虚设"
            )


def compute_doc_acl_tags(
    *,
    dept_path: str | None = None,
    extra_tags: Iterable[str] = (),
) -> frozenset[str]:
    """由文档属性算出 `chunk.acl_tags`。**这是唯一入口**（手册 §4.2.8）。

    · `dept_path` 只写入文档**自己**那一条路径 → MUST NOT 展开祖先（H1）
    · 结果为空 → 规范化为 `{public}`（MUST NOT 留空数组）
    · 结果 MUST 过 `validate_acl_tags`
    """
    tags: set[str] = {t.strip() for t in extra_tags if t and t.strip()}

    if dept_path:
        own = normalize_dept_path(dept_path)
        if D.DOC_TAG_ANCESTOR_EXPANSION:
            # ★ 只为回归测试保留：证明"打开它会全公司互通"（tests/acl/test_visibility.py::test_10）。
            #   生产路径 MUST NOT 走到这里 —— self_check() 会拦住这个取值。
            for ancestor in dept_ancestors(own):
                tags.add(f"dept:{ancestor}")
        else:
            tags.add(f"dept:{own}")

    if not tags:
        tags = {"public"}

    validate_acl_tags(tags)
    return frozenset(tags)
