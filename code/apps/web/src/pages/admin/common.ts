// 管理后台各面板共享的工具函数

import type { DepartmentNode } from '../../mocks/data'

/** 从未知异常中提取中文错误消息（ApiError 已在 http 层解析好后端 detail）。 */
export function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

export type FlatDept = {
  id: string
  name: string
  path: string
  depth: number
}

/** 部门树扁平化（表单下拉、移动目标用）。 */
export function flattenDeptTree(tree: DepartmentNode[]): FlatDept[] {
  const result: FlatDept[] = []
  const walk = (nodes: DepartmentNode[], depth: number) => {
    for (const node of nodes) {
      result.push({ id: node.id, name: node.name, path: node.path, depth })
      walk(node.children, depth + 1)
    }
  }
  walk(tree, 0)
  return result
}

/** 收集某节点子树（含自身）的全部 id，用于移动时禁止把部门挂到自己的子孙下。 */
export function collectSubtreeIds(node: DepartmentNode): Set<string> {
  const ids = new Set<string>()
  const walk = (n: DepartmentNode) => {
    ids.add(n.id)
    n.children.forEach(walk)
  }
  walk(node)
  return ids
}

/** 密级数值 → 中文标签。 */
export const CLEARANCE_LABEL: Record<number, string> = {
  10: '公开',
  20: '内部',
  30: '机密',
  40: '绝密',
}

/** 密级对应的 Tag 颜色。 */
export const CLEARANCE_COLOR: Record<number, string> = {
  10: 'default',
  20: 'blue',
  30: 'orange',
  40: 'red',
}
