// 敏感词管理面板 —— DB 表 + admin 管理，命中即拒答（M6 续篇）
// 列表分页 + 模糊搜；支持单个新增与批量录入（多行/逗号分隔）
import { useCallback, useEffect, useState } from 'react'
import { App, Button, Input, Modal, Popconfirm, Space, Table, Tag } from 'antd'
import { DeleteOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType, TablePaginationConfig } from 'antd/es/table'
import dayjs from 'dayjs'

import {
  batchCreateSensitiveWords,
  createSensitiveWord,
  deleteSensitiveWord,
  fetchSensitiveWords,
  type SensitiveWord,
} from '../../mocks/data'
import { errMsg } from './common'

// 分类 → Tag 颜色（仅展示用，可空）
const CATEGORY_COLOR: Record<string, string> = {
  政治: 'red',
  违法: 'volcano',
  辱骂: 'orange',
  广告: 'gold',
}

export function SensitiveWordsPanel() {
  const { message } = App.useApp()
  const [items, setItems] = useState<SensitiveWord[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [loading, setLoading] = useState(false)
  const [keyword, setKeyword] = useState('')
  const [creating, setCreating] = useState<{ mode: 'single' } | { mode: 'batch' } | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await fetchSensitiveWords(page, pageSize, keyword || undefined)
      // 后端返回是当前页的列表，total 需要从响应头里取不到，这里前端用 items.length 兜底显示
      setItems(data)
      setTotal(data.length >= pageSize ? page * pageSize + 1 : (page - 1) * pageSize + data.length)
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }, [message, page, pageSize, keyword])

  useEffect(() => {
    void load()
  }, [load])

  function handleTableChange(pagination: TablePaginationConfig) {
    setPage(pagination.current ?? 1)
    setPageSize(pagination.pageSize ?? 50)
  }

  async function handleDelete(row: SensitiveWord) {
    try {
      await deleteSensitiveWord(row.id)
      message.success('敏感词已删除，缓存立即失效')
      await load()
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  const columns: ColumnsType<SensitiveWord> = [
    { title: '敏感词', dataIndex: 'word', key: 'word', width: 200 },
    {
      title: '分类',
      dataIndex: 'category',
      key: 'category',
      width: 120,
      render: (v: string | null) =>
        v ? <Tag color={CATEGORY_COLOR[v] ?? 'default'}>{v}</Tag> : <Tag>未分类</Tag>,
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 160,
      render: (v: string) => dayjs(v).format('YYYY-MM-DD HH:mm'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 100,
      render: (_, record) => (
        <Popconfirm
          title={`确认删除敏感词「${record.word}」？`}
          okText="删除"
          okButtonProps={{ danger: true }}
          cancelText="取消"
          onConfirm={() => handleDelete(record)}
        >
          <Button type="link" size="small" danger icon={<DeleteOutlined />}>
            删除
          </Button>
        </Popconfirm>
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
        }}
      >
        <Space>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating({ mode: 'single' })}>
            新增敏感词
          </Button>
          <Button icon={<PlusOutlined />} onClick={() => setCreating({ mode: 'batch' })}>
            批量录入
          </Button>
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
        </Space>
        <Input.Search
          placeholder="按敏感词搜索"
          allowClear
          value={keyword}
          onChange={(e) => {
            setKeyword(e.target.value)
            setPage(1)
          }}
          style={{ width: 240 }}
        />
      </div>

      <Table<SensitiveWord>
        rowKey="id"
        columns={columns}
        dataSource={items}
        loading={loading}
        size="middle"
        onChange={handleTableChange}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          pageSizeOptions: ['20', '50', '100', '200', '500'],
          showTotal: (t) => `共 ${t} 条`,
        }}
      />

      {creating && (
        <SensitiveCreateModal
          mode={creating.mode}
          onClose={() => setCreating(null)}
          onSaved={async () => {
            setCreating(null)
            await load()
          }}
        />
      )}
    </div>
  )
}

// ── 新增敏感词弹窗（单个 / 批量）──────────────────────────
function SensitiveCreateModal({
  mode,
  onClose,
  onSaved,
}: {
  mode: 'single' | 'batch'
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const { message } = App.useApp()
  const [word, setWord] = useState('')
  const [batchText, setBatchText] = useState('')
  const [category, setCategory] = useState('')
  const [saving, setSaving] = useState(false)

  async function handleSubmit() {
    setSaving(true)
    try {
      if (mode === 'single') {
        const w = word.trim()
        if (!w) {
          message.error('敏感词不可为空')
          setSaving(false)
          return
        }
        await createSensitiveWord(w, category.trim() || null)
        message.success('敏感词已添加，缓存立即失效')
      } else {
        // 批量：按换行或逗号拆分
        const pieces = batchText
          .split(/[\n,，]/)
          .map((s) => s.trim())
          .filter(Boolean)
        if (pieces.length === 0) {
          message.error('未识别到任何敏感词')
          setSaving(false)
          return
        }
        const result = await batchCreateSensitiveWords(pieces, category.trim() || null)
        message.success(`已新增 ${result.added} 条，跳过重复 ${result.duplicates_skipped} 条`)
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
      title={mode === 'single' ? '新增敏感词' : '批量录入敏感词'}
      open
      onCancel={onClose}
      confirmLoading={saving}
      okText="保存"
      cancelText="取消"
      onOk={() => void handleSubmit()}
      destroyOnClose
    >
      {mode === 'single' ? (
        <div style={{ marginTop: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div>
            <div style={{ marginBottom: 4 }}>敏感词</div>
            <Input
              placeholder="输入敏感词，会自动转小写"
              value={word}
              onChange={(e) => setWord(e.target.value)}
              maxLength={200}
              autoFocus
            />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>分类（可选）</div>
            <Input
              placeholder="如：政治 / 违法 / 辱骂 / 广告"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              maxLength={50}
            />
          </div>
        </div>
      ) : (
        <div style={{ marginTop: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div>
            <div style={{ marginBottom: 4 }}>敏感词列表（每行一个，或用逗号分隔）</div>
            <Input.TextArea
              placeholder={'敏感词1\n敏感词2\n敏感词3'}
              value={batchText}
              onChange={(e) => setBatchText(e.target.value)}
              autoSize={{ minRows: 6, maxRows: 12 }}
              autoFocus
            />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>统一分类（可选，留空则不分类）</div>
            <Input
              placeholder="如：政治"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              maxLength={50}
            />
          </div>
        </div>
      )}
    </Modal>
  )
}
