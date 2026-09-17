import { useCallback, useEffect, useState } from 'react'
import {
  App,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tag,
  TreeSelect,
} from 'antd'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'

import {
  createDepartment,
  deleteDepartment,
  DepartmentNode,
  fetchDepartments,
  moveDepartment,
  updateDepartment,
} from '../../mocks/data'
import { collectSubtreeIds, errMsg, flattenDeptTree } from './common'

// 新建/重命名弹窗状态
type EditState =
  | { mode: 'createRoot' }
  | { mode: 'createChild'; node: DepartmentNode }
  | { mode: 'rename'; node: DepartmentNode }

type DeptFormValues = {
  name: string
  sort_order: number
  visible_to_parent: boolean
}

export function DeptPanel() {
  const { message } = App.useApp()
  const [tree, setTree] = useState<DepartmentNode[]>([])
  const [loading, setLoading] = useState(true)
  const [edit, setEdit] = useState<EditState | null>(null)
  const [moveNode, setMoveNode] = useState<DepartmentNode | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      setTree(await fetchDepartments())
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    void refresh()
  }, [refresh])

  async function handleDelete(node: DepartmentNode) {
    try {
      await deleteDepartment(node.id)
      message.success(`部门「${node.name}」已删除`)
      await refresh()
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  const columns: ColumnsType<DepartmentNode> = [
    {
      title: '部门名称',
      dataIndex: 'name',
      key: 'name',
      width: 360,
      render: (_, record) => (
        <Space size={8} wrap>
          <span style={{ fontWeight: 500 }}>{record.name}</span>
          <span style={{ color: 'rgba(0,0,0,0.45)', fontSize: 12 }}>{record.path}</span>
          {record.user_count > 0 && (
            <Tag style={{ marginInlineEnd: 0 }}>{record.user_count} 人</Tag>
          )}
          {!record.visible_to_parent && <Tag color="orange">对祖先不可见</Tag>}
        </Space>
      ),
    },
    {
      title: '排序',
      dataIndex: 'sort_order',
      key: 'sort_order',
      width: 90,
    },
    {
      title: '操作',
      key: 'actions',
      width: 300,
      render: (_, record) => (
        <Space size={4}>
          <Button
            type="link"
            size="small"
            onClick={() => setEdit({ mode: 'createChild', node: record })}
          >
            新建子部门
          </Button>
          <Button type="link" size="small" onClick={() => setEdit({ mode: 'rename', node: record })}>
            重命名
          </Button>
          <Button type="link" size="small" onClick={() => setMoveNode(record)}>
            移动
          </Button>
          <Popconfirm
            title={`确认删除部门「${record.name}」？`}
            description="仅允许删除空部门（无子部门且无用户）。"
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => handleDelete(record)}
          >
            <Button type="link" size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setEdit({ mode: 'createRoot' })}>
          新建根部门
        </Button>
        <Button icon={<ReloadOutlined />} onClick={() => void refresh()}>
          刷新
        </Button>
      </Space>

      <Table<DepartmentNode>
        rowKey="id"
        columns={columns}
        dataSource={tree}
        loading={loading}
        pagination={false}
        size="middle"
        childrenColumnName="children"
        locale={{ emptyText: '暂无部门，请新建根部门' }}
      />

      {edit && (
        <DeptFormModal
          state={edit}
          onClose={() => setEdit(null)}
          onSaved={async () => {
            setEdit(null)
            await refresh()
          }}
        />
      )}
      {moveNode && (
        <DeptMoveModal
          node={moveNode}
          tree={tree}
          onClose={() => setMoveNode(null)}
          onSaved={async () => {
            setMoveNode(null)
            await refresh()
          }}
        />
      )}
    </div>
  )
}

// ── 新建 / 重命名弹窗 ────────────────────────────────────────
function DeptFormModal({
  state,
  onClose,
  onSaved,
}: {
  state: NonNullable<EditState>
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const { message } = App.useApp()
  const [form] = Form.useForm<DeptFormValues>()
  const [saving, setSaving] = useState(false)

  const title =
    state.mode === 'createRoot'
      ? '新建根部门'
      : state.mode === 'createChild'
        ? `在「${state.node.name}」下新建子部门`
        : `重命名「${state.node.name}」`

  const initialValues: DeptFormValues =
    state.mode === 'rename'
      ? {
          name: state.node.name,
          sort_order: state.node.sort_order,
          visible_to_parent: state.node.visible_to_parent,
        }
      : { name: '', sort_order: 0, visible_to_parent: true }

  async function handleSubmit(values: DeptFormValues) {
    setSaving(true)
    try {
      if (state.mode === 'createRoot') {
        await createDepartment({
          name: values.name,
          parent_id: null,
          sort_order: values.sort_order,
          visible_to_parent: values.visible_to_parent,
        })
        message.success('根部门已创建')
      } else if (state.mode === 'createChild') {
        await createDepartment({
          name: values.name,
          parent_id: state.node.id,
          sort_order: values.sort_order,
          visible_to_parent: values.visible_to_parent,
        })
        message.success('子部门已创建')
      } else {
        await updateDepartment(state.node.id, {
          name: values.name,
          sort_order: values.sort_order,
          visible_to_parent: values.visible_to_parent,
        })
        message.success('部门已更新，子树路径已级联重写')
      }
      await onSaved()
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={title}
      open
      onCancel={onClose}
      confirmLoading={saving}
      okText="确认"
      cancelText="取消"
      onOk={() => void form.submit()}
      destroyOnClose
    >
      <Form form={form} layout="vertical" initialValues={initialValues} onFinish={handleSubmit}>
        <Form.Item
          label="部门名称（不能含 /）"
          name="name"
          rules={[
            { required: true, message: '请输入部门名称' },
            { pattern: /^[^/]+$/, message: '部门名称不能包含 /' },
          ]}
          style={{ marginTop: 16 }}
        >
          <Input placeholder="例如：技术中心" maxLength={64} />
        </Form.Item>
        <Form.Item label="同层排序（数字越小越靠前）" name="sort_order">
          <InputNumber precision={0} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item
          label="对祖先可见"
          name="visible_to_parent"
          valuePropName="checked"
          extra="取消勾选后，祖先部门看不到本子树（如薪酬组场景）。"
        >
          <Switch />
        </Form.Item>
      </Form>
    </Modal>
  )
}

// ── 移动弹窗 ─────────────────────────────────────────────────
function DeptMoveModal({
  node,
  tree,
  onClose,
  onSaved,
}: {
  node: DepartmentNode
  tree: DepartmentNode[]
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const { message } = App.useApp()
  const [targetId, setTargetId] = useState<string | undefined>(undefined)
  const [saving, setSaving] = useState(false)

  // 禁止移动到自身或自己的子树
  const bannedIds = collectSubtreeIds(node)
  const allowed = flattenDeptTree(tree).filter((d) => !bannedIds.has(d.id))

  const treeData = buildTreeSelectData(tree, bannedIds)

  async function handleMove() {
    setSaving(true)
    try {
      await moveDepartment(node.id, targetId ?? null)
      message.success('部门已移动，子树路径已级联重写')
      await onSaved()
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={`移动「${node.name}」到新父部门`}
      open
      onCancel={onClose}
      confirmLoading={saving}
      okText="移动"
      cancelText="取消"
      onOk={handleMove}
      destroyOnClose
    >
      <div style={{ color: 'rgba(0,0,0,0.45)', fontSize: 13, margin: '16px 0 8px' }}>
        目标父部门（留空表示移到根；不能移动到自身或其子树）
      </div>
      <TreeSelect
        style={{ width: '100%' }}
        allowClear
        placeholder="（根）"
        value={targetId}
        onChange={(v) => setTargetId(v)}
        treeData={treeData}
        treeDefaultExpandAll
        showSearch
        treeNodeFilterProp="title"
        fieldNames={{ label: 'title', value: 'value', children: 'children' }}
      />
      {allowed.length === 0 && (
        <div style={{ marginTop: 8, color: '#faad14', fontSize: 12 }}>
          当前没有可选的目标部门
        </div>
      )}
    </Modal>
  )
}

/** 递归构造 TreeSelect 数据，被禁用的子树整体灰掉不可选。 */
type DeptTreeNode = {
  title: string
  value: string
  disabled: boolean
  children: DeptTreeNode[]
}

function buildTreeSelectData(nodes: DepartmentNode[], bannedIds: Set<string>): DeptTreeNode[] {
  return nodes.map((n) => ({
    title: n.path,
    value: n.id,
    disabled: bannedIds.has(n.id),
    children: buildTreeSelectData(n.children, bannedIds),
  }))
}
