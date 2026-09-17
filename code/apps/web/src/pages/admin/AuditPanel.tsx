// 审计日志面板 —— 谁在何时做了什么（M5 任务 3 提前实现）
// 服务端分页 + 动作前缀过滤；详情列展示动作关键信息（问题/文件名/成员变更等）
import { useCallback, useEffect, useState } from 'react'
import { App, Select, Space, Table, Tag, Tooltip } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType, TablePaginationConfig } from 'antd/es/table'
import dayjs from 'dayjs'

import { AuditLogItem, fetchAuditLogs } from '../../mocks/data'
import { errMsg } from './common'

// 动作 → 标签颜色与中文说明
const ACTION_META: Record<string, { color: string; label: string }> = {
  'chat.ask': { color: 'blue', label: '提问' },
  'doc.upload': { color: 'green', label: '上传文档' },
  'doc.delete': { color: 'red', label: '删除文档' },
  'kb.create': { color: 'cyan', label: '新建知识库' },
  'acl.member.set': { color: 'purple', label: '变更库成员' },
  'auth.login.success': { color: 'success', label: '登录成功' },
  'auth.login.fail': { color: 'error', label: '登录失败' },
  'auth.login.locked': { color: 'volcano', label: '账户锁定' },
}

const ACTION_FILTERS = [
  { value: '', label: '全部动作' },
  { value: 'chat', label: '提问' },
  { value: 'doc', label: '文档操作' },
  { value: 'kb', label: '知识库操作' },
  { value: 'acl', label: '权限变更' },
  { value: 'auth', label: '登录' },
]

/** 从 detail 中提取一行摘要文本（按动作类型取关键字段）。 */
function summarize(item: AuditLogItem): string {
  const d = item.detail ?? {}
  if (item.action === 'chat.ask') {
    const q = typeof d.question === 'string' ? d.question : ''
    const refused = d.refused === true ? '（已拒答）' : ''
    return q ? `${q}${refused}` : '—'
  }
  if (item.action === 'doc.upload' || item.action === 'doc.delete') {
    return typeof d.filename === 'string' ? d.filename : '—'
  }
  if (item.action === 'kb.create') {
    return typeof d.name === 'string' ? d.name : '—'
  }
  if (item.action === 'acl.member.set') {
    const added = Array.isArray(d.added) ? d.added.length : 0
    const removed = Array.isArray(d.removed) ? d.removed.length : 0
    return `新增 ${added} 个 / 移除 ${removed} 个成员`
  }
  if (item.action.startsWith('auth.login')) {
    return typeof d.reason === 'string' ? d.reason : '—'
  }
  return '—'
}

export function AuditPanel() {
  const { message } = App.useApp()
  const [items, setItems] = useState<AuditLogItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [loading, setLoading] = useState(false)
  const [action, setAction] = useState('')

  const load = useCallback(
    async (p: number, ps: number, act: string) => {
      setLoading(true)
      try {
        const data = await fetchAuditLogs(p, ps, act || undefined)
        setItems(data.items)
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
    void load(page, pageSize, action)
  }, [page, pageSize, action, load])

  const handleTableChange = (pagination: TablePaginationConfig) => {
    setPage(pagination.current ?? 1)
    setPageSize(pagination.pageSize ?? 20)
  }

  const columns: ColumnsType<AuditLogItem> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 160,
      render: (v: string) => (v ? dayjs(v).format('MM-DD HH:mm:ss') : '—'),
    },
    {
      title: '操作人',
      dataIndex: 'user_label',
      key: 'user_label',
      width: 120,
      ellipsis: true,
    },
    {
      title: '动作',
      dataIndex: 'action',
      key: 'action',
      width: 140,
      render: (v: string) => {
        const meta = ACTION_META[v]
        return <Tag color={meta?.color ?? 'default'}>{meta?.label ?? v}</Tag>
      },
    },
    {
      title: '详情',
      key: 'detail',
      ellipsis: true,
      render: (_, record) => {
        const text = summarize(record)
        return (
          <Tooltip title={text} placement="topLeft">
            <span>{text}</span>
          </Tooltip>
        )
      },
    },
    {
      title: 'IP',
      dataIndex: 'ip',
      key: 'ip',
      width: 130,
      render: (v: string | null) => v ?? '—',
    },
  ]

  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Select
          value={action}
          onChange={(v) => {
            setAction(v)
            setPage(1)
          }}
          options={ACTION_FILTERS}
          style={{ width: 160 }}
        />
        <a onClick={() => void load(page, pageSize, action)}>
          <ReloadOutlined /> 刷新
        </a>
      </Space>
      <Table<AuditLogItem>
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
          showTotal: (t) => `共 ${t} 条`,
        }}
      />
    </div>
  )
}
