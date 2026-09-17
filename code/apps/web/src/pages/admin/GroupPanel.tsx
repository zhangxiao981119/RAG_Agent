import { useCallback, useEffect, useState } from 'react'
import {
  App,
  Button,
  Drawer,
  Form,
  Input,
  Modal,
  Popconfirm,
  Radio,
  Select,
  Space,
  Table,
  Tag,
} from 'antd'
import { PlusOutlined, ReloadOutlined, TeamOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'

import {
  addGroupMembers,
  AdminGroup,
  AdminUser,
  createGroup,
  deleteGroup,
  fetchGroupMembers,
  fetchGroups,
  fetchUsers,
  GroupMember,
  removeGroupMember,
  updateGroup,
} from '../../mocks/data'
import { errMsg } from './common'

type EditState = { mode: 'create' } | { mode: 'edit'; group: AdminGroup }

type GroupFormValues = {
  name: string
  kind: 'normal' | 'external'
}

export function GroupPanel() {
  const { message } = App.useApp()
  const [groups, setGroups] = useState<AdminGroup[]>([])
  const [loading, setLoading] = useState(true)
  const [edit, setEdit] = useState<EditState | null>(null)
  const [membersGroup, setMembersGroup] = useState<AdminGroup | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      setGroups(await fetchGroups())
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    void refresh()
  }, [refresh])

  async function handleDelete(group: AdminGroup) {
    try {
      await deleteGroup(group.id)
      message.success(`用户组「${group.name}」已删除`)
      await refresh()
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  const columns: ColumnsType<AdminGroup> = [
    { title: '组名', dataIndex: 'name', key: 'name' },
    {
      title: '类型',
      dataIndex: 'kind',
      key: 'kind',
      width: 140,
      render: (v: string) =>
        v === 'external' ? <Tag color="purple">外部</Tag> : <Tag>内部</Tag>,
    },
    {
      title: '成员数',
      dataIndex: 'member_count',
      key: 'member_count',
      width: 100,
    },
    {
      title: '操作',
      key: 'actions',
      width: 220,
      render: (_, record) => (
        <Space size={4}>
          <Button type="link" size="small" onClick={() => setEdit({ mode: 'edit', group: record })}>
            编辑
          </Button>
          <Button
            type="link"
            size="small"
            icon={<TeamOutlined />}
            onClick={() => setMembersGroup(record)}
          >
            成员
          </Button>
          <Popconfirm
            title={`确认删除用户组「${record.name}」？`}
            description="组成员关系会一并解除。"
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
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setEdit({ mode: 'create' })}>
          新建用户组
        </Button>
        <Button icon={<ReloadOutlined />} onClick={() => void refresh()}>
          刷新
        </Button>
      </Space>

      <Table<AdminGroup>
        rowKey="id"
        columns={columns}
        dataSource={groups}
        loading={loading}
        size="middle"
        pagination={false}
      />

      {edit && (
        <GroupFormModal
          state={edit}
          onClose={() => setEdit(null)}
          onSaved={async () => {
            setEdit(null)
            await refresh()
          }}
        />
      )}

      <Drawer
        title={membersGroup ? `管理「${membersGroup.name}」成员` : ''}
        width={560}
        open={membersGroup !== null}
        onClose={() => setMembersGroup(null)}
        destroyOnClose
      >
        {membersGroup && (
          <GroupMembersDrawer
            group={membersGroup}
            onGroupChanged={() => void refresh()}
          />
        )}
      </Drawer>
    </div>
  )
}

// ── 新建 / 编辑组弹窗 ────────────────────────────────────────
function GroupFormModal({
  state,
  onClose,
  onSaved,
}: {
  state: EditState
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const { message } = App.useApp()
  const [form] = Form.useForm<GroupFormValues>()
  const [saving, setSaving] = useState(false)
  const isCreate = state.mode === 'create'

  async function handleSubmit(values: GroupFormValues) {
    setSaving(true)
    try {
      if (isCreate) {
        await createGroup({ name: values.name.trim(), kind: values.kind })
        message.success('用户组已创建')
      } else if (state.mode === 'edit') {
        await updateGroup(state.group.id, { name: values.name.trim(), kind: values.kind })
        message.success('用户组已更新')
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
      title={isCreate ? '新建用户组' : `编辑用户组「${state.mode === 'edit' ? state.group.name : ''}」`}
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
        initialValues={
          state.mode === 'edit'
            ? { name: state.group.name, kind: state.group.kind }
            : { name: '', kind: 'normal' }
        }
        onFinish={handleSubmit}
        style={{ marginTop: 16 }}
      >
        <Form.Item
          label="组名"
          name="name"
          rules={[{ required: true, message: '请输入组名' }]}
        >
          <Input maxLength={64} />
        </Form.Item>
        <Form.Item label="类型" name="kind">
          <Radio.Group>
            <Radio value="normal">内部（normal）</Radio>
            <Radio value="external">外部（external，权限更受限）</Radio>
          </Radio.Group>
        </Form.Item>
      </Form>
    </Modal>
  )
}

// ── 组成员管理抽屉 ───────────────────────────────────────────
function GroupMembersDrawer({
  group,
  onGroupChanged,
}: {
  group: AdminGroup
  onGroupChanged: () => void
}) {
  const { message } = App.useApp()
  const [members, setMembers] = useState<GroupMember[]>([])
  const [allUsers, setAllUsers] = useState<AdminUser[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedUserId, setSelectedUserId] = useState<string | undefined>(undefined)
  const [adding, setAdding] = useState(false)
  const [removingId, setRemovingId] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      // 用户规模有限，一次拉取全部作为添加候选（page_size 上限 200）
      const [mems, userPage] = await Promise.all([
        fetchGroupMembers(group.id),
        fetchUsers(1, 200),
      ])
      setMembers(mems)
      setAllUsers(userPage.items)
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }, [group.id, message])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const memberIds = new Set(members.map((m) => m.user_id))
  const candidates = allUsers.filter((u) => !memberIds.has(u.id))
  const candidateOptions = candidates.map((u) => ({
    label: `${u.username}（${u.display_name}）`,
    value: u.id,
  }))

  async function handleAdd() {
    if (!selectedUserId) return
    setAdding(true)
    try {
      const added = await addGroupMembers(group.id, [selectedUserId])
      message.success(`已添加 ${added} 名成员`)
      setSelectedUserId(undefined)
      await refresh()
      onGroupChanged()
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setAdding(false)
    }
  }

  async function handleRemove(userId: string) {
    setRemovingId(userId)
    try {
      await removeGroupMember(group.id, userId)
      message.success('已移除成员')
      await refresh()
      onGroupChanged()
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setRemovingId(null)
    }
  }

  const columns: ColumnsType<GroupMember> = [
    { title: '用户名', dataIndex: 'username', key: 'username' },
    { title: '姓名', dataIndex: 'display_name', key: 'display_name' },
    {
      title: '操作',
      key: 'actions',
      width: 90,
      render: (_, record) => (
        <Popconfirm
          title="确认将该用户移出本组？"
          okText="移除"
          okButtonProps={{ danger: true }}
          cancelText="取消"
          onConfirm={() => handleRemove(record.user_id)}
        >
          <Button type="link" size="small" danger loading={removingId === record.user_id}>
            移除
          </Button>
        </Popconfirm>
      ),
    },
  ]

  return (
    <div>
      <Space.Compact style={{ width: '100%', marginBottom: 16 }}>
        <Select
          style={{ flex: 1 }}
          showSearch
          allowClear
          placeholder={candidates.length === 0 ? '（暂无可添加用户）' : '选择要添加的用户'}
          value={selectedUserId}
          onChange={setSelectedUserId}
          options={candidateOptions}
          optionFilterProp="label"
          disabled={candidates.length === 0}
        />
        <Button
          type="primary"
          loading={adding}
          disabled={!selectedUserId}
          onClick={handleAdd}
        >
          添加
        </Button>
      </Space.Compact>

      <Table<GroupMember>
        rowKey="user_id"
        columns={columns}
        dataSource={members}
        loading={loading}
        size="small"
        pagination={false}
        locale={{ emptyText: '暂无成员' }}
      />
    </div>
  )
}
