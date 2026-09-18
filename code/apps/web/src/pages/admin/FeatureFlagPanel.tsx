// 特性开关面板 —— 按部门灰度 + 百分比放量，关闭即回滚功能（M6 续篇）
// 列表展示所有规则；行内 Switch 可直接启停；弹窗编辑 pattern / 百分比
import { useCallback, useEffect, useState } from 'react'
import {
  App,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { EditOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import dayjs from 'dayjs'

import {
  createFeatureFlag,
  deleteFeatureFlag,
  fetchFeatureFlags,
  updateFeatureFlag,
  type FeatureFlag,
} from '../../mocks/data'
import { errMsg } from './common'

const { Text } = Typography

// 预置的 feature_key 选项（对应代码里 chat.py 守护的几个特性开关）
const FEATURE_KEY_OPTIONS = [
  { value: 'quota', label: 'quota — 配额管理（超限降级）' },
  { value: 'sensitive_filter', label: 'sensitive_filter — 敏感词过滤' },
  { value: 'rerank', label: 'rerank — 启用重排（关闭则仅用向量分）' },
]

export function FeatureFlagPanel() {
  const { message } = App.useApp()
  const [items, setItems] = useState<FeatureFlag[]>([])
  const [loading, setLoading] = useState(true)
  const [filterKey, setFilterKey] = useState<string | undefined>(undefined)
  const [editing, setEditing] = useState<{ mode: 'create' } | { mode: 'edit'; flag: FeatureFlag } | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setItems(await fetchFeatureFlags(filterKey))
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }, [message, filterKey])

  useEffect(() => {
    void load()
  }, [load])

  // 行内 Switch 启停：关闭 = 回滚该功能（立即清缓存）
  async function handleToggle(flag: FeatureFlag, enabled: boolean) {
    try {
      await updateFeatureFlag(flag.id, { enabled })
      message.success(enabled ? '已开启灰度规则，立即生效' : '已关闭，对应功能已回滚')
      await load()
    } catch (e) {
      message.error(errMsg(e))
      await load()
    }
  }

  async function handleDelete(flag: FeatureFlag) {
    try {
      await deleteFeatureFlag(flag.id)
      message.success('规则已删除')
      await load()
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  const columns: ColumnsType<FeatureFlag> = [
    {
      title: '特性',
      dataIndex: 'feature_key',
      key: 'feature_key',
      width: 200,
      render: (v: string) => <Tag color="blue">{v}</Tag>,
    },
    {
      title: '部门匹配',
      dataIndex: 'dept_path_pattern',
      key: 'dept_path_pattern',
      ellipsis: true,
      render: (v: string) => (
        <Tooltip title={v}>
          <code style={{ fontSize: 12 }}>{v}</code>
        </Tooltip>
      ),
    },
    {
      title: '放量',
      dataIndex: 'rollout_percent',
      key: 'rollout_percent',
      width: 100,
      render: (v: number) => `${v}%`,
    },
    {
      title: '启用',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 90,
      render: (v: boolean, record) => (
        <Switch checked={v} onChange={(checked) => void handleToggle(record, checked)} />
      ),
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      width: 160,
      render: (v: string) => dayjs(v).format('YYYY-MM-DD HH:mm'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 140,
      render: (_, record) => (
        <Space size={4}>
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => setEditing({ mode: 'edit', flag: record })}
          >
            编辑
          </Button>
          <Popconfirm
            title="确认删除该规则？"
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
      <div
        style={{
          marginBottom: 16,
          display: 'flex',
          justifyContent: 'space-between',
          gap: 12,
          flexWrap: 'wrap',
          alignItems: 'center',
        }}
      >
        <Space wrap>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setEditing({ mode: 'create' })}>
            新建灰度规则
          </Button>
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
        </Space>
        <Select
          allowClear
          placeholder="按特性筛选"
          value={filterKey}
          onChange={(v) => setFilterKey(v ?? undefined)}
          options={FEATURE_KEY_OPTIONS}
          style={{ width: 280 }}
        />
      </div>

      <Table<FeatureFlag>
        rowKey="id"
        columns={columns}
        dataSource={items}
        loading={loading}
        size="middle"
        pagination={false}
        locale={{ emptyText: '暂无灰度规则（未配置 = 该特性对所有用户关闭）' }}
      />

      <Text type="secondary" style={{ display: 'block', marginTop: 12 }}>
        匹配规则：按 dept_path_pattern 倒序匹配（更具体优先），SQL LIKE 风格 % 表示任意串；
        启用并命中部门后，再按 hash(user_id) % 100 &lt; rollout_percent 判断是否放量。
      </Text>

      {editing && (
        <FeatureFlagModal
          state={editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null)
            await load()
          }}
        />
      )}
    </div>
  )
}

// ── 新建 / 编辑灰度规则弹窗 ──────────────────────────────
function FeatureFlagModal({
  state,
  onClose,
  onSaved,
}: {
  state: { mode: 'create' } | { mode: 'edit'; flag: FeatureFlag }
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const { message } = App.useApp()
  const isCreate = state.mode === 'create'
  const [form] = Form.useForm<{
    feature_key: string
    dept_path_pattern: string
    enabled: boolean
    rollout_percent: number
  }>()
  const [saving, setSaving] = useState(false)

  async function handleSubmit(values: {
    feature_key: string
    dept_path_pattern: string
    enabled: boolean
    rollout_percent: number
  }) {
    setSaving(true)
    try {
      if (isCreate) {
        await createFeatureFlag({
          feature_key: values.feature_key.trim(),
          dept_path_pattern: values.dept_path_pattern.trim(),
          enabled: values.enabled,
          rollout_percent: values.rollout_percent,
        })
        message.success('灰度规则已创建，立即生效')
      } else if (state.mode === 'edit') {
        // 编辑时仅传可能变更的字段
        await updateFeatureFlag(state.flag.id, {
          dept_path_pattern: values.dept_path_pattern.trim(),
          enabled: values.enabled,
          rollout_percent: values.rollout_percent,
        })
        message.success('灰度规则已更新，立即生效')
      }
      await onSaved()
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setSaving(false)
    }
  }

  const initial: {
    feature_key: string
    dept_path_pattern: string
    enabled: boolean
    rollout_percent: number
  } =
    isCreate
      ? { feature_key: '', dept_path_pattern: '%', enabled: false, rollout_percent: 100 }
      : {
          feature_key: state.flag.feature_key,
          dept_path_pattern: state.flag.dept_path_pattern,
          enabled: state.flag.enabled,
          rollout_percent: state.flag.rollout_percent,
        }

  return (
    <Modal
      title={isCreate ? '新建灰度规则' : `编辑灰度规则「${state.mode === 'edit' ? state.flag.feature_key : ''}」`}
      open
      onCancel={onClose}
      confirmLoading={saving}
      okText="保存"
      cancelText="取消"
      onOk={() => void form.submit()}
      destroyOnClose
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={initial}
        onFinish={handleSubmit}
        style={{ marginTop: 16 }}
      >
        <Form.Item
          label="特性 key（仅小写字母、数字、下划线）"
          name="feature_key"
          rules={[
            { required: true, message: '请选择特性 key' },
            { pattern: /^[a-z0-9_]+$/, message: '仅支持小写字母、数字、下划线' },
          ]}
        >
          {isCreate ? (
            <Select
              showSearch
              placeholder="如 quota / sensitive_filter / rerank"
              options={FEATURE_KEY_OPTIONS}
            />
          ) : (
            <Input disabled />
          )}
        </Form.Item>
        <Form.Item
          label={
            <span>
              部门匹配 pattern（SQL LIKE 风格，% 表示任意串）
              <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                示例：公司/研发中心/% 或 %
              </Text>
            </span>
          }
          name="dept_path_pattern"
          rules={[
            { required: true, message: '请输入部门匹配 pattern' },
            { max: 500, message: '最长 500 字符' },
          ]}
        >
          <Input placeholder="公司/研发中心/%" maxLength={500} />
        </Form.Item>
        <Form.Item label="是否启用" name="enabled" valuePropName="checked">
          <Switch checkedChildren="开" unCheckedChildren="关" />
        </Form.Item>
        <Form.Item
          label="放量百分比（0-100，命中部门后按用户 hash 落桶）"
          name="rollout_percent"
          rules={[
            { required: true, message: '请输入' },
            { type: 'number', min: 0, max: 100, message: '范围 0-100' },
          ]}
        >
          <InputNumber style={{ width: '100%' }} min={0} max={100} step={10} />
        </Form.Item>
      </Form>
    </Modal>
  )
}
