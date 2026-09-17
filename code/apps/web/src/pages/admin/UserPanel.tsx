import { useCallback, useEffect, useState } from 'react'
import {
  App,
  Badge,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
} from 'antd'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType, TablePaginationConfig } from 'antd/es/table'

import {
  AdminRole,
  AdminUser,
  createUser,
  DepartmentNode,
  deleteUser,
  fetchDepartments,
  fetchRoles,
  fetchUsers,
  getStoredUser,
  resetUserPassword,
  updateUser,
} from '../../mocks/data'
import { CLEARANCE_COLOR, CLEARANCE_LABEL, errMsg, flattenDeptTree } from './common'

type EditState =
  | { mode: 'create' }
  | { mode: 'edit'; user: AdminUser }
  | { mode: 'password'; user: AdminUser }

type UserFormValues = {
  username?: string
  display_name: string
  email?: string
  password?: string
  dept_id?: string
  clearance: number
  role_names: string[]
  status?: 'active' | 'disabled'
}

export function UserPanel() {
  const { message } = App.useApp()
  const [users, setUsers] = useState<AdminUser[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [loading, setLoading] = useState(false)
  const [deptTree, setDeptTree] = useState<DepartmentNode[]>([])
  const [roles, setRoles] = useState<AdminRole[]>([])
  const [edit, setEdit] = useState<EditState | null>(null)

  const load = useCallback(
    async (p: number, ps: number) => {
      setLoading(true)
      try {
        const data = await fetchUsers(p, ps)
        setUsers(data.items)
        setTotal(data.total)
      } catch (e) {
        message.error(errMsg(e))
      } finally {
        setLoading(false)
      }
    },
    [message],
  )

  useEffect(() => {
    void load(page, pageSize)
  }, [page, pageSize, load])

  // 部门树 + 角色列表只在弹窗打开时需要，这里随面板加载一次供下拉使用
  useEffect(() => {
    fetchDepartments()
      .then(setDeptTree)
      .catch((e) => message.error(errMsg(e)))
    fetchRoles()
      .then(setRoles)
      .catch((e) => message.error(errMsg(e)))
  }, [message])

  function handleTableChange(pagination: TablePaginationConfig) {
    const nextPage = pagination.current ?? 1
    const nextSize = pagination.pageSize ?? 10
    setPageSize(nextSize)
    setPage(nextPage)
  }

  async function handleDelete(user: AdminUser) {
    try {
      await deleteUser(user.id)
      message.success(`用户「${user.username}」已删除`)
      // 删完后若当前页空了，回退一页
      if (users.length === 1 && page > 1) setPage(page - 1)
      else await load(page, pageSize)
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  const currentUserId = getStoredUser()?.id
  const flatDepts = flattenDeptTree(deptTree)
  const roleOptions = roles.map((r) => ({ label: r.name, value: r.name }))

  const columns: ColumnsType<AdminUser> = [
    { title: '用户名', dataIndex: 'username', key: 'username', width: 130 },
    { title: '姓名', dataIndex: 'display_name', key: 'display_name', width: 120 },
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
      width: 200,
      render: (v: string | null) => v || <span style={{ color: 'rgba(0,0,0,0.35)' }}>—</span>,
    },
    {
      title: '部门',
      dataIndex: 'dept_path',
      key: 'dept_path',
      render: (v: string | null) => v || <span style={{ color: 'rgba(0,0,0,0.35)' }}>未分配</span>,
    },
    {
      title: '密级',
      dataIndex: 'clearance',
      key: 'clearance',
      width: 90,
      render: (v: number) => (
        <Tag color={CLEARANCE_COLOR[v] ?? 'default'}>
          {CLEARANCE_LABEL[v] ?? `L${v}`}
        </Tag>
      ),
    },
    {
      title: '角色',
      dataIndex: 'role_names',
      key: 'role_names',
      width: 160,
      render: (v: string[]) =>
        v.length > 0 ? (
          <Space size={4} wrap>
            {v.map((r) => (
              <Tag color="geekblue" key={r}>
                {r}
              </Tag>
            ))}
          </Space>
        ) : (
          <span style={{ color: 'rgba(0,0,0,0.35)' }}>—</span>
        ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (v: string) =>
        v === 'active' ? (
          <Badge status="success" text="正常" />
        ) : (
          <Badge status="error" text="已禁用" />
        ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 200,
      fixed: 'right',
      render: (_, record) => (
        <Space size={4}>
          <Button type="link" size="small" onClick={() => setEdit({ mode: 'edit', user: record })}>
            编辑
          </Button>
          <Button
            type="link"
            size="small"
            onClick={() => setEdit({ mode: 'password', user: record })}
          >
            重置密码
          </Button>
          {record.id === currentUserId ? (
            <Button type="link" size="small" danger disabled>
              删除
            </Button>
          ) : (
            <Popconfirm
              title={`确认删除用户「${record.username}」？`}
              description="删除后不可恢复。"
              okText="删除"
              okButtonProps={{ danger: true }}
              cancelText="取消"
              onConfirm={() => handleDelete(record)}
            >
              <Button type="link" size="small" danger>
                删除
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setEdit({ mode: 'create' })}>
          新建用户
        </Button>
        <Button icon={<ReloadOutlined />} onClick={() => void load(page, pageSize)}>
          刷新
        </Button>
      </Space>

      <Table<AdminUser>
        rowKey="id"
        columns={columns}
        dataSource={users}
        loading={loading}
        size="middle"
        scroll={{ x: 1100 }}
        onChange={handleTableChange}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          pageSizeOptions: [10, 20, 50],
          showTotal: (t) => `共 ${t} 个用户`,
        }}
      />

      {edit && (edit.mode === 'create' || edit.mode === 'edit') && (
        <UserFormModal
          state={edit}
          flatDepts={flatDepts}
          roleOptions={roleOptions}
          onClose={() => setEdit(null)}
          onSaved={async () => {
            setEdit(null)
            await load(page, pageSize)
          }}
        />
      )}
      {edit && edit.mode === 'password' && (
        <UserPasswordModal
          user={edit.user}
          onClose={() => setEdit(null)}
          onSaved={() => setEdit(null)}
        />
      )}
    </div>
  )
}

// ── 新建 / 编辑用户弹窗 ──────────────────────────────────────
function UserFormModal({
  state,
  flatDepts,
  roleOptions,
  onClose,
  onSaved,
}: {
  state: { mode: 'create' } | { mode: 'edit'; user: AdminUser }
  flatDepts: { id: string; path: string; depth: number }[]
  roleOptions: { label: string; value: string }[]
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const { message } = App.useApp()
  const [form] = Form.useForm<UserFormValues>()
  const [saving, setSaving] = useState(false)
  const isCreate = state.mode === 'create'

  const initialValues: Partial<UserFormValues> =
    state.mode === 'edit'
      ? {
          display_name: state.user.display_name,
          email: state.user.email ?? '',
          dept_id: state.user.dept_id ?? undefined,
          clearance: state.user.clearance,
          role_names: state.user.role_names,
          status: state.user.status,
        }
      : { display_name: '', email: '', clearance: 20, role_names: [], status: 'active' }

  const deptOptions = flatDepts.map((d) => ({
    label: `${'　'.repeat(d.depth)}${d.path}`,
    value: d.id,
  }))

  async function handleSubmit(values: UserFormValues) {
    setSaving(true)
    try {
      if (isCreate) {
        await createUser({
          username: values.username!,
          display_name: values.display_name.trim(),
          email: values.email?.trim() || null,
          password: values.password!,
          dept_id: values.dept_id ?? null,
          clearance: values.clearance,
          role_names: values.role_names,
        })
        message.success('用户已创建')
      } else if (state.mode === 'edit') {
        // 只提交发生变化的字段；后端用 exclude_unset 区分「未传」与「传 null」
        const patch: Parameters<typeof updateUser>[1] = {}
        if (values.display_name.trim() !== state.user.display_name)
          patch.display_name = values.display_name.trim()
        if ((values.email?.trim() || null) !== (state.user.email ?? null))
          patch.email = values.email?.trim() || null
        if ((values.dept_id ?? null) !== (state.user.dept_id ?? null))
          patch.dept_id = values.dept_id ?? null
        if (values.clearance !== state.user.clearance) patch.clearance = values.clearance
        if (
          [...values.role_names].sort().join(',') !==
          [...state.user.role_names].sort().join(',')
        )
          patch.role_names = values.role_names
        if (values.status !== state.user.status) patch.status = values.status

        if (Object.keys(patch).length === 0) {
          message.info('内容未变更')
          onClose()
          return
        }
        await updateUser(state.user.id, patch)
        message.success('用户已更新')
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
      title={isCreate ? '新建用户' : `编辑用户「${state.mode === 'edit' ? state.user.username : ''}」`}
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
        initialValues={initialValues}
        onFinish={handleSubmit}
        style={{ marginTop: 16 }}
      >
        {isCreate && (
          <Form.Item
            label="用户名"
            name="username"
            rules={[
              { required: true, message: '请输入用户名' },
              { pattern: /^[a-zA-Z0-9_-]+$/, message: '仅支持字母、数字、下划线、短横线' },
            ]}
          >
            <Input placeholder="字母、数字、_- " maxLength={64} autoComplete="off" />
          </Form.Item>
        )}
        <Form.Item
          label="姓名"
          name="display_name"
          rules={[{ required: true, message: '请输入姓名' }]}
        >
          <Input maxLength={64} />
        </Form.Item>
        <Form.Item label="邮箱（可空）" name="email">
          <Input placeholder="可选" maxLength={128} />
        </Form.Item>
        {isCreate && (
          <Form.Item
            label="初始密码"
            name="password"
            rules={[
              { required: true, message: '请输入初始密码' },
              { min: 8, message: '密码至少 8 位' },
            ]}
          >
            <Input.Password placeholder="至少 8 位" autoComplete="new-password" />
          </Form.Item>
        )}
        <Form.Item label="部门" name="dept_id">
          <Select
            allowClear
            showSearch
            placeholder="不分配部门"
            options={deptOptions}
            optionFilterProp="label"
          />
        </Form.Item>
        <Form.Item
          label="密级（0-100，越大权限越高）"
          name="clearance"
          rules={[{ required: true, message: '请输入密级' }]}
        >
          <InputNumber min={0} max={100} precision={0} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item label="角色" name="role_names">
          <Select
            mode="multiple"
            allowClear
            placeholder="选择角色"
            options={roleOptions}
          />
        </Form.Item>
        {!isCreate && (
          <Form.Item label="状态" name="status">
            <Select
              options={[
                { label: '正常', value: 'active' },
                { label: '已禁用', value: 'disabled' },
              ]}
            />
          </Form.Item>
        )}
      </Form>
    </Modal>
  )
}

// ── 重置密码弹窗 ─────────────────────────────────────────────
function UserPasswordModal({
  user,
  onClose,
  onSaved,
}: {
  user: AdminUser
  onClose: () => void
  onSaved: () => void
}) {
  const { message } = App.useApp()
  const [form] = Form.useForm<{ new_password: string; confirm: string }>()
  const [saving, setSaving] = useState(false)

  async function handleSubmit(values: { new_password: string }) {
    setSaving(true)
    try {
      await resetUserPassword(user.id, values.new_password)
      message.success('密码已重置')
      onSaved()
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={`重置「${user.username}」的密码`}
      open
      onCancel={onClose}
      confirmLoading={saving}
      okText="重置"
      cancelText="取消"
      onOk={() => void form.submit()}
      destroyOnClose
    >
      <Form
        form={form}
        layout="vertical"
        onFinish={handleSubmit}
        style={{ marginTop: 16 }}
      >
        <Form.Item
          label="新密码"
          name="new_password"
          rules={[
            { required: true, message: '请输入新密码' },
            { min: 8, message: '密码至少 8 位' },
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item
          label="确认新密码"
          name="confirm"
          dependencies={['new_password']}
          rules={[
            { required: true, message: '请再次输入新密码' },
            ({ getFieldValue }) => ({
              validator(_, value) {
                if (!value || getFieldValue('new_password') === value) return Promise.resolve()
                return Promise.reject(new Error('两次输入的密码不一致'))
              },
            }),
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
      </Form>
    </Modal>
  )
}
