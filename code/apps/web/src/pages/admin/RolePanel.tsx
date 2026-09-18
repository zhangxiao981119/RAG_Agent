import { useCallback, useEffect, useState } from 'react'
import { App, Button, Form, Input, Modal, Popconfirm, Space, Table } from 'antd'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'

import {
  AdminRole,
  createRole,
  deleteRole,
  fetchRoles,
  updateRole,
} from '../../mocks/data'
import { errMsg } from './common'

export function RolePanel() {
  const { message } = App.useApp()
  const [roles, setRoles] = useState<AdminRole[]>([])
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState<{ mode: 'create' } | { mode: 'edit'; role: AdminRole } | null>(
    null,
  )
  // 前端搜索关键字（按角色名过滤）
  const [search, setSearch] = useState('')

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      setRoles(await fetchRoles())
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    void refresh()
  }, [refresh])

  async function handleDelete(role: AdminRole) {
    try {
      const result = await deleteRole(role.id)
      message.success(
        result.affected_users > 0
          ? `角色已删除，并从 ${result.affected_users} 个用户的角色列表中移除`
          : '角色已删除',
      )
      await refresh()
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  const columns: ColumnsType<AdminRole> = [
    { title: '角色名', dataIndex: 'name', key: 'name' },
    {
      title: '关联用户数',
      dataIndex: 'user_count',
      key: 'user_count',
      width: 140,
      render: (v: number) => `${v} 人`,
    },
    {
      title: '操作',
      key: 'actions',
      width: 160,
      render: (_, record) => (
        <Space size={4}>
          <Button type="link" size="small" onClick={() => setEditing({ mode: 'edit', role: record })}>
            重命名
          </Button>
          <Popconfirm
            title={`确认删除角色「${record.name}」？`}
            description={
              record.user_count > 0
                ? `将自动从 ${record.user_count} 个用户的 role_names 中移除该角色。`
                : undefined
            }
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

  // 前端搜索过滤：按角色名匹配（大小写不敏感），空关键字返回全部
  const filteredRoles = roles.filter((r) => {
    const q = search.trim().toLowerCase()
    if (!q) return true
    return r.name.toLowerCase().includes(q)
  })

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16, gap: 12, flexWrap: 'wrap' }}>
        <Space>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setEditing({ mode: 'create' })}>
            新建角色
          </Button>
          <Button icon={<ReloadOutlined />} onClick={() => void refresh()}>
            刷新
          </Button>
        </Space>
        <Input.Search
          placeholder="按角色名搜索"
          allowClear
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ width: 240 }}
        />
      </div>

      <Table<AdminRole>
        rowKey="id"
        columns={columns}
        dataSource={filteredRoles}
        loading={loading}
        size="middle"
        pagination={false}
        locale={{ emptyText: '暂无角色' }}
      />

      {editing && (
        <RoleFormModal
          state={editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null)
            await refresh()
          }}
        />
      )}
    </div>
  )
}

// ── 新建 / 重命名角色弹窗 ────────────────────────────────────
function RoleFormModal({
  state,
  onClose,
  onSaved,
}: {
  state: { mode: 'create' } | { mode: 'edit'; role: AdminRole }
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const { message } = App.useApp()
  const [form] = Form.useForm<{ name: string }>()
  const [saving, setSaving] = useState(false)
  const isCreate = state.mode === 'create'

  async function handleSubmit(values: { name: string }) {
    setSaving(true)
    try {
      if (isCreate) {
        await createRole(values.name.trim())
        message.success('角色已创建')
      } else if (state.mode === 'edit') {
        await updateRole(state.role.id, values.name.trim())
        message.success('角色已重命名，所有用户的 role_names 已自动同步')
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
      title={isCreate ? '新建角色' : `重命名角色「${state.mode === 'edit' ? state.role.name : ''}」`}
      open
      onCancel={onClose}
      confirmLoading={saving}
      okText="确认"
      cancelText="取消"
      onOk={() => void form.submit()}
      destroyOnClose
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{ name: state.mode === 'edit' ? state.role.name : '' }}
        onFinish={handleSubmit}
        style={{ marginTop: 16 }}
      >
        <Form.Item
          label="角色名（仅字母、数字、下划线、短横线）"
          name="name"
          rules={[
            { required: true, message: '请输入角色名' },
            { pattern: /^[a-zA-Z0-9_-]+$/, message: '仅支持字母、数字、下划线、短横线' },
          ]}
          extra={
            isCreate
              ? undefined
              : '改名后会自动同步所有用户的 role_names 字段。'
          }
        >
          <Input placeholder="例如：auditor" maxLength={64} />
        </Form.Item>
      </Form>
    </Modal>
  )
}
