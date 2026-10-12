// 提示词版本管理面板 —— DB 存版本，约束段锁在代码里（方案 A）
// 主列表展示各提示词 key 的当前状态；点「管理版本」抽屉内可新建 / 激活 / 归档 / 对比
// 激活即时生效（后端写后清 Redis 缓存），约束段由后端 fail-closed 校验兜底
import { useCallback, useEffect, useState } from 'react'
import {
  App,
  Button,
  Drawer,
  Modal,
  Input,
  Popconfirm,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { DiffOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import dayjs from 'dayjs'

import {
  activatePromptVersion,
  archivePromptVersion,
  createPromptVersion,
  fetchPromptDiff,
  fetchPrompts,
  fetchPromptVersionDetail,
  fetchPromptVersions,
  type PromptKeyState,
  type PromptVersion,
} from '../../mocks/data'
import { errMsg } from './common'

const { Text, Paragraph } = Typography

// 版本状态 → Tag 颜色
const STATUS_TAG: Record<PromptVersion['status'], { color: string; label: string }> = {
  draft: { color: 'default', label: '草稿' },
  active: { color: 'green', label: '生效中' },
  archived: { color: 'orange', label: '已归档' },
}

export function PromptPanel() {
  const { message } = App.useApp()
  const [items, setItems] = useState<PromptKeyState[]>([])
  const [loading, setLoading] = useState(true)
  const [managing, setManaging] = useState<PromptKeyState | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setItems(await fetchPrompts())
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    void load()
  }, [load])

  const columns: ColumnsType<PromptKeyState> = [
    {
      title: '提示词',
      dataIndex: 'label',
      key: 'label',
      render: (v: string, record) => (
        <Space direction="vertical" size={0}>
          <Text strong>{v}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {record.key}
          </Text>
        </Space>
      ),
    },
    {
      title: '生效版本',
      dataIndex: 'active_version',
      key: 'active_version',
      width: 140,
      render: (v: number | null) =>
        v === null ? <Tag>内置默认</Tag> : <Tag color="green">v{v}</Tag>,
    },
    {
      title: '状态',
      dataIndex: 'is_customized',
      key: 'is_customized',
      width: 120,
      render: (v: boolean) =>
        v ? <Tag color="blue">已自定义</Tag> : <Tag color="default">内置默认</Tag>,
    },
    {
      title: '版本数',
      dataIndex: 'version_count',
      key: 'version_count',
      width: 90,
    },
    {
      title: '操作',
      key: 'actions',
      width: 140,
      render: (_, record) => (
        <Button type="link" size="small" onClick={() => setManaging(record)}>
          管理版本
        </Button>
      ),
    },
  ]

  return (
    <div>
      <div style={{ marginBottom: 16, display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
          刷新
        </Button>
      </div>

      <Table<PromptKeyState>
        rowKey="key"
        columns={columns}
        dataSource={items}
        loading={loading}
        size="middle"
        pagination={false}
      />

      <Text type="secondary" style={{ display: 'block', marginTop: 12 }}>
        提示词分「锁定区」与「可编辑区」：锁定区（角色设定 + 合规约束 + 拒答文案）代码内置，不可在线修改；
        可编辑区可落库切版本。生效版本即锁定区 + 可编辑区拼装结果，激活后 60 秒内全量生效。
      </Text>

      {managing && (
        <PromptDetailDrawer
          state={managing}
          onClose={() => setManaging(null)}
          onChanged={async () => {
            await load()
            // 同步刷新抽屉内选中项的生效版本
            try {
              const fresh = (await fetchPrompts()).find((p) => p.key === managing.key)
              if (fresh) setManaging(fresh)
            } catch {
              // 刷新失败保持原状态，列表已提示
            }
          }}
        />
      )}
    </div>
  )
}

// ── 版本管理抽屉 ─────────────────────────────────────────
function PromptDetailDrawer({
  state,
  onClose,
  onChanged,
}: {
  state: PromptKeyState
  onClose: () => void
  onChanged: () => Promise<void>
}) {
  const { message } = App.useApp()
  const [versions, setVersions] = useState<PromptVersion[]>([])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [detail, setDetail] = useState<{ version: number; built: string } | null>(null)
  const [diffText, setDiffText] = useState<{ version: number; diff: string } | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setVersions(await fetchPromptVersions(state.key))
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }, [message, state.key])

  useEffect(() => {
    void load()
  }, [load])

  async function handleActivate(row: PromptVersion) {
    try {
      await activatePromptVersion(state.key, row.id)
      message.success(`v${row.version} 已激活，立即生效`)
      await load()
      await onChanged()
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  async function handleArchive(row: PromptVersion) {
    try {
      await archivePromptVersion(state.key, row.id)
      message.success(`v${row.version} 已归档`)
      await load()
      await onChanged()
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  async function handleDetail(row: PromptVersion) {
    try {
      const d = await fetchPromptVersionDetail(state.key, row.id)
      setDetail({ version: d.version, built: d.built })
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  async function handleDiff(row: PromptVersion) {
    try {
      const d = await fetchPromptDiff(state.key, row.id)
      setDiffText({ version: row.version, diff: d.diff })
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  const columns: ColumnsType<PromptVersion> = [
    {
      title: '版本',
      dataIndex: 'version',
      key: 'version',
      width: 80,
      render: (v: number) => `v${v}`,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (v: PromptVersion['status']) => (
        <Tag color={STATUS_TAG[v].color}>{STATUS_TAG[v].label}</Tag>
      ),
    },
    {
      title: '备注',
      dataIndex: 'note',
      key: 'note',
      ellipsis: true,
      render: (v: string | null) => v || <Text type="secondary">—</Text>,
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
      width: 220,
      render: (_, record) => (
        <Space size={2}>
          {record.status !== 'active' && record.status !== 'archived' && (
            <Popconfirm
              title={`激活 v${record.version}？`}
              description="激活后立即生效，同 key 原生效版本将自动归档。"
              okText="激活"
              cancelText="取消"
              onConfirm={() => handleActivate(record)}
            >
              <Button type="link" size="small">
                激活
              </Button>
            </Popconfirm>
          )}
          {record.status !== 'archived' && (
            <Popconfirm
              title={`归档 v${record.version}？`}
              description={
                record.status === 'active' ? '归档生效版本后，该提示词将回落到内置默认。' : undefined
              }
              okText="归档"
              cancelText="取消"
              onConfirm={() => handleArchive(record)}
            >
              <Button type="link" size="small">
                归档
              </Button>
            </Popconfirm>
          )}
          <Button type="link" size="small" onClick={() => handleDetail(record)}>
            详情
          </Button>
          <Button
            type="link"
            size="small"
            icon={<DiffOutlined />}
            onClick={() => handleDiff(record)}
          >
            对比
          </Button>
        </Space>
      ),
    },
  ]

  return (
    <Drawer
      title={`提示词版本管理 · ${state.label}`}
      width={920}
      open
      onClose={onClose}
      destroyOnClose
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <div>
          <Text strong>锁定区（代码内置，不可在线修改）</Text>
          <Paragraph>
            <pre
              style={{
                margin: '8px 0 0',
                padding: 12,
                background: '#f5f5f5',
                borderRadius: 6,
                maxHeight: 220,
                overflow: 'auto',
                whiteSpace: 'pre-wrap',
                fontSize: 12,
              }}
            >
              {state.locked}
            </pre>
          </Paragraph>
        </div>

        <div>
          <Space style={{ width: '100%', justifyContent: 'space-between' }}>
            <Space>
              <Text strong>版本列表</Text>
              <Tooltip title="生效版本：内置默认 = 未切换到任何 DB 版本">
                <Text type="secondary" style={{ fontSize: 12 }}>
                  当前：{state.active_version === null ? '内置默认' : `v${state.active_version}`}
                </Text>
              </Tooltip>
            </Space>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating(true)}>
              新建版本
            </Button>
          </Space>

          <Table<PromptVersion>
            rowKey="id"
            columns={columns}
            dataSource={versions}
            loading={loading}
            size="small"
            pagination={false}
            style={{ marginTop: 8 }}
            locale={{ emptyText: '暂无自定义版本（当前使用内置默认可编辑区）' }}
          />
        </div>
      </Space>

      {creating && (
        <PromptCreateModal
          promptKey={state.key}
          label={state.label}
          activeVersionId={state.active_version_id}
          fallbackEditable={state.default_editable}
          onClose={() => setCreating(false)}
          onSaved={async () => {
            setCreating(false)
            await load()
          }}
        />
      )}

      <Modal
        title={detail ? `v${detail.version} 完整生效文本（锁定区 + 可编辑区）` : ''}
        open={detail !== null}
        onCancel={() => setDetail(null)}
        footer={<Button onClick={() => setDetail(null)}>关闭</Button>}
        width={760}
        destroyOnClose
      >
        <pre
          style={{
            margin: 0,
            padding: 12,
            background: '#f5f5f5',
            borderRadius: 6,
            maxHeight: 480,
            overflow: 'auto',
            whiteSpace: 'pre-wrap',
            fontSize: 12,
          }}
        >
          {detail?.built}
        </pre>
      </Modal>

      <Modal
        title={diffText ? `v${diffText.version} 与内置默认的差异` : ''}
        open={diffText !== null}
        onCancel={() => setDiffText(null)}
        footer={<Button onClick={() => setDiffText(null)}>关闭</Button>}
        width={760}
        destroyOnClose
      >
        <pre
          style={{
            margin: 0,
            padding: 12,
            background: '#f5f5f5',
            borderRadius: 6,
            maxHeight: 480,
            overflow: 'auto',
            whiteSpace: 'pre-wrap',
            fontSize: 12,
          }}
        >
          {diffText?.diff || '（无差异）'}
        </pre>
      </Modal>
    </Drawer>
  )
}

// ── 新建版本弹窗 ─────────────────────────────────────────
function PromptCreateModal({
  promptKey,
  label,
  activeVersionId,
  fallbackEditable,
  onClose,
  onSaved,
}: {
  promptKey: string
  label: string
  activeVersionId: string | null
  fallbackEditable: string
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const { message } = App.useApp()
  const [content, setContent] = useState(fallbackEditable)
  const [note, setNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [prefilling, setPrefilling] = useState(activeVersionId !== null)

  // 若有生效版本，以其可编辑区作为起点（编辑现状，而非回到内置默认）
  useEffect(() => {
    if (!activeVersionId) return
    let alive = true
    void (async () => {
      try {
        const d = await fetchPromptVersionDetail(promptKey, activeVersionId)
        if (alive) setContent(d.content)
      } catch {
        // 预填失败保留内置默认，不阻断新建
      } finally {
        if (alive) setPrefilling(false)
      }
    })()
    return () => {
      alive = false
    }
  }, [promptKey, activeVersionId])

  async function handleSubmit() {
    const trimmed = content.trim()
    if (!trimmed) {
      message.error('可编辑区内容不可为空')
      return
    }
    setSaving(true)
    try {
      await createPromptVersion(promptKey, { content, note: note.trim() || null })
      message.success('已新建草稿版本，需「激活」后生效')
      await onSaved()
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={`新建版本 · ${label}`}
      open
      onCancel={onClose}
      confirmLoading={saving}
      okText="保存草稿"
      cancelText="取消"
      onOk={() => void handleSubmit()}
      width={760}
      destroyOnClose
    >
      <Space direction="vertical" size={12} style={{ width: '100%', marginTop: 12 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          仅编辑「可编辑区」；锁定区（角色设定 / 合规约束 / 拒答文案）由后端自动拼接，无需重复。
        </Text>
        <div>
          <div style={{ marginBottom: 4 }}>可编辑区内容</div>
          <Input.TextArea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            autoSize={{ minRows: 10, maxRows: 22 }}
            disabled={prefilling}
            placeholder="填写可编辑区内容，占位符请使用 {history}、{question} 等已声明的名称"
          />
          {prefilling && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              正在载入当前生效版本...
            </Text>
          )}
        </div>
        <div>
          <div style={{ marginBottom: 4 }}>备注（可选）</div>
          <Input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            maxLength={500}
            placeholder="如：调整追问建议措辞"
          />
        </div>
      </Space>
    </Modal>
  )
}